"""Smart home links: switch on lights, sound a siren or start an Alexa routine when the camera
sees something.

Devices (config.json, "home_links" > "devices"), each {"id", "name", "kind", ...}:
  home_assistant  url, token               turn Home Assistant lights, switches, sirens, scenes on/off
  hue             host, username           Philips Hue lights and rooms, paired with the bridge button
  alexa           url                      a "trigger link" that starts an Alexa routine
                                           (made with Voice Monkey, IFTTT or similar)
  webhook         url, method, body        any web address, for other systems
  mqtt            host, port, username, password, topic

Rules ("home_links" > "rules"), each:
  {"id", "name", "enabled",
   "events": ["person", "stranger", ...],    what sets it off (see EVENTS)
   "cameras": [],                            which cameras (empty means all)
   "when": "always" | "hours",
   "schedule": [{"days", "start", "end"}],   when "hours": the times it is active
   "only_away": false,                       only when nobody is home (home/away mode)
   "cooldown_minutes": 2,                    don't repeat within this time
   "actions": [{"device", "do", "target", "for_minutes"}]}
     do: on, off, flash, run (Alexa, webhook), send (MQTT)
     for_minutes: switch back off after this long (0 leaves it on)
"""
import collections
import datetime
import json
import logging
import queue
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .presence import clean_schedule, in_schedule

log = logging.getLogger("camhub.homelinks")

EVENTS = [
    ("stranger", "Someone it doesn't recognise"),
    ("person", "Any person"),
    ("motion", "Any movement"),
    ("vehicle", "An unknown car"),
    ("parcel", "A parcel arrives or is taken"),
    ("loitering", "Someone hanging around"),
    ("line_crossed", "Something crosses a line you drew"),
    ("ai_alert", "An AI agent raises an alert"),
    ("camera_offline", "A camera goes offline"),
]
EVENT_KEYS = [k for k, _ in EVENTS]

KINDS = {
    "home_assistant": {"name": "Home Assistant", "secret": "token", "actions": ["on", "off", "flash"]},
    "hue": {"name": "Philips Hue", "secret": "username", "actions": ["on", "off", "flash"]},
    "alexa": {"name": "Alexa routine", "secret": "url", "actions": ["run"]},
    "webhook": {"name": "Web address (webhook)", "secret": None, "actions": ["run"]},
    "mqtt": {"name": "MQTT", "secret": "password", "actions": ["send", "on", "off"]},
}
FIELDS = {
    "home_assistant": ["url", "token"],
    "hue": ["host", "username"],
    "alexa": ["url"],
    "webhook": ["url", "method", "body"],
    "mqtt": ["host", "port", "username", "password", "topic"],
}

DEFAULT_RULE = {
    "name": "Lights on for strangers at night",
    "enabled": True,
    "events": ["stranger"],
    "cameras": [],
    "when": "hours",
    "schedule": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "17:00", "end": "07:00"}],
    "only_away": False,
    "cooldown_minutes": 2,
    "actions": [],
}

TIMEOUT = 8


def event_keys(e):
    """What kind of thing an event is, in the words rules use."""
    t = e.get("type") or ""
    keys = set()
    if t == "clip_started":
        keys.add("motion")
        if e.get("person"):
            keys.add("person")
    if t in ("person", "known_person"):
        keys.add("person")
    if t in ("stranger", "unknown_face", "unknown_person") or e.get("stranger") is True:
        keys.update(("stranger", "person"))
    if t in ("unknown_vehicle", "unknown_car") or (t in ("vehicle", "car") and e.get("known") is False):
        keys.add("vehicle")
    if t.startswith("parcel"):
        keys.add("parcel")
    if t == "loitering":
        keys.add("loitering")
    if t == "line_crossed":
        keys.add("line_crossed")
    if t == "ai_alert":
        keys.add("ai_alert")
    if t == "health" and e.get("state") == "problem" and e.get("kind") == "camera_offline":
        keys.add("camera_offline")
    return keys


