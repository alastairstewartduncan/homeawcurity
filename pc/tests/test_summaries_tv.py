"""Checks for night summaries and watching on TV."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth, summaries  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.storage import Storage  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")


class FakeCam:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]
        self._frame_cond = threading.Condition()
        self._frame = np.full((360, 640, 3), 90, np.uint8)

    @property
    def motion(self):
        return self.cfg["motion"]


def make_hub(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server": {"password_hash": auth.hash_password("pw123456")},
        "storage": {"path": str(tmp_path / "rec")},
        "cameras": [{"id": "front", "name": "Front"}, {"id": "back", "name": "Back"}]}))
    config = Config(str(cfg_path))

    class Hub:
        pass

    hub = Hub()
    hub.config = config
    hub.storage = Storage(config)
    hub.events = EventBus()
    hub.cameras = {c["id"]: FakeCam(c) for c in config.cameras}
    from camhub.tv import TV
    hub.summaries = summaries.NightSummaries(hub)
    hub.tv = TV(hub)
    return hub


def add_clip(hub, cam, when, seconds=4, person=False):
    clip_id, base = hub.storage.new_clip_paths(cam, when)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=15",
                    "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", base + ".mp4"], check=True)
    with open(base + ".json", "w") as f:
        json.dump({"start": when.isoformat(timespec="seconds"), "duration": seconds, "person": person}, f)
    return clip_id


def test_night_window_and_speed():
    s = summaries.clean_settings({"start": "21:00", "end": "07:00"})
    a, b = summaries.night_window(datetime.date(2026, 11, 26), s)
    assert a == datetime.datetime(2026, 11, 25, 21, 0) and b == datetime.datetime(2026, 11, 26, 7, 0)
    assert summaries.pick_speed(0, 60) == summaries.MIN_SPEED
    assert summaries.pick_speed(3600, 60) == 60
    assert summaries.pick_speed(10 ** 6, 60) == summaries.MAX_SPEED
    bad = summaries.clean_settings({"start": "25:00", "seconds": 1, "keep_days": "x"})
    assert bad["start"] == "21:00" and bad["seconds"] == 15 and bad["keep_days"] == 30


@needs_ffmpeg
def test_night_summary_is_made(tmp_path, monkeypatch):
    monkeypatch.setattr("camhub.camera.FFMPEG", "ffmpeg", raising=False)
    hub = make_hub(tmp_path)
    q = hub.events.subscribe()
    add_clip(hub, "front", datetime.datetime(2026, 11, 25, 23, 10, 0), person=True)
    add_clip(hub, "back", datetime.datetime(2026, 11, 26, 2, 30, 0))
    add_clip(hub, "front", datetime.datetime(2026, 11, 26, 12, 0, 0))   # daytime: left out
    # The summaries folder must never look like a camera.
    os.makedirs(os.path.join(hub.storage.root, "_summaries"), exist_ok=True)
    assert all(d["count"] for d in hub.storage.days())

    assert hub.summaries.tick(datetime.datetime(2026, 11, 26, 7, 0)) is False   # before the ready time
    info = hub.summaries.make("2026-11-26")
    assert info["count"] == 2 and info["people"] == 1 and info["video"]
    assert [c["camera"] for c in info["chapters"]] == ["front", "back"]
    assert info["chapters"][0]["t"] == 1.0          # after the one-second title card
    assert os.path.getsize(hub.summaries.path("2026-11-26", ".mp4")) > 1000
    assert os.path.isfile(hub.summaries.path("2026-11-26", ".jpg"))
    e = q.get(timeout=1)
    assert e["type"] == "night_summary" and e["count"] == 2 and "2 recordings" in e["text"]
    assert "_summaries" not in [c for c in os.listdir(hub.storage.root) if c in hub.storage._cams(None)]

    # Made once a morning only.
    assert hub.summaries.tick(datetime.datetime(2026, 11, 26, 8, 0)) is False

    # A quiet night still gets a note, without a video.
    quiet = hub.summaries.make("2026-11-20")
    assert quiet["count"] == 0 and not quiet["video"]
    assert summaries.headline(quiet).startswith("Quiet night")

    hub.summaries.prune(datetime.date(2027, 1, 30))
    assert hub.summaries.items() == []


@needs_ffmpeg
def test_summary_and_tv_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    monkeypatch.setattr("camhub.camera.FFMPEG", "ffmpeg", raising=False)
    hub = make_hub(tmp_path)
    add_clip(hub, "front", datetime.datetime(2026, 11, 26, 3, 0, 0))
    client = TestClient(create_app(hub))
    assert client.get("/api/summaries").status_code == 401
    assert client.post("/api/tv/link").status_code == 401
    assert client.get("/tv", follow_redirects=False).headers["location"] == "/login?next=/tv"
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200

    r = client.put("/api/summaries/settings", json={"seconds": 90, "start": "22:30"})
    assert r.json()["seconds"] == 90 and r.json()["start"] == "22:30"
    assert client.post("/api/summaries/make", json={"date": "../x"}).status_code == 400
    assert client.post("/api/summaries/make", json={"date": "2026-11-26"}).json()["started"]
    for _ in range(200):
        if not hub.summaries.busy and client.get("/api/summaries").json()["items"]:
            break
        time.sleep(0.1)
    items = client.get("/api/summaries").json()["items"]
    assert items[0]["date"] == "2026-11-26" and items[0]["count"] == 1
    assert client.get("/api/summaries/2026-11-26").json()["chapters"][0]["camera"] == "front"
    assert client.get("/api/summaries/2026-11-26/video").headers["content-type"] == "video/mp4"
    assert client.get("/tv").status_code == 200

    link = client.post("/api/tv/link").json()
    assert [s["id"] for s in link["sources"]] == ["all", "front", "back"]
    other = TestClient(create_app(hub))           # like a Chromecast: not signed in
    assert other.get("/tv/wrong-key/front/live.m3u8").status_code == 403
    assert other.get("/api/summaries").status_code == 401
    r = other.get(link["summary"] + "2026-11-26.mp4")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    r = other.get(link["sources"][1]["live"])
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == "*"
    piece = [ln for ln in r.text.splitlines() if ln and not ln.startswith("#")][0]
    assert piece.startswith("p/front-")
    base = link["sources"][1]["live"].rsplit("/", 1)[0]
    seg = other.get(f"{base}/{piece}")
    assert seg.status_code == 200 and seg.content[:1] == b"\x47"     # MPEG-TS
    assert client.delete("/api/summaries/2026-11-26").json()["ok"]
    hub.tv.stop_all()
