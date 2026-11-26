"""Privacy areas: covered in the live picture and in recordings.  Run:  python -m pytest tests"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth, privacy  # noqa: E402
from camhub.config import DEFAULT_MOTION, Config  # noqa: E402
from camhub.storage import Storage  # noqa: E402

TOP_LEFT = {"style": "black", "masks": [{"name": "Window", "points": [[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]]}]}
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def test_clean_keeps_areas_inside_the_picture():
    out = privacy.clean({"style": "weird", "masks": [
        {"name": "A", "points": [[-1, 0], [2, 0], [0.5, 0.5]]},
        {"name": "too few", "points": [[0, 0], [1, 1]]},
        {"points": [["x", 0], [0, 0], [1, 1]]},
    ]})
    assert out["style"] == "blur"
    assert len(out["masks"]) == 1
    assert out["masks"][0]["points"] == [[0.0, 0.0], [1.0, 0.0], [0.5, 0.5]]


def test_frame_is_covered_only_inside_the_area():
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
    black = privacy.apply_to_frame(frame, TOP_LEFT)
    assert black[:170, :310].max() == 0
    assert np.array_equal(black[200:, 340:], frame[200:, 340:])
    blur = privacy.apply_to_frame(frame, {**TOP_LEFT, "style": "blur"})
    # Pixelated: neighbouring pixels inside a block are the same; outside is untouched.
    assert np.array_equal(blur[10, 10], blur[11, 11])
    assert not np.array_equal(blur[:170, :310], frame[:170, :310])
    assert np.array_equal(blur[200:, 340:], frame[200:, 340:])
    # Read-only pictures (from ffmpeg) work, and no areas means the same picture back.
    ro = frame.copy()
    ro.setflags(write=False)
    privacy.apply_to_frame(ro, TOP_LEFT)
    assert privacy.apply_to_frame(frame, {"masks": []}) is frame


def _test_video(path, seconds=3, fmt="mp4"):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
                    "-f", "lavfi", "-i", "sine=f=440:sample_rate=16000", "-t", str(seconds),
                    "-c:v", "libx264", "-g", "15", "-c:a", "aac", "-f", fmt, str(path)], check=True)


def _frame_at(path, second=1):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(second), "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(360, 640, 3)


@needs_ffmpeg
def test_recording_is_made_again_with_areas_covered(tmp_path):
    src = tmp_path / "clip.mp4.part"
    _test_video(src)
    dst = tmp_path / "out.mp4"
    assert privacy.mask_clip(str(src), str(dst), TOP_LEFT)
    before, after = _frame_at(src), _frame_at(dst)
    assert after[10:160, 10:300].mean() < 20                 # covered
    assert before[10:160, 10:300].mean() > 60
    diff = np.abs(after[220:, 360:].astype(int) - before[220:, 360:].astype(int)).mean()
    assert diff < 8                                           # the rest looks the same
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                              str(dst)], capture_output=True, text=True).stdout.split()
    assert streams == ["video", "audio"]                      # sound kept


@needs_ffmpeg
def test_clip_writer_covers_areas_before_the_clip_appears(tmp_path):
    from camhub.camera import ClipWriter

    ts = tmp_path / "in.ts"
    _test_video(ts, fmt="mpegts")
    import datetime
    base = str(tmp_path / "120000")
    w = ClipWriter(base, "front_20261126_120000", "front", datetime.datetime(2026, 11, 26, 12), "motion",
                   False, None, b"", privacy_settings=TOP_LEFT)
    data = ts.read_bytes()
    for i in range(0, len(data), 188 * 64):
        w.write(data[i:i + 188 * 64])
    done = threading.Event()
    result = {}
    w.close(lambda meta: (result.update(meta=meta), done.set()))
    assert done.wait(60)
    assert result["meta"]["privacy_masked"] is True
    assert os.path.exists(base + ".mp4") and not os.path.exists(base + ".mp4.part")
    assert _frame_at(base + ".mp4")[10:160, 10:300].mean() < 20
    assert json.load(open(base + ".json"))["privacy_masked"] is True


@needs_ffmpeg
def test_live_picture_is_covered(tmp_path):
    from camhub import camera as cam_mod
    from camhub.events import EventBus

    video = tmp_path / "sub.mp4"
    _test_video(video, seconds=4)
    (tmp_path / "config.json").write_text(json.dumps({"storage": {"path": str(tmp_path / "rec")}, "cameras": []}))
    cfg = Config(str(tmp_path / "config.json"))

    class NoPeople:
        available = False

    c = {"id": "front", "name": "Front", "main_url": "", "sub_url": str(video),
         "motion": dict(DEFAULT_MOTION), "privacy": TOP_LEFT}
    cam = cam_mod.Camera(c, Storage(cfg), EventBus(), NoPeople())
    threading.Thread(target=cam._analyze_loop, daemon=True).start()
    t0 = time.monotonic()
    jpeg = None
    while jpeg is None and time.monotonic() - t0 < 20:
        jpeg, _ = cam.latest_jpeg()
        time.sleep(0.2)
    cam.stop()
    assert jpeg is not None
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    assert img[5:int(h * 0.45), 5:int(w * 0.45)].mean() < 20
    assert img[int(h * 0.6):, int(w * 0.6):].mean() > 40


def test_privacy_api_saves_areas(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server": {"password_hash": auth.hash_password("pw123456")},
        "storage": {"path": str(tmp_path / "rec")},
        "cameras": [{"id": "front", "name": "Front"}]}))
    config = Config(str(cfg_path))

    class FakeCam:
        def __init__(self, cfg):
            self.cfg = cfg

    class FakeHub:
        pass

    hub = FakeHub()
    hub.config = config
    hub.storage = Storage(config)
    hub.cameras = {"front": FakeCam(config.cameras[0])}
    client = TestClient(create_app(hub))
    assert client.get("/api/cameras/front/privacy").status_code == 401
    client.post("/api/login", json={"password": "pw123456"})
    assert client.get("/api/cameras/front/privacy").json() == {"style": "blur", "masks": []}
    r = client.put("/api/cameras/front/privacy", json={"style": "black", "masks": [
        {"name": "Street", "points": [[0, 0.8], [1, 0.8], [1, 1.5]]}]})
    assert r.status_code == 200
    saved = json.loads(cfg_path.read_text())["cameras"][0]["privacy"]
    assert saved["style"] == "black"
    assert saved["masks"][0]["points"][2] == [1.0, 1.0]
    assert hub.cameras["front"].cfg["privacy"] == saved
    assert client.get("/api/cameras/nope/privacy").status_code == 404
    # Settings saved this way survive a restart.
    assert Config(str(cfg_path)).cameras[0]["privacy"] == saved
