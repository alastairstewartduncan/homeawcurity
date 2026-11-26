"""Away-from-home access: certificate, pairing, the gate, guessing protection, UPnP.  Run:  python -m pytest tests"""
import asyncio
import hashlib
import http.server
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth, remote, upnp  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.storage import Storage  # noqa: E402


class Detector:
    available = False


class Hub:
    pass


def make_hub(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"server": {"password_hash": auth.hash_password("pw123456")},
                             "storage": {"path": str(tmp_path / "rec")}, "cameras": []}))
    config = Config(str(p))
    hub = Hub()
    hub.config = config
    hub.storage = Storage(config)
    hub.events = EventBus()
    hub.cameras = {}
    hub.detector = Detector()
    hub.remote = remote.RemoteAccess(config, hub.events)
    return hub, p


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_certificate_and_fingerprint(tmp_path):
    cert, key = str(tmp_path / "c.pem"), str(tmp_path / "k.pem")
    remote.make_certificate(cert, key)
    fp = remote.fingerprint(cert)
    der = ssl.PEM_cert_to_DER_cert(open(cert).read())
    assert fp == hashlib.sha256(der).hexdigest() and len(fp) == 64
    assert remote.pretty_fingerprint(fp).count(" ") == 15
    # The key and certificate load as a server certificate.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)


def test_pairing_keys_are_hashed_and_removable(tmp_path):
    hub, path = make_hub(tmp_path)
    r = hub.remote
    events = hub.events.subscribe()
    dev, key = r.pair("Pixel 8")
    assert len(key) >= 40
    saved = json.loads(path.read_text())["remote"]
    assert key not in path.read_text() and saved["devices"][0]["key_hash"] == remote.key_hash(key)
    assert r.device_for(key)["id"] == dev["id"]
    assert r.device_for(key[:-1] + ("A" if key[-1] != "A" else "B")) is None
    assert r.device_for("") is None and r.device_for("short") is None
    assert events.get_nowait()["title"] == "New phone set up for away access"
    assert "key_hash" not in r.devices()[0]
    assert r.remove(dev["id"]) and r.device_for(key) is None and not r.remove(dev["id"])


def test_guard_blocks_an_address_that_keeps_trying():
    g = remote.Guard(max_failures=3, window=60, ban=60)
    for _ in range(2):
        g.failed("203.0.113.5")
    assert not g.banned("203.0.113.5")
    g.failed("203.0.113.5")
    assert g.banned("203.0.113.5") and not g.banned("203.0.113.6")
    assert g.refused_since(60) == 3 and g.banned_count() == 1


def test_settings_validation(tmp_path):
    hub, _ = make_hub(tmp_path)
    r = hub.remote
    with pytest.raises(ValueError):
        r.update({"port": 80})
    with pytest.raises(ValueError):
        r.update({"port": 8080})  # the home viewer's port
    with pytest.raises(ValueError):
        r.update({"address": "my home!"})
    s = r.update({"enabled": True, "port": 9443, "upnp": False, "address": "https://MyHome.example.com:9443/x"})
    assert s["enabled"] and s["port"] == 9443 and s["address"] == "myhome.example.com"
    assert s["away_url"] == "https://myhome.example.com:9443"
    assert len(s["fingerprint"].replace(" ", "")) == 64  # certificate made on first use
    assert remote.shared_address("100.64.1.2") and not remote.shared_address("81.2.69.160")


