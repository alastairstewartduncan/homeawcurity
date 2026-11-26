"""Checks for listening and talking that don't need a real camera.  Run:  python -m pytest tests"""
import os
import queue
import re
import shutil
import socket
import struct
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import talk  # noqa: E402


# Reference G.711 encoders, one sample at a time, as in the classic Sun g711.c.
def ref_ulaw(s):
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if s < 0 else 0
    s = min(abs(s), CLIP) + BIAS
    exp = 7
    mask = 0x4000
    while exp > 0 and not (s & mask):
        exp -= 1
        mask >>= 1
    mant = (s >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mant) & 0xFF


def ref_alaw(s):
    s >>= 3
    if s >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        s = -s - 1
    seg_end = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]
    seg = next((i for i, e in enumerate(seg_end) if s <= e), 8)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= (s >> 1) & 0x0F if seg < 2 else (s >> seg) & 0x0F
    return aval ^ mask


def test_g711_matches_reference():
    samples = np.array(list(range(-32768, 32768, 7)) + [-32768, -1, 0, 1, 32767], dtype=np.int16)
    u = talk.ulaw_encode(samples)
    a = talk.alaw_encode(samples)
    for i, s in enumerate(samples.tolist()):
        assert u[i] == ref_ulaw(s), s
        assert a[i] == ref_alaw(s), s


SDP = """v=0\r
o=- 1 1 IN IP4 0.0.0.0\r
s=Session\r
a=control:*\r
m=video 0 RTP/AVP 96\r
a=rtpmap:96 H264/90000\r
a=control:trackID=0\r
m=audio 0 RTP/AVP 8\r
a=rtpmap:8 PCMA/8000\r
a=control:trackID=1\r
a=recvonly\r
m=audio 0 RTP/AVP 0\r
a=rtpmap:0 PCMU/8000\r
a=control:trackID=2\r
a=sendonly\r
"""


def test_find_backchannel():
    ctl, pt, codec = talk.find_backchannel(SDP, "rtsp://cam:554/ch0_0.264")
    assert ctl == "rtsp://cam:554/ch0_0.264/trackID=2"
    assert (pt, codec) == (0, "PCMU")
    no_talk = SDP.split("m=audio 0 RTP/AVP 0")[0]
    assert talk.find_backchannel(no_talk, "rtsp://cam/x") is None


class FakeCamera:
    """A tiny RTSP server with a talk track and Digest login, which counts the sound it gets."""

    def __init__(self, backchannel=True):
        self.backchannel = backchannel
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.rtp = queue.Queue()
        self.methods = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        buf = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
                while buf:
                    if buf[:1] == b"$":
                        if len(buf) < 4:
                            break
                        n = struct.unpack(">H", buf[2:4])[0]
                        if len(buf) < 4 + n:
                            break
                        self.rtp.put((buf[1], buf[4:4 + n]))
                        buf = buf[4 + n:]
                        continue
                    if b"\r\n\r\n" not in buf:
                        break
                    head, buf = buf.split(b"\r\n\r\n", 1)
                    lines = head.decode().split("\r\n")
                    method = lines[0].split()[0]
                    hdr = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
                    cseq = hdr.get("CSeq", "0")
                    self.methods.append(method)
                    if method != "OPTIONS" and "Authorization" not in hdr:
                        conn.sendall(f'RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n'
                                     f'WWW-Authenticate: Digest realm="cam", nonce="abc"\r\n\r\n'.encode())
                        continue
                    extra, body = "", ""
                    if method == "DESCRIBE":
                        if hdr.get("Require") == talk.REQUIRE and not self.backchannel:
                            conn.sendall(f"RTSP/1.0 551 Option not supported\r\nCSeq: {cseq}\r\n\r\n".encode())
                            continue
                        body = SDP
                        extra = "Content-Type: application/sdp\r\n"
                    elif method == "SETUP":
                        extra = "Session: 1234;timeout=60\r\nTransport: RTP/AVP/TCP;unicast;interleaved=4-5\r\n"
                    conn.sendall((f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}"
                                  f"Content-Length: {len(body.encode())}\r\n\r\n{body}").encode())
        except OSError:
            pass


