"""Smart home links (lights, sirens, Alexa routines).  Run:  python -m pytest tests"""
import http.server
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from camhub import homelinks  # noqa: E402
from camhub.homelinks import ActionError, HomeLinks, event_keys, run_action  # noqa: E402
from test_alerts import MON, Clock, FakeHub, make_config  # noqa: E402


class Presence:
    def __init__(self, where="away"):
        self.where = where

    def state(self):
        return {"where": self.where}


def setup(tmp_path, rules=None, devices=None, where="away", now=MON.replace(hour=23)):
    config, path = make_config(tmp_path, {"home_links": {
        "devices": devices or [{"id": "d1", "kind": "webhook", "name": "Hook", "url": "http://x"}],
        "rules": rules or []}})
    hub = FakeHub(config)
    hub.presence = Presence(where)
    calls = []

    def runner(dev, do, target, e):
        calls.append((dev["id"], do, target, e.get("type")))
        return "ok"

    clock = Clock()
    links = HomeLinks(hub, clock=clock, local_now=lambda: now, runner=runner)
    return links, calls, clock, path


def rule(**kw):
    r = {"id": "r1", "name": "Porch", "enabled": True, "events": ["stranger"], "cameras": [],
         "when": "always", "schedule": [], "only_away": False, "cooldown_minutes": 2,
         "actions": [{"device": "d1", "do": "run", "target": "", "for_minutes": 0}]}
    r.update(kw)
    return r


def drain(links):
    while not links._jobs.empty():
        job = links._jobs.get_nowait()
        if job:
            job()


def test_event_keys():
    assert event_keys({"type": "clip_started", "person": False}) == {"motion"}
    assert event_keys({"type": "clip_started", "person": True}) == {"motion", "person"}
    assert event_keys({"type": "stranger", "stranger": True}) == {"stranger", "person"}
    assert event_keys({"type": "known_person"}) == {"person"}
    assert event_keys({"type": "vehicle", "known": False}) == {"vehicle"}
    assert event_keys({"type": "vehicle", "known": True}) == set()
    assert event_keys({"type": "parcel_taken"}) == {"parcel"}
    assert event_keys({"type": "line_crossed"}) == {"line_crossed"}
    assert event_keys({"type": "health", "state": "problem", "kind": "camera_offline"}) == {"camera_offline"}
    assert event_keys({"type": "health", "state": "ok", "kind": "camera_offline"}) == set()


def test_rule_matching_and_cooldown(tmp_path):
    links, calls, clock, _ = setup(tmp_path, [rule()])
    assert links.on_event({"type": "person", "camera": "front"}) == []
    assert links.on_event({"type": "stranger", "camera": "front"}) == ["r1"]
    drain(links)
    assert calls == [("d1", "run", "", "stranger")]
    # Within the cooldown nothing happens again.
    clock.t += 60
    assert links.on_event({"type": "stranger", "camera": "front"}) == []
    clock.t += 61
    assert links.on_event({"type": "stranger", "camera": "front"}) == ["r1"]
    drain(links)
    assert len(links.activity) == 2 and links.activity[-1]["ok"]


def test_hours_cameras_and_away(tmp_path):
    night = [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}]
    links, calls, _, _ = setup(tmp_path, [rule(when="hours", schedule=night, cameras=["back"])])
    assert links.on_event({"type": "stranger", "camera": "front"}) == []
    assert links.on_event({"type": "stranger", "camera": "back"}) == ["r1"]

    links, calls, _, _ = setup(tmp_path, [rule(when="hours", schedule=night)], now=MON.replace(hour=12))
    assert links.on_event({"type": "stranger", "camera": "front"}) == []

    links, calls, _, _ = setup(tmp_path, [rule(only_away=True)], where="home")
    assert links.on_event({"type": "stranger", "camera": "front"}) == []
    links.hub.presence.where = "away"
    assert links.on_event({"type": "stranger", "camera": "front"}) == ["r1"]

    links, calls, _, _ = setup(tmp_path, [rule(enabled=False)])
    assert links.on_event({"type": "stranger", "camera": "front"}) == []


def test_lights_switch_off_after_a_while(tmp_path):
    dev = {"id": "d1", "kind": "home_assistant", "name": "HA", "url": "http://ha", "token": "t"}
    r = rule(actions=[{"device": "d1", "do": "on", "target": "light.porch", "for_minutes": 0.002}])
    links, calls, _, _ = setup(tmp_path, [r], [dev])
    links.on_event({"type": "stranger", "camera": "front"})
    drain(links)
    time.sleep(0.4)
    drain(links)
    assert calls == [("d1", "on", "light.porch", "stranger"), ("d1", "off", "light.porch", "timer")]


