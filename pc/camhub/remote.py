"""Away-from-home access: the phone app reaches this PC directly, with no outside service.

How it stays safe:
- A second, encrypted-only port (8443 by default) is opened just for paired phones. It uses
  this PC's own certificate, made here on first use. The phone app is given the certificate's
  fingerprint when it is paired at home and refuses anything else, so nobody can pretend to
  be this PC, even on a hostile Wi-Fi.
- Phones are paired at home, on the home network. Each gets its own long random key (only a
  hash is stored here). Keys can be removed one by one in Settings.
- The away port never offers the password page. Without a paired key every request gets a
  plain "not found", and an address that keeps trying is blocked for an hour.
- The router port can be opened automatically (UPnP, spoken only to the router itself) or by
  hand. The home's internet address is read from the router, so no website is asked.
"""
import asyncio
import collections
import datetime
import hashlib
import ipaddress
import logging
import os
import secrets
import subprocess
import threading
import time

from . import upnp

log = logging.getLogger("camhub.remote")

COOKIE = "camhub_device"
FIREWALL_RULE = "Home Camera away access"
MAPPING_NAME = "Home Camera away access"
DEFAULTS = {
    "enabled": False,
    "port": 8443,
    # Ask the router to open the port (UPnP). Off means the port is forwarded by hand.
    "upnp": True,
    # The user's own name for their home, e.g. a hostname from the router's DDNS. Optional.
    "address": "",
    "server_id": "",
    # {"id", "name", "key_hash", "created", "last_seen", "last_ip"}
    "devices": [],
}
MAX_FAILURES = 10          # unpaired requests from one address ...
FAILURE_WINDOW = 600       # ... within 10 minutes ...
BAN_SECONDS = 3600         # ... blocks it for an hour.
UPNP_EVERY = 1800          # renew the router mapping every 30 minutes (lease 1 hour)
ADDRESS_EVERY = 300        # re-read the home's internet address every 5 minutes


def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


HOME_NETWORKS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10")]


def is_private(ip):
    """An address on a home network (not just "not on the internet", which includes test ranges)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in HOME_NETWORKS if n.version == a.version)


def shared_address(ip):
    """True for an internet address that isn't really the home's own (carrier-grade NAT)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a in ipaddress.ip_network("100.64.0.0/10") or is_private(ip)


# ---- the certificate ------------------------------------------------------------------------

def make_certificate(cert_path, key_path):
    """A self-made certificate (ECDSA P-256, 20 years). Phones trust it by fingerprint."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Home Camera")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365 * 20))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def fingerprint(cert_path):
    """SHA-256 of the certificate, as lowercase hex (what the phone checks)."""
    import ssl
    with open(cert_path, "r", encoding="ascii") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def pretty_fingerprint(fp):
    return " ".join(fp[i:i + 4].upper() for i in range(0, len(fp), 4))


# ---- guessing protection -----------------------------------------------------------------------

class Guard:
    """Counts unpaired requests per address and blocks addresses that keep trying."""

    def __init__(self, max_failures=MAX_FAILURES, window=FAILURE_WINDOW, ban=BAN_SECONDS):
        self.max, self.window, self.ban = max_failures, window, ban
        self._fails = {}
        self._banned = {}
        self._lock = threading.Lock()
        self.blocked_log = collections.deque(maxlen=2000)  # times of refused requests

    def banned(self, ip):
        with self._lock:
            until = self._banned.get(ip)
            if until and until > time.time():
                return True
            self._banned.pop(ip, None)
            return False

    def failed(self, ip):
        now = time.time()
        with self._lock:
            self.blocked_log.append(now)
            times = [t for t in self._fails.get(ip, []) if t > now - self.window] + [now]
            self._fails[ip] = times
            if len(times) >= self.max:
                self._banned[ip] = now + self.ban
                self._fails.pop(ip, None)
                log.warning("Away access: blocked %s for an hour after %d tries without a paired key", ip, len(times))
            if len(self._fails) > 5000:  # don't grow forever under a scan
                self._fails = {k: v for k, v in self._fails.items() if v[-1] > now - self.window}

    def refused_since(self, seconds):
        cut = time.time() - seconds
        with self._lock:
            return sum(1 for t in self.blocked_log if t > cut)

    def banned_count(self):
        now = time.time()
        with self._lock:
            return sum(1 for u in self._banned.values() if u > now)


# ---- the gate in front of the away port ----------------------------------------------------------

def _key_from_scope(scope):
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            v = value.decode("latin-1")
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v.strip()
    return ""


class Gate:
    """ASGI wrapper for the away port: only requests carrying a paired phone's key get through."""

    SECURITY_HEADERS = [
        (b"strict-transport-security", b"max-age=31536000"),
        (b"cache-control", b"no-store"),
    ]

    def __init__(self, app, remote):
        self.app = app
        self.remote = remote

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        ip = (scope.get("client") or ("?", 0))[0]
        guard = self.remote.guard
        if guard.banned(ip):
            return await self._refuse(scope, send, 429)
        device = self.remote.device_for(_key_from_scope(scope))
        if device is None:
            guard.failed(ip)
            return await self._refuse(scope, send, 404)
        self.remote.seen(device, ip)
        scope = dict(scope)
        scope["camhub.device"] = device["id"]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                names = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    h for h in self.SECURITY_HEADERS if h[0] not in names]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _refuse(scope, send, status):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", b"9")]})
        await send({"type": "http.response.body", "body": b"Not found"})


