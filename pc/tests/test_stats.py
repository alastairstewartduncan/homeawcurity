"""Stats: activity counts, visitors, hot spots, system health and the weekly report.
Run:  python -m pytest tests"""
import datetime
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth, stats as statsmod  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.storage import Storage  # noqa: E402


def make_config(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"server": {"password_hash": auth.hash_password("pw123456")},
                             "storage": {"path": str(tmp_path / "rec"), "retention_days": 30, "min_free_gb": 0},
                             "cameras": [{"id": "front", "name": "Front"}]}))
    return Config(str(p)), p


class FakeCam:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]
        self.online = True
        self._frame_id = 0
        self.ptz = None

    def status(self):
        return {"id": self.id, "name": self.cfg["name"], "online": self.online}


class FakeHub:
    def __init__(self, config):
        self.config = config
        self.storage = Storage(config)
        self.events = EventBus()
        self.cameras = {c["id"]: FakeCam(c) for c in config.cameras}
        self.stats = statsmod.Stats(self, threaded=False)


def ev(kind, when, **kw):
    return {"type": kind, "camera": "front", "time": when.isoformat(timespec="seconds"), **kw}


def test_counts_heatmap_and_visitors(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    st = hub.stats
    today = datetime.date.today()
    sat = today - datetime.timedelta(days=(today.weekday() - 5) % 7)     # the last Saturday (or today)
    at = datetime.datetime.combine(sat, datetime.time(14, 10))
    st.on_event(ev("clip_started", at, person=True, trigger="person"))
    st.on_event(ev("clip_started", at, trigger="continued"))             # not a new recording
    st.on_event(ev("known_person", at, person_name="Ann"))
    st.on_event(ev("stranger", at))
    st.on_event(ev("stranger", at + datetime.timedelta(minutes=5)))
    st.on_event(ev("parcel_arrived", at))
    st.on_event(ev("vehicle", at, known=False))
    st.on_event(ev("clip_started", at - datetime.timedelta(hours=10), trigger="motion"))
    st.on_event({"type": "health", "camera": "front"})                    # not counted

    a = st.activity(7)
    t = a["totals"]
    assert t["activity"] == 2 and t["person"] == 1 and t["family"] == 1 and t["stranger"] == 2
    assert t["people"] == 3, "family + strangers when faces are recognised"
    assert t["vehicle"] == 1 and t["parcel"] == 1 and a["face_recognition"]
    assert a["heatmap"]["activity"][5][14] == 1 and a["heatmap"]["activity"][5][4] == 1
    assert a["heatmap"]["people"][5][14] == 3
    assert a["busiest"]["weekday"] == "Saturday" and a["busiest"]["hour"] == 14
    day = next(d for d in a["daily"] if d["day"] == sat.isoformat())
    assert day["people"] == 3 and day["parcel"] == 1
    assert len(a["daily"]) == 7 and a["daily"][-1]["day"] == today.isoformat()
    # A camera filter.
    assert st.activity(7, camera="back")["totals"]["activity"] == 0


def test_people_without_face_recognition():
    assert statsmod.people_count({"person": 4, "family": 0, "stranger": 0}) == 4
    assert statsmod.people_count({"person": 1, "family": 2, "stranger": 1}) == 3


def test_backfill_from_recordings_on_disk(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    folder = tmp_path / "rec" / "front" / "2026-11-20"
    folder.mkdir(parents=True)
    for stem, meta in (("081500", {"start": "2026-11-20T08:15:00", "person": True, "seen": ["Stranger", "Parcel"]}),
                       ("230000", {"start": "2026-11-20T23:00:00", "person": False})):
        (folder / f"{stem}.mp4").write_bytes(b"x" * 1000)
        (folder / f"{stem}.json").write_text(json.dumps(meta))
    assert hub.stats.backfill() == 2
    assert hub.stats.backfill() == 0, "only once"
    rows = {(r["hour"], r["kind"]): r["n"] for r in hub.stats.db.execute("SELECT * FROM counts")}
    assert rows[("2026-11-20T08", "activity")] == 1 and rows[("2026-11-20T08", "person")] == 1
    assert rows[("2026-11-20T08", "stranger")] == 1 and rows[("2026-11-20T08", "parcel")] == 1
    assert rows[("2026-11-20T23", "activity")] == 1
    assert hub.stats.db.execute("SELECT bytes FROM recorded").fetchone()[0] == 2000


def test_hot_spots(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    cam = hub.cameras["front"]
    hub.stats.attach(cam)
    mask = np.zeros((180, 320), np.uint8)
    mask[100:160, 200:260] = 255                     # movement low on the right
    for _ in range(20):
        cam.on_motion_mask(mask)
    heat = hub.stats.heat_for("front")
    assert heat.total() > 0
    png = cv2.imdecode(np.frombuffer(heat.picture(320, 180), np.uint8), cv2.IMREAD_UNCHANGED)
    assert png.shape == (180, 320, 4)
    assert png[130, 230, 3] > 100 and png[20, 20, 3] == 0, "glow where it moved, clear elsewhere"
    heat.save()
    again = statsmod.HeatMap(heat.path)
    assert abs(again.total() - heat.total()) < 1e-3
    heat.fade(statsmod.HEAT_HALF_LIFE_DAYS * 86400)
    assert abs(heat.total() - again.total() / 2) < 1e-2
    heat.clear()
    assert statsmod.HeatMap(heat.path).total() == 0


def test_health_uptime_and_disk(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    st = hub.stats
    now = 1_800_000_000
    cam = hub.cameras["front"]
    for i in range(12):                         # an hour of samples, offline for 3 of them
        cam.online = i not in (3, 4, 5)
        cam._frame_id += 1500
        st.sample_cameras(now - 3600 + i * 300)
    cam.online = True
    h = statsmod.camera_health("front", "Front", st.db.execute("SELECT * FROM samples ORDER BY t").fetchall(),
                               now, online_now=True)
    assert h["uptime_24h"] == 75.0
    assert h["online_since"] == now - 3600 + 5 * 300 + 300
    assert h["timeline"] and h["signal"] is None and not h["signal_supported"]
    # Disk: 1 GB a day recorded, keep 30 days.
    for d in range(1, 4):
        st.db.execute("INSERT INTO recorded (day, camera, bytes) VALUES (?, 'front', ?)",
                      ((datetime.date.today() - datetime.timedelta(days=d)).isoformat(), 1024 ** 3))
    out = st.health()["disk"]
    assert out["per_day"] == 1024 ** 3
    assert out["days_left"] is not None and out["free"] > 0


def test_weekly_report_once_on_monday(tmp_path):
    config, _ = make_config(tmp_path)
    hub = FakeHub(config)
    st = hub.stats
    q = hub.events.subscribe()
    monday = datetime.datetime(2026, 11, 30, 7, 0)
    sat = datetime.datetime(2026, 11, 28, 14, 5)
    for _ in range(3):
        st.on_event(ev("stranger", sat))
    st.on_event(ev("known_person", sat))
    st.on_event(ev("clip_started", sat, person=True))
    st.on_event(ev("parcel_arrived", sat))
    assert st.weekly_tick(monday) is None, "before 08:00"
    r = st.weekly_tick(monday.replace(hour=8, minute=1))
    assert r and r["week"] == "2026-11-23"
    assert r["title"] == "4 visitors, 3 strangers, 1 parcel"
    assert "Busiest: Saturday 2pm." in r["text"]
    e = q.get_nowait()
    assert e["type"] == "weekly_report" and e["title"] == r["title"]
    assert st.weekly_tick(monday.replace(hour=9)) is None, "only once a week"
    assert st.weekly_tick(datetime.datetime(2026, 12, 2, 9)) is None, "not on Wednesday"
    assert st.reports()[0]["week"] == "2026-11-23"
    config.data["stats"]["weekly_report"] = False
    assert st.weekly_tick(datetime.datetime(2026, 12, 7, 9)) is None


def test_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    config, path = make_config(tmp_path)
    hub = FakeHub(config)

    class Det:
        available = False

    hub.detector = Det()
    client = TestClient(create_app(hub))
    assert client.get("/api/stats").status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200
    hub.stats.on_event(ev("clip_started", datetime.datetime.now(), person=True))
    r = client.get("/api/stats?days=30").json()
    assert r["days"] == 30 and r["totals"]["activity"] == 1 and r["cameras"][0]["name"] == "Front"
    assert r["settings"] == {"weekly_report": True, "weekly_time": "08:00"}
    h = client.get("/api/stats/health").json()
    assert h["cameras"][0]["id"] == "front" and "disk" in h
    png = client.get("/api/stats/hotspots/front.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert client.get("/api/stats/hotspots/nope.png").status_code == 404
    assert client.delete("/api/stats/hotspots/front").json() == {"ok": True}
    r = client.put("/api/stats/settings", json={"weekly_report": False, "weekly_time": "25:00"}).json()
    assert r == {"weekly_report": False, "weekly_time": "08:00"}
    assert json.loads(path.read_text())["stats"]["weekly_report"] is False
    q = hub.events.subscribe()
    r = client.post("/api/stats/weekly/send").json()
    assert "visitor" in r["title"] and q.get_nowait()["test"] is True
    assert hub.stats.reports() == [], "a test send doesn't stop Monday's report"
