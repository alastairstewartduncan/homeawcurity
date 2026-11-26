"""Home/away mode and health alerts.  Run:  python -m pytest tests"""
import datetime
import json
import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.health import GB, HealthMonitor  # noqa: E402
from camhub.presence import Presence, clean_schedule, in_schedule  # noqa: E402
from camhub.storage import Storage  # noqa: E402

MON = datetime.datetime(2026, 11, 23, 12, 0)  # a Monday


def make_config(tmp_path, extra=None):
    p = tmp_path / "config.json"
    data = {"server": {"password_hash": auth.hash_password("pw123456")},
            "storage": {"path": str(tmp_path / "rec"), "min_free_gb": 20},
            "cameras": [{"id": "front", "name": "Front"}]}
    data.update(extra or {})
    p.write_text(json.dumps(data))
    return Config(str(p)), p


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_schedule_overnight_and_days():
    rows = clean_schedule([{"days": [0], "start": "23:00", "end": "06:30"},
                           {"days": [9], "start": "01:00", "end": "02:00"},   # bad day: dropped
                           {"days": [1], "start": "25:00", "end": "02:00"}])  # bad time: dropped
    assert rows == [{"days": [0], "start": "23:00", "end": "06:30"}]
    assert not in_schedule(rows, MON.replace(hour=22, minute=59))
    assert in_schedule(rows, MON.replace(hour=23, minute=0))
    # Early Tuesday morning still belongs to Monday night.
    assert in_schedule(rows, (MON + datetime.timedelta(days=1)).replace(hour=6, minute=29))
    assert not in_schedule(rows, (MON + datetime.timedelta(days=1)).replace(hour=6, minute=30))
    # Early Monday morning belongs to Sunday night, which isn't in the schedule.
    assert not in_schedule(rows, MON.replace(hour=3))
    day = [{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "17:00"}]
    assert in_schedule(day, MON.replace(hour=9))
    assert not in_schedule(day, MON.replace(hour=17))
    assert not in_schedule(day, MON + datetime.timedelta(days=5))  # Saturday


def test_home_and_away_from_phones(tmp_path):
    config, path = make_config(tmp_path)
    bus = EventBus()
    clock = Clock()
    pr = Presence(config, bus, clock=clock, local_now=lambda: MON)
    bus.enrich = pr.tag_event
    q = bus.subscribe()

    # No phones yet: treated as away, so alerts stay on as before.
    st = pr.state()
    assert st["where"] == "away" and st["alerts"] == "all"
    bus.publish({"type": "clip_started", "person": False})
    assert q.get_nowait()["alert"] is True

    pr.report("phone-a", "Alastair's phone", True)
    st = pr.state()
    assert st["where"] == "home" and st["alerts"] == "none"
    assert "is home" in st["reason"]
    assert json.loads(path.read_text())["presence"]["phones"]["phone-a"]["home"] is True

    # A presence event goes out when the state changes.
    kinds = [q.get_nowait() for _ in range(q.qsize())]
    assert any(e["type"] == "presence" and e["where"] == "home" for e in kinds)

    bus.publish({"type": "clip_started", "person": True})
    assert q.get_nowait()["alert"] is False
    # Other events are left alone.
    bus.publish({"type": "clip_saved"})
    assert "alert" not in q.get_nowait()

    # People-only while home.
    pr.update({"home_alerts": "people"})
    assert pr.should_alert({"type": "person"})
    assert pr.should_alert({"type": "clip_started", "person": True})
    assert not pr.should_alert({"type": "clip_started", "person": False})

    # A second phone that's out doesn't change anything while one is home.
    pr.report("phone-b", "Sam", False)
    assert pr.state()["where"] == "home"
    pr.report("phone-a", "Alastair's phone", False)
    st = pr.state()
    assert st["where"] == "away" and st["reason"] == "Everyone is out" and st["alerts"] == "all"

    # A phone that hasn't checked in for a long time stops counting.
    pr.report("phone-a", "Alastair's phone", True)
    assert pr.state()["where"] == "home"
    clock.t += 13 * 3600
    st = pr.state()
    assert st["where"] == "away" and "checked in" in st["reason"]
    assert all(p["stale"] for p in st["phones"])


def test_manual_mode_and_alert_hours(tmp_path):
    config, _ = make_config(tmp_path)
    now = {"t": MON.replace(hour=12)}
    pr = Presence(config, EventBus(), clock=Clock(), local_now=lambda: now["t"])
    pr.report("phone-a", "Phone", True)
    assert pr.state()["alerts"] == "none"

    # Alert hours switch alerts on even with someone home.
    pr.update({"schedule": [{"days": list(range(7)), "start": "23:00", "end": "06:00"}]})
    now["t"] = MON.replace(hour=23, minute=30)
    st = pr.state()
    assert st["where"] == "home" and st["alerts"] == "all" and st["alert_hours"]
    now["t"] = MON.replace(hour=12)

    # Set by hand: stays until a phone arrives or leaves.
    pr.update({"mode": "away"})
    assert pr.state()["where"] == "away"
    pr.report("phone-a", "Phone", True)       # same as before: no change, override stays
    assert pr.state()["mode"] == "away"
    pr.report("phone-a", "Phone", False)      # left home: back to automatic
    st = pr.state()
    assert st["mode"] == "auto" and st["where"] == "away"

    pr.update({"mode": "home"})
    assert pr.state()["alerts"] == "none"
    assert pr.forget_phone("phone-a")
    assert pr.state()["phones"] == []

    try:
        pr.update({"mode": "holiday"})
        assert False, "should refuse"
    except ValueError:
        pass