# ---- the feature ---------------------------------------------------------------------------------

class RemoteAccess:
    def __init__(self, config, events=None, folder=None):
        self.config = config
        self.events = events
        self.folder = folder or os.path.join(os.path.dirname(config.path), "remote")
        self.cert_path = os.path.join(self.folder, "cert.pem")
        self.key_path = os.path.join(self.folder, "key.pem")
        self.guard = Guard()
        self._lock = threading.RLock()
        self._loop = None
        self._app = None
        self._server = None
        self._task = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._dirty_seen = 0.0
        self.router = {"state": "off", "message": "", "external_ip": "", "router": "", "checked": ""}
        self.listening = {"state": "off", "message": ""}
        self.firewall = ""
        cfg = config.data.setdefault("remote", {})
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, list(v) if isinstance(v, list) else v)
        if not cfg["server_id"]:
            cfg["server_id"] = secrets.token_hex(8)
            config.save()

    @property
    def cfg(self):
        return self.config.data["remote"]

    # ---- certificate -------------------------------------------------------------

    def ensure_certificate(self):
        if not (os.path.isfile(self.cert_path) and os.path.isfile(self.key_path)):
            make_certificate(self.cert_path, self.key_path)
            log.info("Away access: made this PC's certificate")
        return fingerprint(self.cert_path)

    def fingerprint(self):
        try:
            return fingerprint(self.cert_path)
        except OSError:
            return ""

    # ---- paired phones -------------------------------------------------------------

    def device_for(self, key):
        if not key or len(key) < 32:
            return None
        h = key_hash(key)
        with self._lock:
            for d in self.cfg["devices"]:
                if secrets.compare_digest(d.get("key_hash", ""), h):
                    return d
        return None

    def pair(self, name):
        key = secrets.token_urlsafe(32)
        dev = {"id": secrets.token_hex(6), "name": (str(name or "").strip() or "Phone")[:40],
               "key_hash": key_hash(key), "created": _now_iso(), "last_seen": "", "last_ip": ""}
        with self._lock:
            self.cfg["devices"].append(dev)
            self.config.save()
        log.info("Away access: paired %s", dev["name"])
        if self.events is not None:
            self.events.publish({"type": "health", "state": "ok", "key": "remote-pair-" + dev["id"],
                                 "kind": "security", "title": "New phone set up for away access",
                                 "message": f"{dev['name']} can now see your cameras from outside the house. "
                                            "If that wasn't you, remove it in Settings > Away from home."})
        return dev, key

    def remove(self, device_id):
        with self._lock:
            before = len(self.cfg["devices"])
            self.cfg["devices"] = [d for d in self.cfg["devices"] if d["id"] != device_id]
            changed = len(self.cfg["devices"]) != before
            if changed:
                self.config.save()
        return changed

    def seen(self, device, ip):
        now = time.time()
        device["last_ip"] = ip
        device["last_seen"] = _now_iso()
        # Save now and then, not on every request.
        if now - self._dirty_seen > 3600:
            self._dirty_seen = now
            self.config.save()

    def devices(self):
        with self._lock:
            return [{k: d.get(k, "") for k in ("id", "name", "created", "last_seen", "last_ip")}
                    for d in self.cfg["devices"]]

    # ---- addresses -----------------------------------------------------------------

    def lan_ip(self):
        # The address facing the router, so a VPN or second network card isn't picked by mistake.
        return upnp.local_ip_towards(self.router.get("router") or "192.0.2.1")

    def away_host(self):
        return (self.cfg.get("address") or "").strip() or self.router.get("external_ip", "")

    def addresses(self):
        port = int(self.cfg["port"])
        lan = self.lan_ip()
        away = self.away_host()
        return {"port": port, "lan": [lan] if lan else [], "away_host": away,
                "home_url": f"https://{lan}:{port}" if lan else "",
                "away_url": f"https://{_bracket(away)}:{port}" if away else ""}

    # ---- settings -------------------------------------------------------------------

    def update(self, body):
        cfg = self.cfg
        if "port" in body:
            try:
                port = int(body["port"])
            except (TypeError, ValueError):
                raise ValueError("The port must be a number.")
            if not 1024 <= port <= 65535:
                raise ValueError("Choose a port from 1024 to 65535.")
            if port == int(self.config.server.get("port", 8080)):
                raise ValueError("That port is already used by the viewer at home. Choose another.")
            cfg["port"] = port
        if "upnp" in body:
            cfg["upnp"] = bool(body["upnp"])
        if "address" in body:
            addr = str(body["address"] or "").strip().lower()
            addr = addr.removeprefix("https://").removeprefix("http://").split("/")[0]
            if addr and ":" in addr and not addr.startswith("["):
                try:
                    ipaddress.IPv6Address(addr)
                except ValueError:
                    addr = addr.rsplit(":", 1)[0]  # drop a port the user typed
            if addr and not all(c.isalnum() or c in ".-:[]" for c in addr):
                raise ValueError("Enter just a name like myhome.example.com, or an address.")
            cfg["address"] = addr[:253]
        old_port = getattr(self, "_running_port", None)
        if "enabled" in body:
            cfg["enabled"] = bool(body["enabled"])
            if cfg["enabled"]:
                self.ensure_certificate()
        self.config.save()
        self.apply(old_port)
        return self.status()

    def status(self):
        a = self.addresses()
        ext = self.router.get("external_ip", "")
        warn = ""
        if self.cfg["enabled"] and ext and shared_address(ext):
            warn = ("Your router's internet address is shared with other customers (carrier-grade NAT), "
                    "so phones outside can't reach it however the router is set. Ask your internet provider "
                    "for a public IPv4 address (often free, sometimes called a static or dedicated IP).")
        return {
            "enabled": bool(self.cfg["enabled"]), "port": a["port"], "upnp": bool(self.cfg["upnp"]),
            "address": self.cfg.get("address", ""), "lan_ip": (a["lan"] or [""])[0],
            "away_host": a["away_host"], "away_url": a["away_url"],
            "fingerprint": pretty_fingerprint(self.fingerprint()) if self.cfg["enabled"] else "",
            "listening": dict(self.listening), "router": dict(self.router), "firewall": self.firewall,
            "warning": warn, "devices": self.devices(),
            "refused_24h": self.guard.refused_since(86400), "blocked_now": self.guard.banned_count(),
        }

    # ---- running the away port ---------------------------------------------------------

    def attach(self, app, loop):
        """Called by the web server once it is running, so the away port can be started and stopped."""
        self._app = app
        self._loop = loop
        self._thread = threading.Thread(target=self._router_loop, name="remote-router", daemon=True)
        self._thread.start()
        self.apply()

    def apply(self, old_port=None):
        if self._loop is None:
            return
        self._old_port = old_port
        self._wake.set()
        future = asyncio.run_coroutine_threadsafe(self._restart(), self._loop)
        try:
            in_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            in_loop = False
        if not in_loop:  # wait, so Settings shows whether it worked
            try:
                future.result(10)
            except Exception:
                pass

    async def _restart(self):
        await self._stop_server()
        if not self.cfg["enabled"]:
            self.listening = {"state": "off", "message": ""}
            return
        import uvicorn

        class QuietServer(uvicorn.Server):
            """The main server handles Ctrl+C; this one mustn't take over the signals."""
            def install_signal_handlers(self):
                pass

            def capture_signals(self):
                import contextlib
                return contextlib.nullcontext()

        try:
            self.ensure_certificate()
            port = int(self.cfg["port"])
            config = uvicorn.Config(
                Gate(self._app, self), host=self.config.server.get("host", "0.0.0.0"), port=port,
                ssl_certfile=self.cert_path, ssl_keyfile=self.key_path,
                ssl_ciphers="ECDHE+AESGCM:ECDHE+CHACHA20", lifespan="off", log_level="warning",
                access_log=False, log_config=None, server_header=False, date_header=False,
                limit_concurrency=64, timeout_keep_alive=30)
            server = QuietServer(config)
            self._server = server
            self._running_port = port
            self._task = asyncio.create_task(server.serve())
            for _ in range(50):
                await asyncio.sleep(0.1)
                if server.started or self._task.done():
                    break
            if server.started:
                self.listening = {"state": "ok", "message": f"Listening on port {port} (encrypted)."}
                log.info("Away access: listening on port %s", port)
            else:
                self.listening = {"state": "error", "message": f"Couldn't use port {port}. "
                                  "Another program may be using it: choose another port."}
                self._server = None
        except Exception as e:
            log.exception("Away access didn't start")
            self.listening = {"state": "error", "message": f"Couldn't start: {e}"}
            self._server = None

    async def _stop_server(self):
        server, task = self._server, self._task
        self._server = self._task = None
        if server is not None:
            server.should_exit = True
            if task is not None:
                try:
                    await asyncio.wait_for(task, 5)
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.cfg.get("upnp") and self.cfg.get("enabled"):
            try:
                upnp.Router.find(1.5).delete_mapping(int(self.cfg["port"]))
            except Exception:
                pass

    # ---- router port and the home's internet address ---------------------------------------

    def _router_loop(self):
        last_map = 0.0
        last_addr = 0.0
        mapped_port = None
        while not self._stop.is_set():
            woke = self._wake.is_set()
            self._wake.clear()
            cfg = self.cfg
            now = time.time()
            old = getattr(self, "_old_port", None)
            try:
                if woke:
                    self._firewall(cfg["enabled"], int(cfg["port"]), old)
                if not cfg["enabled"] or not cfg["upnp"]:
                    if mapped_port is not None:
                        self._unmap(mapped_port)
                        mapped_port = None
                    if cfg["enabled"] and (woke or now - last_addr > ADDRESS_EVERY):
                        last_addr = now
                        self._read_address(map_port=None)
                    elif not cfg["enabled"]:
                        self.router = {"state": "off", "message": "", "external_ip": "", "router": "", "checked": ""}
                elif woke or now - last_map > UPNP_EVERY or now - last_addr > ADDRESS_EVERY:
                    want = int(cfg["port"])
                    if mapped_port is not None and mapped_port != want:
                        self._unmap(mapped_port)
                    do_map = woke or now - last_map > UPNP_EVERY or mapped_port != want
                    if self._read_address(map_port=want if do_map else None):
                        if do_map:
                            mapped_port = want
                            last_map = now
                    last_addr = now
            except Exception as e:  # never let this thread die
                log.warning("Away access router check: %s", e)
            self._wake.wait(30)

    def _read_address(self, map_port):
        try:
            router = upnp.Router.find()
            ip = router.external_ip()
            if map_port is not None:
                lan = self.lan_ip()
                router.add_mapping(map_port, lan, map_port, MAPPING_NAME, lease=3600)
                msg = f"Your router is forwarding port {map_port} to this PC ({lan})."
            elif self.cfg["upnp"]:
                msg = self.router.get("message", "")
            else:
                msg = "Port opening by hand. Your home's internet address was read from the router."
            self.router = {"state": "ok", "message": msg, "external_ip": ip, "router": router.host,
                           "checked": _now_iso()}
            return True
        except Exception as e:
            if self.cfg["upnp"]:
                problem = (f"Your router didn't open the port automatically. {e} "
                           "Turn on UPnP in the router's settings, or forward the port by hand (see below).")
            else:
                problem = "Your router didn't say what your home's internet address is. Enter your own address below."
            self.router = {"state": "error" if self.cfg["upnp"] else "manual", "message": problem,
                           "external_ip": self.router.get("external_ip", ""), "router": "", "checked": _now_iso()}
            return False

    def _unmap(self, port):
        try:
            upnp.Router.find().delete_mapping(port)
        except Exception:
            pass

    def _firewall(self, enabled, port, old_port):
        """Lets the away port through Windows Firewall (the recorder runs as SYSTEM, so it may)."""
        if os.name != "nt":
            return
        def netsh(*args):
            return subprocess.run(["netsh", "advfirewall", "firewall", *args], capture_output=True,
                                  creationflags=0x08000000, timeout=20)
        netsh("delete", "rule", f"name={FIREWALL_RULE}")
        if enabled:
            r = netsh("add", "rule", f"name={FIREWALL_RULE}", "dir=in", "action=allow", "protocol=TCP",
                      f"localport={port}", "profile=any")
            self.firewall = "ok" if r.returncode == 0 else "Windows Firewall didn't accept the rule."
        else:
            self.firewall = ""