def describe(e):
    keys = event_keys(e)
    for k, label in EVENTS:
        if k in keys:
            return label
    return e.get("type") or "Test"


# ---------- talking to devices ----------------------------------------------------------

class ActionError(Exception):
    pass


def _http(method, url, body=None, headers=None, insecure=False):
    data = None
    headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode()
            headers.setdefault("Content-Type", "text/plain")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = None
    if insecure and url.lower().startswith("https"):
        # Hue bridges use their own certificate, which nothing else vouches for.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read()
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise ActionError("It refused the login. Check the token or link.")
        if err.code == 404:
            raise ActionError("It said that address or device doesn't exist (404).")
        raise ActionError(f"It answered with an error ({err.code}).")
    except (urllib.error.URLError, socket.timeout, OSError) as err:
        reason = getattr(err, "reason", err)
        raise ActionError(f"Couldn't reach it ({reason}). Check the address and that it's switched on.")
    try:
        return json.loads(raw.decode() or "null")
    except ValueError:
        return raw.decode(errors="replace")


def _fill(text, e):
    """Puts details of the event into a webhook body: {camera}, {event}, {time}."""
    vals = {"camera": e.get("camera_name") or e.get("camera") or "", "event": describe(e),
            "time": e.get("time") or datetime.datetime.now().isoformat(timespec="seconds"),
            "type": e.get("type") or ""}
    out = str(text)
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def ha_call(dev, do, target):
    base = str(dev.get("url") or "").rstrip("/")
    if not base:
        raise ActionError("Add the Home Assistant address first.")
    if not target or "." not in target:
        raise ActionError("Choose which Home Assistant device to use.")
    domain = target.split(".", 1)[0]
    data = {"entity_id": target}
    if domain in ("scene", "script", "automation", "button") and do != "off":
        service = "trigger" if domain == "automation" else "press" if domain == "button" else "turn_on"
    elif do == "off":
        service = "turn_off"
    else:
        service = "turn_on"
        if do == "flash" and domain == "light":
            data["flash"] = "long"
    if domain in ("scene", "script") and do == "off":
        return "Scenes and scripts can't be switched off"
    hdr = {"Authorization": "Bearer " + str(dev.get("token") or "")}
    _http("POST", f"{base}/api/services/{domain}/{service}", data, hdr)
    return f"{target}: {service.replace('_', ' ')}"


def ha_targets(dev):
    base = str(dev.get("url") or "").rstrip("/")
    hdr = {"Authorization": "Bearer " + str(dev.get("token") or "")}
    states = _http("GET", f"{base}/api/states", headers=hdr)
    keep = ("light", "switch", "siren", "scene", "script", "input_boolean", "automation", "button", "fan")
    out = []
    for s in states if isinstance(states, list) else []:
        eid = s.get("entity_id") or ""
        if eid.split(".", 1)[0] in keep:
            out.append({"id": eid, "name": (s.get("attributes") or {}).get("friendly_name") or eid})
    return sorted(out, key=lambda t: (t["id"].split(".")[0], t["name"].lower()))


def _hue_base(dev):
    host = str(dev.get("host") or "").strip()
    if not host:
        raise ActionError("Add the Hue Bridge address first.")
    if not dev.get("username"):
        raise ActionError("Pair with the Hue Bridge first.")
    return f"https://{host}/api/{dev['username']}"


def _hue_errors(res):
    if isinstance(res, list):
        for r in res:
            if isinstance(r, dict) and "error" in r:
                raise ActionError("Hue Bridge: " + str(r["error"].get("description", "error")))


def hue_call(dev, do, target):
    base = _hue_base(dev)
    kind, _, num = str(target or "").partition(":")
    if kind not in ("light", "group") or not num:
        raise ActionError("Choose which Hue light or room to use.")
    path = f"{base}/lights/{num}/state" if kind == "light" else f"{base}/groups/{num}/action"
    if do == "off":
        state = {"on": False}
    elif do == "flash":
        state = {"on": True, "alert": "lselect"}   # flashes for about 15 seconds
    else:
        state = {"on": True, "bri": 254}
    _hue_errors(_http("PUT", path, state, insecure=True))
    return f"Hue {kind} {num}: {do}"