def test_event_numbers_keep_rising_after_restart():
    a = EventBus()
    a.publish({"type": "x"})
    first = a.recent[-1]["seq"]
    b = EventBus()  # as if the PC restarted
    b.publish({"type": "x"})
    assert b.recent[-1]["seq"] >= first
    assert b.since(first - 1)


class FakeCam:
    def __init__(self, cfg):
        self.cfg = cfg
        self.online = True
        self.why = None

    def status(self):
        return {"id": self.cfg["id"], "name": self.cfg["name"], "online": self.online, "problem": self.why}


class FakeHub:
    def __init__(self, config):
        self.config = config
        self.storage = Storage(config)
        self.events = EventBus()
        self.cameras = {c["id"]: FakeCam(c) for c in config.cameras}


def health_events(q):
    out = []
    while not q.empty():
        e = q.get_nowait()
        if e["type"] == "health":
            out.append(e)
    return out


def test_camera_offline_alert_and_back(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    clock = Clock(0)
    h = HealthMonitor(hub, clock=clock)
    q = hub.events.subscribe()
    cam = hub.cameras["front"]

    h.check_cameras()
    assert not health_events(q)
    cam.online, cam.why = False, "Wrong password."
    h.check_cameras()
    clock.t += 4 * 60
    h.check_cameras()
    assert not health_events(q), "not yet: under 5 minutes"
    clock.t += 61
    h.check_cameras()
    ev = health_events(q)
    assert len(ev) == 1 and ev[0]["state"] == "problem" and ev[0]["kind"] == "camera_offline"
    assert "Front is offline" == ev[0]["title"] and "Wrong password." in ev[0]["message"]
    assert h.status()[0]["key"] == "offline:front"
    clock.t += 600
    h.check_cameras()
    assert not health_events(q), "only one alert per problem"
    assert "15 minutes" in h.status()[0]["message"]

    cam.online = True
    h.check_cameras()
    ev = health_events(q)
    assert len(ev) == 1 and ev[0]["state"] == "ok" and "back online" in ev[0]["title"]
    assert h.status() == []

    # A short blip doesn't alert at all.
    cam.online = False
    h.check_cameras()
    cam.online = True
    clock.t += 60
    h.check_cameras()
    assert not health_events(q)

    # Turned off.
    h.update({"camera_offline_minutes": 0})
    cam.online = False
    clock.t += 3600
    h.check_cameras()
    assert not health_events(q)


def test_disk_alerts(tmp_path, monkeypatch):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    h = HealthMonitor(hub, clock=Clock(0))
    q = hub.events.subscribe()
    Usage = namedtuple("Usage", "total used free")
    free = {"b": 100 * GB}
    monkeypatch.setattr("camhub.health.shutil.disk_usage", lambda p: Usage(500 * GB, 0, free["b"]))

    h.check_disk()
    assert not health_events(q)
    free["b"] = 9 * GB  # below half of the 20 GB kept free
    h.check_disk()
    ev = health_events(q)
    assert ev[0]["kind"] == "disk_low" and "9.0 GB" in ev[0]["message"]
    free["b"] = 10.5 * GB  # back over the line, but not by enough to call it fixed
    h.check_disk()
    assert not health_events(q)
    free["b"] = 15 * GB
    h.check_disk()
    assert health_events(q)[0]["state"] == "ok"

    # Recordings folder gone (unplugged USB drive).
    monkeypatch.setattr("camhub.health.os.path.isdir", lambda p: False)
    h.check_disk()
    ev = health_events(q)
    assert ev[0]["kind"] == "disk_folder"


def test_recording_failed_alert(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    h = HealthMonitor(hub, clock=Clock(0))
    q = hub.events.subscribe()
    h.on_event({"type": "clip_failed", "camera": "front"})
    h.on_event({"type": "clip_failed", "camera": "front"})
    ev = health_events(q)
    assert len(ev) == 1 and ev[0]["kind"] == "recording_failed" and "Front" in ev[0]["title"]
    h.on_event({"type": "clip_saved", "camera": "front"})
    assert health_events(q)[0]["state"] == "ok"
    h.update({"recording_alerts": False})
    h.on_event({"type": "clip_failed", "camera": "front"})
    assert not health_events(q)


def test_alert_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    config, path = make_config(tmp_path)
    hub = FakeHub(config)
    hub.presence = Presence(config, hub.events)
    hub.events.enrich = hub.presence.tag_event
    hub.health = HealthMonitor(hub)

    class Det:
        available = False

    hub.detector = Det()
    client = TestClient(create_app(hub))
    assert client.post("/api/presence/report", json={"phone": "x", "home": True}).status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200

    r = client.post("/api/presence/report", json={"phone": "abc-123", "name": "Pixel", "home": True})
    assert r.status_code == 200 and r.json()["where"] == "home"
    assert client.post("/api/presence/report", json={"name": "No id"}).status_code == 400
    r = client.put("/api/presence", json={"mode": "away", "home_alerts": "people",
                                          "schedule": [{"days": [5, 6], "start": "22:00", "end": "07:00"}]})
    assert r.status_code == 200 and r.json()["where"] == "away"
    assert client.put("/api/presence", json={"mode": "nope"}).status_code == 400
    saved = json.loads(path.read_text())["presence"]
    assert saved["mode"] == "away" and saved["schedule"][0]["days"] == [5, 6]

    st = client.get("/api/status").json()
    assert st["presence"]["where"] == "away" and st["health"] == []

    r = client.put("/api/health", json={"camera_offline_minutes": "10", "disk_alerts": False})
    assert r.status_code == 200 and r.json()["settings"]["camera_offline_minutes"] == 10
    assert json.loads(path.read_text())["health"]["disk_alerts"] is False
    assert client.delete("/api/presence/phones/abc-123").status_code == 200
    assert client.delete("/api/presence/phones/abc-123").status_code == 404