def _bracket(host):
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


# ---- web routes ---------------------------------------------------------------------------------

def add_remote_routes(app, hub):
    from fastapi import HTTPException, Request

    remote = hub.remote

    def away(request):
        """Came in through the encrypted port from outside the home network."""
        ip = request.client.host if request.client else ""
        return bool(request.scope.get("camhub.device")) and not is_private(ip)

    async def body_of(request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        return body

    @app.get("/api/remote")
    def get_remote(request: Request):
        s = remote.status()
        s["this_device"] = request.scope.get("camhub.device", "")
        return s

    @app.put("/api/remote")
    async def put_remote(request: Request):
        if away(request):
            raise HTTPException(403, "Change away access at home, so you can't lock yourself out.")
        body = await body_of(request)
        try:
            return await asyncio.to_thread(remote.update, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/remote/pair")
    async def pair(request: Request):
        """Sets up the phone app this request came from. Only from the home network."""
        ip = request.client.host if request.client else ""
        if not is_private(ip):
            raise HTTPException(403, "Set up phones at home, on the same Wi-Fi as this PC.")
        if not remote.cfg["enabled"]:
            raise HTTPException(409, "Turn on away access first.")
        body = await body_of(request)
        dev, key = remote.pair(body.get("name"))
        a = remote.addresses()
        return {"key": key, "device": dev["id"], "name": dev["name"], "fingerprint": remote.fingerprint(),
                "server_id": remote.cfg["server_id"], **a}

    @app.get("/api/remote/hello")
    def hello(request: Request):
        """The phone app checks it has the right PC and picks up new addresses."""
        dev = request.scope.get("camhub.device")
        if not dev:
            raise HTTPException(404)
        a = remote.addresses()
        return {"server_id": remote.cfg["server_id"], "device": dev, "away": away(request), **a}

    @app.delete("/api/remote/devices/{device_id}")
    def remove_device(device_id: str, request: Request):
        if device_id == "self":
            device_id = request.scope.get("camhub.device", "")
        if not remote.remove(device_id):
            raise HTTPException(404, "No such phone")
        return {"ok": True, "devices": remote.devices()}