def test_gate_and_routes(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    hub, _ = make_hub(tmp_path)
    hub.remote.update({"enabled": True, "upnp": False})
    app = create_app(hub)
    gate = remote.Gate(app, hub.remote)
    outside = TestClient(gate, client=("203.0.113.9", 5000))
    home_tls = TestClient(gate, client=("192.168.0.40", 5000))
    lan = TestClient(app, client=("192.168.0.40", 5000))

    # Pairing needs a signed-in user on the home network.
    assert lan.post("/api/remote/pair", json={"name": "Phone"}).status_code == 401
    assert lan.post("/api/login", json={"password": "pw123456"}).status_code == 200
    assert TestClient(app, client=("203.0.113.9", 1)).post(
        "/api/login", json={"password": "pw123456"}).status_code == 200
    r = lan.post("/api/remote/pair", json={"name": "Pixel"})
    assert r.status_code == 200
    p = r.json()
    assert p["fingerprint"] == hub.remote.fingerprint() and p["port"] == 8443 and p["key"]
    key = p["key"]

    # The away port gives nothing without a key: not even the login page.
    for path in ("/", "/login", "/api/login", "/api/status", "/static/app.js"):
        assert outside.get(path).status_code == 404
    # With the key (header or cookie) everything works.
    ok = outside.get("/api/status", headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200 and ok.headers["strict-transport-security"]
    outside.cookies.set(remote.COOKIE, key)
    assert outside.get("/").status_code == 200
    hello = outside.get("/api/remote/hello").json()
    assert hello["away"] and hello["server_id"] == hub.remote.cfg["server_id"]
    assert hello["device"] == p["device"]
    assert hub.remote.devices()[0]["last_ip"] == "203.0.113.9"
    # From outside: settings can't be changed and phones can't be added.
    assert outside.put("/api/remote", json={"enabled": False}).status_code == 403
    assert outside.post("/api/remote/pair", json={"name": "x"}).status_code == 403
    # At home through the encrypted port: allowed.
    home_tls.cookies.set(remote.COOKIE, key)
    assert not home_tls.get("/api/remote/hello").json()["away"]
    # A password token (from the home viewer) isn't accepted on the away port.
    token = lan.post("/api/login", json={"password": "pw123456"}).json()["token"]
    assert TestClient(gate, client=("203.0.113.20", 1)).get(
        "/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 404

    # Removing the phone locks it out at once.
    assert outside.delete("/api/remote/devices/self").status_code == 200
    assert outside.get("/api/status").status_code == 404

    # Guessing: the address is blocked after 10 tries, even with a good key afterwards.
    dev, key2 = hub.remote.pair("Tablet")
    guesser = TestClient(gate, client=("198.51.100.7", 1))
    for _ in range(remote.MAX_FAILURES):
        guesser.get("/api/status", headers={"Authorization": "Bearer " + "x" * 43})
    assert guesser.get("/api/status", headers={"Authorization": f"Bearer {key2}"}).status_code == 429
    assert TestClient(gate, client=("198.51.100.8", 1)).get(
        "/api/status", headers={"Authorization": f"Bearer {key2}"}).status_code == 200


def test_away_port_really_serves_tls_with_this_pcs_certificate(tmp_path):
    """Starts the real encrypted port, as the recorder does, and connects to it."""
    import uvicorn

    from camhub.server import create_app

    hub, _ = make_hub(tmp_path)
    port = free_port()
    hub.config.server["host"] = "127.0.0.1"
    hub.config.server["port"] = free_port()
    hub.remote.update({"enabled": True, "upnp": False, "port": port})
    dev, key = hub.remote.pair("Phone")
    app = create_app(hub)
    lan = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=hub.config.server["port"],
                                        log_level="warning", log_config=None))
    lan.install_signal_handlers = lambda: None
    loop = asyncio.new_event_loop()

    async def main():
        hub.remote.attach(app, asyncio.get_running_loop())
        await lan.serve()

    t = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if hub.remote.listening["state"] != "off":
                break
            time.sleep(0.05)
        assert hub.remote.listening["state"] == "ok", hub.remote.listening
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the phone checks the fingerprint instead
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw) as tls:
                der = tls.getpeercert(binary_form=True)
                assert tls.version() in ("TLSv1.2", "TLSv1.3")
        assert hashlib.sha256(der).hexdigest() == hub.remote.fingerprint()

        def get(path, k):
            req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {k}"})
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, b""

        assert get("/api/remote/hello", key)[0] == 200
        assert get("/api/remote/hello", "nope" * 10)[0] == 404
        # Plain http on the away port doesn't work.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            raw.settimeout(3)
            try:
                reply = raw.recv(100)
            except OSError:
                reply = b""
            assert not reply.startswith(b"HTTP/1.1 200")
        # Turning it off closes the port.
        hub.remote.update({"enabled": False})
        for _ in range(100):
            if hub.remote.listening["state"] == "off":
                break
            time.sleep(0.05)
        time.sleep(0.3)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
    finally:
        hub.remote.stop()
        lan.should_exit = True
        t.join(10)


class FakeRouter(http.server.BaseHTTPRequestHandler):
    calls = []
    permanent_only = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = f"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
          <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType><deviceList><device>
          <deviceList><device><serviceList><service>
            <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
            <controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList>
          </device></deviceList></device></root>""".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        action = self.headers["SOAPAction"].strip('"').split("#")[1]
        FakeRouter.calls.append((action, raw))
        if action == "AddPortMapping" and FakeRouter.permanent_only and "<NewLeaseDuration>0<" not in raw:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"<s:Envelope><s:Body><s:Fault><detail><UPnPError><errorCode>725</errorCode>"
                             b"<errorDescription>OnlyPermanentLeasesSupported</errorDescription></UPnPError>"
                             b"</detail></s:Fault></s:Body></s:Envelope>")
            return
        out = "<NewExternalIPAddress>81.2.69.160</NewExternalIPAddress>" if action == "GetExternalIPAddress" else ""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f'<s:Envelope xmlns:s="x" xmlns:u="y"><s:Body><u:{action}Response>{out}'
                         f'</u:{action}Response></s:Body></s:Envelope>'.encode())


def test_upnp_router_talk():
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        loc = f"http://127.0.0.1:{srv.server_port}/desc.xml"
        control, service = upnp.find_service(loc)
        assert control.endswith("/ctl/IPConn") and service.endswith("WANIPConnection:1")
        router = upnp.Router(control, service)
        assert router.external_ip() == "81.2.69.160"
        router.add_mapping(8443, "192.168.0.10", 8443, "Home Camera away access")
        action, raw = FakeRouter.calls[-1]
        assert action == "AddPortMapping" and "<NewInternalClient>192.168.0.10<" in raw
        assert "<NewLeaseDuration>3600<" in raw
        FakeRouter.permanent_only = True
        router.add_mapping(8443, "192.168.0.10", 8443, "x")  # falls back to a permanent mapping
        assert "<NewLeaseDuration>0<" in FakeRouter.calls[-1][1]
        router.delete_mapping(8443)
        assert FakeRouter.calls[-1][0] == "DeletePortMapping"
    finally:
        FakeRouter.permanent_only = False
        srv.shutdown()