def hue_targets(dev):
    base = _hue_base(dev)
    out = []
    groups = _http("GET", base + "/groups", insecure=True)
    _hue_errors(groups)
    for gid, g in (groups or {}).items() if isinstance(groups, dict) else []:
        if g.get("type") in ("Room", "Zone", "LightGroup"):
            out.append({"id": f"group:{gid}", "name": f"{g.get('name')} (room)"})
    lights = _http("GET", base + "/lights", insecure=True)
    for lid, li in (lights or {}).items() if isinstance(lights, dict) else []:
        out.append({"id": f"light:{lid}", "name": li.get("name") or f"Light {lid}"})
    return out


def hue_pair(host):
    """Asks the bridge for a login. Works for 30 seconds after its round button is pressed."""
    host = str(host or "").strip()
    if not host:
        raise ActionError("Enter the Hue Bridge address.")
    res = _http("POST", f"https://{host}/api", {"devicetype": "homecamera#pc"}, insecure=True)
    for r in res if isinstance(res, list) else []:
        if "success" in r:
            return r["success"]["username"]
        if "error" in r and r["error"].get("type") == 101:
            raise ActionError("Press the round button on top of the Hue Bridge, then press Pair again "
                              "within 30 seconds.")
    raise ActionError("The Hue Bridge didn't give a login. Check the address.")


def hue_find():
    res = _http("GET", "https://discovery.meethue.com/")
    return [r.get("internalipaddress") for r in res if isinstance(r, dict) and r.get("internalipaddress")] \
        if isinstance(res, list) else []


def _mqtt_str(s):
    b = str(s).encode()
    return struct.pack("!H", len(b)) + b


def _mqtt_len(n):
    out = b""
    while True:
        byte, n = n % 128, n // 128
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def mqtt_publish(dev, topic, payload):
    """Sends one MQTT message (version 3.1.1, no extra libraries needed)."""
    host = str(dev.get("host") or "").strip()
    if not host:
        raise ActionError("Add the MQTT broker address first.")
    port = int(dev.get("port") or 1883)
    user, pw = str(dev.get("username") or ""), str(dev.get("password") or "")
    flags = 0x02 | (0x80 if user else 0) | (0x40 if user and pw else 0)
    body = _mqtt_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 30) + \
        _mqtt_str("homecamera-" + secrets.token_hex(3))
    if user:
        body += _mqtt_str(user)
        if pw:
            body += _mqtt_str(pw)
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as s:
            s.sendall(b"\x10" + _mqtt_len(len(body)) + body)
            ack = s.recv(4)
            if len(ack) < 4 or ack[0] != 0x20:
                raise ActionError("The MQTT broker didn't answer properly.")
            if ack[3] != 0:
                raise ActionError("The MQTT broker refused the login." if ack[3] in (4, 5)
                                  else f"The MQTT broker refused the connection ({ack[3]}).")
            pub = _mqtt_str(topic) + payload.encode()
            s.sendall(b"\x30" + _mqtt_len(len(pub)) + pub + b"\xe0\x00")
    except OSError as err:
        raise ActionError(f"Couldn't reach the MQTT broker ({err}).")
    return f"MQTT {topic}"


