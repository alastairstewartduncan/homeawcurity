"""Quick checks for the parts that don't need a camera.  Run:  python -m pytest tests"""
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.storage import Storage  # noqa: E402
from camhub.tsbuffer import PreRollBuffer, TsPacketizer, TsStream  # noqa: E402


def test_password_and_token():
    h = auth.hash_password("correct horse")
    assert auth.check_password("correct horse", h)
    assert not auth.check_password("wrong", h)
    t = auth.make_token("s1")
    assert auth.check_token("s1", t)
    assert not auth.check_token("s2", t)
    assert not auth.check_token("s1", t + "x")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_keyframes_found_and_preroll_starts_on_one():
    ts = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "6",
         "-c:v", "libx264", "-g", "10", "-f", "mpegts", "pipe:1"], capture_output=True, check=True).stdout
    p, s = TsPacketizer(), TsStream()
    buf = PreRollBuffer(seconds=2)
    keys = 0
    t = 0.0
    for pkt in p.feed(ts[:1000]):
        pass
    for pkt in list(p.feed(ts[1000:])):
        k = s.inspect(pkt)
        keys += k
        t += 0.001
        buf.add(pkt, k, now=t)
    assert s.video_pid is not None and s.video_type == 0x1B
    assert keys >= 5
    # The kept data is decodable from its first byte.
    data = s.headers() + buf.snapshot()
    r = subprocess.run(["ffprobe", "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-show_entries",
                        "stream=codec_name", "-of", "csv"], input=data, capture_output=True)
    assert b"h264" in r.stdout


def test_clip_ids_cannot_escape_storage(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"storage": {"path": str(tmp_path / "rec")}}))
    st = Storage(Config(str(cfg_path)))
    assert st.clip_base("front_20261122_074900").endswith(os.path.join("front", "2026-11-22", "074900"))
    for bad in ("../x_20261122_074900", "front_2026112_074900", "front_20261122_07490a", "a/b_20261122_074900"):
        assert st.clip_base(bad) is None


def test_settings_api_saves_zones(tmp_path):
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

        @property
        def motion(self):
            return self.cfg["motion"]

    class FakeHub:
        pass

    hub = FakeHub()
    hub.config = config
    hub.storage = Storage(config)
    hub.cameras = {"front": FakeCam(config.cameras[0])}
    client = TestClient(create_app(hub))
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200
    zones = [{"name": "Road", "points": [[0, 0], [0.5, 0], [0.5, 2]]}]
    r = client.put("/api/cameras/front/settings", json={"motion": {"ignore_zones": zones, "sensitivity": 99}})
    assert r.status_code == 200
    saved = json.loads(cfg_path.read_text())["cameras"][0]["motion"]
    assert saved["sensitivity"] == 10
    assert saved["ignore_zones"][0]["points"][2] == [0.5, 1.0]


def test_ignore_zone_blocks_motion():
    from camhub.camera import Camera

    cam = Camera.__new__(Camera)
    cam.cfg = {"motion": {"ignore_zones": [{"name": "All", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}}
    cam._mask_key = None
    import cv2
    bg = cv2.createBackgroundSubtractorMOG2(history=50, varThreshold=16, detectShadows=False)
    still = np.full((360, 640, 3), 80, np.uint8)
    for _ in range(20):
        cam._motion_level(bg, still)
    moved = still.copy()
    moved[100:250, 200:350] = 255
    assert cam._motion_level(bg, moved) == 0
    cam.cfg["motion"]["ignore_zones"] = []
    bg = cv2.createBackgroundSubtractorMOG2(history=50, varThreshold=16, detectShadows=False)
    for _ in range(20):
        cam._motion_level(bg, still)
    assert cam._motion_level(bg, moved) > 5


def test_ptz_talks_onvif():
    import http.server
    import threading

    from camhub.ptz import OnvifPtz

    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            seen.append(body)
            port = self.server.server_address[1]
            if "GetSystemDateAndTime" in body:
                out = ("<tt:UTCDateTime><tt:Date><tt:Year>2026</tt:Year><tt:Month>11</tt:Month><tt:Day>22</tt:Day>"
                       "</tt:Date><tt:Time><tt:Hour>8</tt:Hour><tt:Minute>0</tt:Minute><tt:Second>0</tt:Second>"
                       "</tt:Time></tt:UTCDateTime>")
            elif "GetCapabilities" in body:
                out = (f"<tt:Media><tt:XAddr>http://10.0.0.9:{port}/onvif/media</tt:XAddr></tt:Media>"
                       f"<tt:PTZ><tt:XAddr>http://10.0.0.9:{port}/onvif/ptz</tt:XAddr></tt:PTZ>")
            elif "GetProfiles" in body:
                out = '<trt:Profiles token="Profile_1" fixed="true"></trt:Profiles>'
            else:
                out = "<ok/>"
            data = f"<s:Envelope><s:Body>{out}</s:Body></s:Envelope>".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ptz = OnvifPtz("127.0.0.1", srv.server_address[1], "admin", "pw")
    assert ptz.connect(), ptz.error
    assert ptz.profile == "Profile_1"
    ptz.move(0.5, -0.5)
    ptz.stop()
    srv.shutdown()
    assert 'PanTilt x="0.50" y="-0.50"' in seen[-2]
    assert "<tptz:Stop>" in seen[-1] and "PasswordDigest" in seen[-1]


def test_camera_setup_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import camhub.discovery as discovery
    from camhub.__main__ import Hub
    from camhub.server import create_app

    calls = []

    def fake_find(ip, user, password, port=554, report=print):
        calls.append((ip, user, password))
        report("searching")
        return ("rtsp://cam/main", "rtsp://cam/sub") if ip == "10.0.0.5" else (None, None)

    monkeypatch.setattr(discovery, "find_streams", fake_find)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"server": {"password_hash": auth.hash_password("pw123456")},
                                    "storage": {"path": str(tmp_path / "rec")}}))
    config = Config(str(cfg_path))
    hub = Hub(config)  # not started, so no streams are opened
    client = TestClient(create_app(hub))
    client.post("/api/login", json={"password": "pw123456"})

    r = client.post("/api/cameras/setup", json={"name": "Front door", "ip": "10.0.0.9", "password": "x"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert client.get("/api/cameras").json() == []

    r = client.post("/api/cameras/setup", json={"name": "Front door", "ip": "10.0.0.5", "user": "admin",
                                                 "password": "secret"})
    assert r.json()["ok"] is True
    cams = client.get("/api/cameras").json()
    assert cams[0]["id"] == "front-door" and cams[0]["has_password"] and "password" not in cams[0]
    saved = json.loads(cfg_path.read_text())["cameras"][0]
    assert saved["main_url"] == "rtsp://cam/main" and saved["onvif"]["password"] == "secret"

    # Changing it with the password left empty keeps the saved one.
    r = client.post("/api/cameras/setup", json={"id": "front-door", "name": "Front", "ip": "10.0.0.5",
                                                 "user": "admin", "password": ""})
    assert r.json()["ok"] and calls[-1][2] == "secret"
    assert "front-door" in hub.cameras and hub.cameras["front-door"].cfg["name"] == "Front"

    assert client.put("/api/storage", json={"retention_days": 7, "min_free_gb": 5}).status_code == 200
    assert client.delete("/api/cameras/front-door").status_code == 200
    saved = json.loads(cfg_path.read_text())
    assert saved["cameras"] == [] and saved["storage"]["retention_days"] == 7
    assert client.post("/api/cameras/setup", json={"ip": "bad ip!"}).status_code == 400