def test_probe_and_talk_session():
    cam = FakeCamera()
    url = f"rtsp://admin:pw@127.0.0.1:{cam.port}/ch0_0.264"
    assert talk.probe(url) is True
    t = talk.Talker("test", lambda: url)
    t.IDLE_CLOSE = 0.5
    t.start()
    try:
        tone = (np.sin(np.arange(8000) / 8000 * 2 * np.pi * 440) * 8000).astype("<i2").tobytes()
        for i in range(0, len(tone), 3200):     # 0.2 s at a time, like the page sends
            t.put(tone[i:i + 3200])
        got = []
        deadline = time.time() + 5
        while len(got) < 50 and time.time() < deadline:
            try:
                got.append(cam.rtp.get(timeout=1))
            except queue.Empty:
                pass
        assert len(got) == 50                       # 1 s of sound in 20 ms packets
        ch, pkt = got[0]
        assert ch == 4                              # the channel the camera asked for
        assert pkt[1] & 0x7F == 0 and len(pkt) == 12 + 160
        seqs = [struct.unpack(">H", p[2:4])[0] for _, p in got]
        assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:]))
        # The connection closes by itself once the talking stops.
        deadline = time.time() + 5
        while "TEARDOWN" not in cam.methods and time.time() < deadline:
            time.sleep(0.1)
        assert "TEARDOWN" in cam.methods
    finally:
        t.stop()


def test_camera_without_talk_is_reported():
    cam = FakeCamera(backchannel=False)
    assert talk.probe(f"rtsp://admin:pw@127.0.0.1:{cam.port}/ch0_0.264") is False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_listen_relay_gives_sound(tmp_path):
    import subprocess

    from camhub.audio import RATE, AudioRelay

    clip = tmp_path / "sound.mkv"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5", "-t", "3", "-c:a", "pcm_mulaw",
                    "-c:v", "mjpeg", str(clip)], check=True)
    relay = AudioRelay("test", lambda: str(clip))
    q = relay.subscribe()
    try:
        total = 0
        deadline = time.time() + 10
        while total < RATE and time.time() < deadline:      # half a second of 16-bit sound
            data = q.get(timeout=5)
            assert data is not None, relay.problem
            total += len(data)
        assert total >= RATE
    finally:
        relay.unsubscribe(q)
        relay.stop()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_listen_without_sound_explains(tmp_path):
    import subprocess

    from camhub.audio import AudioRelay

    clip = tmp_path / "silent.mkv"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5",
                    "-t", "2", "-c:v", "mjpeg", str(clip)], check=True)
    relay = AudioRelay("test", lambda: str(clip))
    q = relay.subscribe()
    try:
        assert q.get(timeout=10) is None
        time.sleep(0.3)
        assert re.search(r"isn't sending any sound", relay.problem or "")
    finally:
        relay.unsubscribe(q)
        relay.stop()


def test_ptz_finds_the_right_port_and_zoom(monkeypatch):
    """The camera answers ONVIF on a different port from the saved one (8888 vs 8899 on the WHC905)."""
    import http.server

    from camhub import ptz

    replies = {
        "GetSystemDateAndTime": "<tds:SystemDateAndTime><tt:UTCDateTime><tt:Time><tt:Hour>1</tt:Hour></tt:Time>"
                                "<tt:Date><tt:Year>2026</tt:Year><tt:Month>1</tt:Month><tt:Day>1</tt:Day></tt:Date>"
                                "</tt:UTCDateTime></tds:SystemDateAndTime>",
        "GetCapabilities": "<tt:Media><tt:XAddr>http://10.0.0.1/onvif/media</tt:XAddr></tt:Media>"
                           "<tt:PTZ><tt:XAddr>http://10.0.0.1/onvif/ptz_service</tt:XAddr></tt:PTZ>",
        "GetProfiles": '<trt:Profiles fixed="true" token="PROFILE_000"></trt:Profiles>',
        "GetNodes": "<tt:ContinuousZoomVelocitySpace></tt:ContinuousZoomVelocitySpace>",
        "GotoHomePosition": "<tptz:GotoHomePositionResponse/>",
    }
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            name = re.search(r"<s:Body><\w+:(\w+)", body).group(1)
            calls.append(name)
            out = f"<s:Envelope><s:Body>{replies.get(name, '')}</s:Body></s:Envelope>".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    wrong = closed.getsockname()[1]
    closed.close()
    monkeypatch.setattr(ptz, "COMMON_PORTS", [port])
    found = []
    p = ptz.OnvifPtz("127.0.0.1", wrong, "admin", "pw", on_port_found=found.append)
    try:
        assert p.connect(), p.error
        assert found == [port] and p.port == port
        assert p.zoom is True and p.profile == "PROFILE_000"
        p.home()
        assert calls[-1] == "GotoHomePosition"
    finally:
        srv.shutdown()