def test_settings_keep_secrets_and_hide_them(tmp_path):
    dev = {"id": "d1", "kind": "home_assistant", "name": "HA", "url": "homeassistant.local:8123", "token": "SECRET"}
    links, _, _, path = setup(tmp_path, [], [dev])
    pub = links.public()
    assert pub["devices"][0]["token"] == "" and pub["devices"][0]["has_token"] is True
    # Saving from the screen (token blank) keeps the saved token.
    body = {"devices": pub["devices"], "rules": [rule(actions=[
        {"device": "d1", "do": "on", "target": "light.porch", "for_minutes": 5},
        {"device": "gone", "do": "on"}], events=["stranger", "bogus"])]}
    links.update(body)
    saved = json.loads(path.read_text())["home_links"]
    assert saved["devices"][0]["token"] == "SECRET"
    assert saved["devices"][0]["url"] == "http://homeassistant.local:8123"
    assert saved["rules"][0]["events"] == ["stranger"]
    assert len(saved["rules"][0]["actions"]) == 1
    try:
        links.update({"devices": [{"kind": "toaster"}], "rules": []})
        assert False
    except ValueError:
        pass


class Recorder(http.server.BaseHTTPRequestHandler):
    seen = []

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        Recorder.seen.append((self.command, self.path, self.headers.get("Authorization"),
                              self.rfile.read(n).decode()))
        out = b"[]" if "states" not in self.path else json.dumps(
            [{"entity_id": "light.porch", "attributes": {"friendly_name": "Porch"}},
             {"entity_id": "sensor.temp", "attributes": {}}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, *a):
        pass


def web_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Recorder.seen = []
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_home_assistant_and_webhook_calls():
    srv, base = web_server()
    try:
        ha = {"kind": "home_assistant", "url": base, "token": "abc"}
        run_action(ha, "flash", "light.porch", {"type": "stranger"})
        run_action(ha, "off", "siren.alarm", {"type": "timer"})
        assert homelinks.list_targets(ha) == [{"id": "light.porch", "name": "Porch"}]
        hook = {"kind": "webhook", "url": base + "/hook?cam={camera}", "method": "POST", "body": ""}
        run_action(hook, "run", "", {"type": "stranger", "camera": "front", "camera_name": "Front"})
        seen = Recorder.seen
        assert seen[0][:3] == ("POST", "/api/services/light/turn_on", "Bearer abc")
        assert json.loads(seen[0][3]) == {"entity_id": "light.porch", "flash": "long"}
        assert seen[1][1] == "/api/services/siren/turn_off"
        assert seen[3][1] == "/hook?cam=Front"
        assert json.loads(seen[3][3])["event"] == "Someone it doesn't recognise"
    finally:
        srv.shutdown()
    try:
        run_action({"kind": "home_assistant", "url": base, "token": "x"}, "on", "light.porch", {})
        assert False
    except ActionError as e:
        assert "Couldn't reach" in str(e)


def test_mqtt_publish():
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    got = {}

    def broker():
        c, _ = lsock.accept()
        connect = c.recv(1024)
        got["connect"] = connect
        c.sendall(b"\x20\x02\x00\x00")
        data = b""
        while not data.endswith(b"\xe0\x00"):
            chunk = c.recv(1024)
            if not chunk:
                break
            data += chunk
        got["publish"] = data
        c.close()

    t = threading.Thread(target=broker, daemon=True)
    t.start()
    dev = {"kind": "mqtt", "host": "127.0.0.1", "port": lsock.getsockname()[1], "username": "u",
           "password": "p", "topic": "homecamera/alert"}
    assert run_action(dev, "on", "garden/siren", {"type": "stranger"}) == "MQTT garden/siren"
    t.join(2)
    lsock.close()
    assert got["connect"][0] == 0x10 and b"MQTT" in got["connect"] and got["connect"].endswith(b"\x00\x01u\x00\x01p")
    assert got["publish"][0] == 0x30 and b"garden/siren" in got["publish"] and b"ON" in got["publish"]


def test_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    links, _, _, path = setup(tmp_path, [rule()])
    hub = links.hub
    hub.home_links = links

    class Det:
        available = False

    hub.detector = Det()
    client = TestClient(create_app(hub))
    assert client.get("/api/homelinks").status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200
    r = client.get("/api/homelinks").json()
    assert r["rules"][0]["id"] == "r1" and any(e["id"] == "stranger" for e in r["events"])
    r = client.put("/api/homelinks", json={"devices": r["devices"], "rules": []})
    assert r.status_code == 200 and r.json()["rules"] == []
    assert client.post("/api/homelinks/rules/nope/try").status_code == 404
    r = client.post("/api/homelinks/try", json={"device": {"kind": "alexa", "url": ""}, "do": "run"})
    assert r.json()["ok"] is False