def run_action(dev, do, target, event):
    """Carries out one action. Returns a short description, or raises ActionError."""
    kind = dev.get("kind")
    if kind == "home_assistant":
        return ha_call(dev, do, target)
    if kind == "hue":
        return hue_call(dev, do, target)
    if kind == "alexa":
        url = str(dev.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ActionError("Paste the routine's trigger link first.")
        _http("GET", url)
        return "Alexa routine started"
    if kind == "webhook":
        url = _fill(str(dev.get("url") or "").strip(), event)
        if not url.startswith(("http://", "https://")):
            raise ActionError("Enter a web address starting with http:// or https://.")
        method = "GET" if str(dev.get("method") or "POST").upper() == "GET" else "POST"
        body = None
        if method == "POST":
            body = _fill(dev["body"], event) if dev.get("body") else {
                "event": describe(event), "type": event.get("type"), "camera": event.get("camera"),
                "camera_name": event.get("camera_name"), "time": event.get("time"),
                "clip": event.get("clip")}
        _http(method, url, body)
        return "Web address called"
    if kind == "mqtt":
        topic = str(target or dev.get("topic") or "homecamera/alert")
        if do in ("on", "off"):
            payload = do.upper()
        else:
            payload = json.dumps({"event": describe(event), "type": event.get("type"),
                                  "camera": event.get("camera"), "camera_name": event.get("camera_name"),
                                  "time": event.get("time")})
        return mqtt_publish(dev, topic, payload)
    raise ActionError("Unknown kind of device.")


def list_targets(dev):
    if dev.get("kind") == "home_assistant":
        return ha_targets(dev)
    if dev.get("kind") == "hue":
        return hue_targets(dev)
    return []


# ---------- settings ----------------------------------------------------------------------

def _clean_device(d, old=None):
    kind = d.get("kind")
    if kind not in KINDS:
        raise ValueError("Unknown kind of smart home device")
    out = {"id": str(d.get("id") or "") or "d" + secrets.token_hex(4), "kind": kind,
           "name": str(d.get("name") or KINDS[kind]["name"]).strip()[:60]}
    for f in FIELDS[kind]:
        v = d.get(f)
        if f == "port":
            try:
                v = int(v or 1883)
            except (TypeError, ValueError):
                raise ValueError("The MQTT port must be a number")
        else:
            v = str(v or "").strip()[:2000]
        out[f] = v
    secret = KINDS[kind]["secret"]
    if secret and not out.get(secret) and old and old.get("kind") == kind and not d.get("clear_" + secret):
        out[secret] = old.get(secret, "")
    if kind == "home_assistant" and out["url"] and "://" not in out["url"]:
        out["url"] = "http://" + out["url"]
    return out


def _clean_rule(r, device_ids):
    events = [e for e in (r.get("events") or []) if e in EVENT_KEYS]
    actions = []
    for a in r.get("actions") or []:
        if a.get("device") not in device_ids:
            continue
        do = a.get("do") if a.get("do") in ("on", "off", "flash", "run", "send") else "on"
        try:
            mins = max(0.0, min(720.0, float(a.get("for_minutes") or 0)))
        except (TypeError, ValueError):
            mins = 0.0
        actions.append({"device": a["device"], "do": do, "target": str(a.get("target") or "")[:200],
                        "for_minutes": mins})
    try:
        cooldown = max(0.0, min(240.0, float(r.get("cooldown_minutes", 2))))
    except (TypeError, ValueError):
        cooldown = 2.0
    return {"id": str(r.get("id") or "") or "r" + secrets.token_hex(4),
            "name": str(r.get("name") or "Rule").strip()[:80],
            "enabled": bool(r.get("enabled", True)),
            "events": events,
            "cameras": [str(c) for c in (r.get("cameras") or [])],
            "when": "hours" if r.get("when") == "hours" else "always",
            "schedule": clean_schedule(r.get("schedule") or []),
            "only_away": bool(r.get("only_away")),
            "cooldown_minutes": cooldown,
            "actions": actions}


class HomeLinks:
    def __init__(self, hub, clock=time.monotonic, local_now=datetime.datetime.now, runner=run_action):
        self.hub = hub
        self.config = hub.config
        self.clock = clock
        self.local_now = local_now
        self.runner = runner
        cur = self.config.data.get("home_links") or {}
        self.config.data["home_links"] = {"devices": list(cur.get("devices") or []),
                                         "rules": list(cur.get("rules") or [])}
        self._lock = threading.RLock()
        self._last_fired = {}          # rule id -> clock time
        self._off_timers = {}          # (device id, target) -> Timer
        self.activity = collections.deque(maxlen=40)
        self._events = None
        self._stop = threading.Event()
        self._jobs = queue.Queue()

    @property
    def data(self):
        return self.config.data["home_links"]

    def device(self, dev_id):
        return next((d for d in self.data["devices"] if d.get("id") == dev_id), None)

    # ---- settings ----

    def public(self):
        """Settings for the viewer, with passwords and tokens left out."""
        devs = []
        for d in self.data["devices"]:
            d = dict(d)
            secret = KINDS.get(d.get("kind"), {}).get("secret")
            if secret:
                d["has_" + secret] = bool(d.get(secret))
                d[secret] = ""
            devs.append(d)
        return {"devices": devs, "rules": self.data["rules"],
                "kinds": {k: {"name": v["name"], "actions": v["actions"]} for k, v in KINDS.items()},
                "events": [{"id": k, "name": n} for k, n in EVENTS],
                "default_rule": DEFAULT_RULE, "activity": list(self.activity)[::-1]}

    def update(self, body):
        if not isinstance(body.get("devices", []), list) or not isinstance(body.get("rules", []), list):
            raise ValueError("Expected lists of devices and rules")
        with self._lock:
            old = {d["id"]: d for d in self.data["devices"]}
            devices = [_clean_device(d, old.get(d.get("id"))) for d in body.get("devices", [])
                       if isinstance(d, dict)]
            ids = {d["id"] for d in devices}
            rules = [_clean_rule(r, ids) for r in body.get("rules", []) if isinstance(r, dict)]
            self.data["devices"] = devices
            self.data["rules"] = rules
            self.config.save()
        return self.public()

    def with_saved_secret(self, d):
        """A device from the settings screen, which may not be saved yet."""
        old = self.device(d.get("id"))
        return _clean_device(d, old)

    # ---- running ----

    def start(self):
        self._events = self.hub.events.subscribe()
        threading.Thread(target=self._listen, name="homelinks", daemon=True).start()
        threading.Thread(target=self._work, name="smarthome-actions", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._jobs.put(None)
        if self._events is not None:
            self.hub.events.unsubscribe(self._events)
        with self._lock:
            for t in self._off_timers.values():
                t.cancel()

    def _listen(self):
        while not self._stop.is_set():
            try:
                e = self._events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.on_event(e)
            except Exception:
                log.exception("Smart home rule check failed")

    def _work(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                log.exception("Smart home action failed")

    def is_away(self):
        presence = getattr(self.hub, "presence", None)
        if presence is None:
            return True
        try:
            return presence.state().get("where") != "home"
        except Exception:
            return True

    def matches(self, rule, e):
        if not rule.get("enabled") or not rule.get("actions"):
            return False
        if not set(rule.get("events") or []) & event_keys(e):
            return False
        cams = rule.get("cameras") or []
        if cams and e.get("camera") not in cams:
            return False
        if rule.get("when") == "hours" and not in_schedule(rule.get("schedule") or [], self.local_now()):
            return False
        if rule.get("only_away") and not self.is_away():
            return False
        return True

    def on_event(self, e):
        """Checks an event against every rule, and queues the actions of those that match."""
        if e.get("type") == "home_links":
            return []
        fired = []
        now = self.clock()
        with self._lock:
            rules = [dict(r) for r in self.data["rules"]]
        for rule in rules:
            if not self.matches(rule, e):
                continue
            with self._lock:
                last = self._last_fired.get(rule["id"])
                cooling = last is not None and now - last < float(rule.get("cooldown_minutes") or 0) * 60
                if not cooling:
                    self._last_fired[rule["id"]] = now
            if cooling:
                # Still going on: keep lights on for longer, but don't switch them on again.
                for a in rule["actions"]:
                    if a.get("for_minutes") and a["do"] in ("on", "flash"):
                        self._schedule_off(a, rule)
                continue
            fired.append(rule["id"])
            self._jobs.put(lambda r=rule, ev=dict(e): self.run_rule(r, ev))
        return fired

    def run_rule(self, rule, e):
        results = []
        for a in rule.get("actions") or []:
            results.append(self._do(a, e, rule.get("name")))
            if a.get("for_minutes") and a["do"] in ("on", "flash"):
                self._schedule_off(a, rule)
        return results

    def _do(self, a, e, rule_name):
        dev = self.device(a.get("device"))
        rec = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "rule": rule_name,
               "device": dev.get("name") if dev else "(removed)", "do": a.get("do"),
               "event": describe(e), "ok": False}
        try:
            if dev is None:
                raise ActionError("That device has been removed.")
            rec["message"] = self.runner(dev, a.get("do"), a.get("target"), e)
            rec["ok"] = True
            log.info("Smart home: %s -> %s %s (%s)", rule_name, rec["device"], a.get("do"), rec["message"])
        except ActionError as err:
            rec["message"] = str(err)
            log.warning("Smart home: %s -> %s failed: %s", rule_name, rec["device"], err)
        except Exception as err:
            rec["message"] = f"Something went wrong ({err})"
            log.exception("Smart home action failed")
        self.activity.append(rec)
        return rec

    def _schedule_off(self, a, rule):
        dev = self.device(a.get("device"))
        if dev is None or "off" not in KINDS.get(dev.get("kind"), {}).get("actions", []):
            return
        key = (a["device"], a.get("target") or "")
        off = dict(a, do="off")

        def fire():
            with self._lock:
                if self._off_timers.get(key) is t:
                    self._off_timers.pop(key, None)
            self._jobs.put(lambda: self._do(off, {"type": "timer"}, (rule.get("name") or "") + " (timer)"))

        t = threading.Timer(float(a["for_minutes"]) * 60, fire)
        t.daemon = True
        with self._lock:
            old = self._off_timers.pop(key, None)
            if old:
                old.cancel()
            self._off_timers[key] = t
        if not self._stop.is_set():
            t.start()

    def test_rule(self, rule_id):
        rule = next((r for r in self.data["rules"] if r["id"] == rule_id), None)
        if rule is None:
            return None
        return self.run_rule(rule, {"type": "test", "camera_name": "Test"})


# ---------- web routes ----------------------------------------------------------------------

def add_home_link_routes(app, hub):
    import asyncio

    from fastapi import HTTPException, Request

    links = hub.home_links

    async def body_of(request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        return body

    @app.get("/api/homelinks")
    def get_links():
        return links.public()

    @app.put("/api/homelinks")
    async def put_links(request: Request):
        try:
            return links.update(await body_of(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/homelinks/try")
    async def try_action(request: Request):
        """Tries one action straight away, with a device that may not be saved yet."""
        body = await body_of(request)
        try:
            dev = links.with_saved_secret(body.get("device") or {})
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            msg = await asyncio.to_thread(run_action, dev, body.get("do") or "on", body.get("target"),
                                          {"type": "test", "camera_name": "Test"})
            return {"ok": True, "message": msg}
        except ActionError as e:
            return {"ok": False, "message": str(e)}

    @app.post("/api/homelinks/targets")
    async def targets(request: Request):
        """Lists the lights and switches a device has, for choosing from."""
        body = await body_of(request)
        try:
            dev = links.with_saved_secret(body.get("device") or {})
            return {"ok": True, "targets": await asyncio.to_thread(list_targets, dev)}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except ActionError as e:
            return {"ok": False, "message": str(e), "targets": []}

    @app.post("/api/homelinks/rules/{rule_id}/try")
    async def try_rule(rule_id: str):
        res = await asyncio.to_thread(links.test_rule, rule_id)
        if res is None:
            raise HTTPException(404, "Save the rule first")
        return {"results": res}

    @app.post("/api/homelinks/hue/pair")
    async def pair_hue(request: Request):
        body = await body_of(request)
        try:
            return {"ok": True, "username": await asyncio.to_thread(hue_pair, body.get("host"))}
        except ActionError as e:
            return {"ok": False, "message": str(e)}

    @app.get("/api/homelinks/hue/find")
    async def find_hue():
        try:
            return {"bridges": await asyncio.to_thread(hue_find)}
        except ActionError:
            return {"bridges": []}
