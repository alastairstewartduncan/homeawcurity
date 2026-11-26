"""Talking through the camera's speaker, for the Hold to talk button.

Uses the standard ONVIF "audio backchannel": we ask the camera over RTSP for a stream that
goes the other way, then send it G.711 sound (the format phones used for decades) as RTP
packets over the same connection. Cameras that don't offer a backchannel only allow
talking from their maker's app, and the button stays hidden for them.

The pages send short pieces of 8 kHz 16-bit sound every fifth of a second. The first piece
opens the connection to the camera, and it's closed again a couple of seconds after the
last one.
"""
import base64
import hashlib
import logging
import queue
import random
import re
import socket
import struct
import threading
import time
from urllib.parse import unquote, urlsplit

import numpy as np

log = logging.getLogger("camhub.talk")

RATE = 8000
PACKET = 160                  # samples per RTP packet (20 ms)
REQUIRE = "www.onvif.org/ver20/backchannel"


# ---- G.711 -----------------------------------------------------------------

def ulaw_encode(pcm):
    """16-bit samples (numpy int16) to G.711 mu-law bytes."""
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0)
    x = np.minimum(np.abs(x), 32635) + 0x84
    exp = np.floor(np.log2(x)).astype(np.int32) - 7
    exp = np.clip(exp, 0, 7)
    mant = (x >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8).tobytes()


def alaw_encode(pcm):
    """16-bit samples (numpy int16) to G.711 A-law bytes."""
    x = pcm.astype(np.int32) >> 3          # A-law works on 13 bits
    sign = np.where(x >= 0, 0x80, 0)
    x = np.where(x < 0, -x - 1, x)
    x = np.minimum(x, 0xFFF)
    exp = np.where(x < 32, 0, np.floor(np.log2(np.maximum(x, 1))).astype(np.int32) - 4)
    exp = np.clip(exp, 0, 7)
    mant = np.where(exp == 0, x >> 1, x >> exp) & 0x0F
    return ((sign | (exp << 4) | mant) ^ 0x55).astype(np.uint8).tobytes()


# ---- RTSP ------------------------------------------------------------------

class RtspError(Exception):
    pass


class RtspClient:
    """Just enough RTSP over one TCP connection for the backchannel."""

    def __init__(self, url, timeout=8):
        u = urlsplit(url)
        self.host, self.port = u.hostname, u.port or 554
        self.user, self.password = unquote(u.username or ""), unquote(u.password or "")
        netloc = self.host + (f":{u.port}" if u.port else "")
        self.url = f"rtsp://{netloc}{u.path or '/'}" + (f"?{u.query}" if u.query else "")
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.cseq = 0
        self.session = None
        self._auth = None          # (scheme, params)
        self._buf = b""
        self.lock = threading.Lock()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _auth_header(self, method, uri):
        if not self._auth or not self.user:
            return ""
        scheme, p = self._auth
        if scheme == "basic":
            return "Authorization: Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode() + "\r\n"
        ha1 = hashlib.md5(f"{self.user}:{p.get('realm', '')}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{p.get('nonce', '')}:{ha2}".encode()).hexdigest()
        return (f'Authorization: Digest username="{self.user}", realm="{p.get("realm", "")}", '
                f'nonce="{p.get("nonce", "")}", uri="{uri}", response="{resp}"\r\n')

    def _read_reply(self):
        """Reads one RTSP reply, skipping any interleaved RTP/RTCP the camera sends meanwhile."""
        while True:
            while len(self._buf) < 4:
                self._recv()
            if self._buf[:1] == b"$":
                n = struct.unpack(">H", self._buf[2:4])[0]
                while len(self._buf) < 4 + n:
                    self._recv()
                self._buf = self._buf[4 + n:]
                continue
            while b"\r\n\r\n" not in self._buf:
                self._recv()
            head, self._buf = self._buf.split(b"\r\n\r\n", 1)
            lines = head.decode("utf-8", "replace").split("\r\n")
            m = re.match(r"RTSP/1\.\d (\d+)", lines[0])
            if not m:
                raise RtspError("Unexpected reply: " + lines[0][:80])
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers.setdefault(k.strip().lower(), v.strip())
            n = int(headers.get("content-length", 0))
            while len(self._buf) < n:
                self._recv()
            body, self._buf = self._buf[:n], self._buf[n:]
            return int(m.group(1)), headers, body.decode("utf-8", "replace")

    def _recv(self):
        data = self.sock.recv(65536)
        if not data:
            raise RtspError("The camera closed the connection")
        self._buf += data

    def request(self, method, uri=None, headers=None, retry=True):
        uri = uri or self.url
        with self.lock:
            self.cseq += 1
            msg = f"{method} {uri} RTSP/1.0\r\nCSeq: {self.cseq}\r\nUser-Agent: HomeCamera\r\n"
            msg += self._auth_header(method, uri)
            if self.session:
                msg += f"Session: {self.session}\r\n"
            for k, v in (headers or {}).items():
                msg += f"{k}: {v}\r\n"
            self.sock.sendall((msg + "\r\n").encode())
            code, h, body = self._read_reply()
        if code == 401 and retry and self.user:
            www = h.get("www-authenticate", "")
            if www.lower().startswith("digest"):
                self._auth = ("digest", dict(re.findall(r'(\w+)="([^"]*)"', www)))
            else:
                self._auth = ("basic", {})
            return self.request(method, uri, headers, retry=False)
        return code, h, body

    def send_rtp(self, channel, packet):
        with self.lock:
            self.sock.sendall(b"$" + bytes([channel]) + struct.pack(">H", len(packet)) + packet)


def find_backchannel(sdp, base_url):
    """Returns (control_url, payload_type, codec) for the camera's talk track, or None."""
    sections = re.split(r"\r?\n(?=m=)", sdp)
    session_base = base_url
    m = re.search(r"^a=control:(\S+)", sections[0], re.M)
    if m and m.group(1) != "*":
        session_base = m.group(1) if "://" in m.group(1) else session_base
    for sec in sections[1:]:
        if not sec.startswith("m=audio") or not re.search(r"^a=sendonly", sec, re.M):
            continue
        pts = sec.split("\n", 1)[0].split()[3:]
        codecs = dict(re.findall(r"^a=rtpmap:(\d+) ([\w\-]+)/", sec, re.M))
        choice = None
        for pt in pts:
            name = codecs.get(pt) or {"0": "PCMU", "8": "PCMA"}.get(pt)
            if name and name.upper() in ("PCMU", "PCMA"):
                choice = (int(pt), name.upper())
                break
        if not choice:
            continue
        ctl = re.search(r"^a=control:(\S+)", sec, re.M)
        ctl = ctl.group(1) if ctl else ""
        if "://" not in ctl:
            ctl = session_base.rstrip("/") + "/" + ctl.lstrip("/")
        return ctl, choice[0], choice[1]
    return None


def probe(url):
    """True if the camera offers a talk track, False if it doesn't. Raises if it can't be asked."""
    c = RtspClient(url)
    try:
        code, _, sdp = c.request("DESCRIBE", headers={"Accept": "application/sdp", "Require": REQUIRE})
        if code == 551 or code == 400:      # "Option not supported"
            return False
        if code != 200:
            raise RtspError(f"The camera answered {code}")
        return find_backchannel(sdp, c.url) is not None
    finally:
        c.close()


class TalkSession:
    """One open backchannel. Feed it 8 kHz 16-bit sound with `put`."""

    def __init__(self, url):
        self.client = RtspClient(url)
        c = self.client
        code, _, sdp = c.request("DESCRIBE", headers={"Accept": "application/sdp", "Require": REQUIRE})
        if code != 200:
            raise RtspError(f"The camera answered {code} when asked to talk")
        found = find_backchannel(sdp, c.url)
        if not found:
            raise RtspError("This camera doesn't accept sound from Home Camera")
        ctl, self.pt, codec = found
        self.encode = ulaw_encode if codec == "PCMU" else alaw_encode
        code, h, _ = c.request("SETUP", ctl, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1", "Require": REQUIRE})
        if code != 200:
            raise RtspError(f"The camera answered {code} when setting up talk")
        c.session = h.get("session", "").split(";")[0]
        m = re.search(r"interleaved=(\d+)", h.get("transport", ""))
        self.channel = int(m.group(1)) if m else 0
        code, _, _ = c.request("PLAY", headers={"Range": "npt=0.000-", "Require": REQUIRE})
        if code != 200:
            raise RtspError(f"The camera answered {code} when starting talk")
        c.sock.settimeout(None)
        self.q = queue.Queue(maxsize=100)
        self.closed = False
        self.last_put = time.monotonic()
        self.play_end = time.monotonic()   # when the sound already sent will have finished playing
        self.seq = random.randint(0, 0xFFFF)
        self.ts = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    def put(self, pcm_bytes):
        now = time.monotonic()
        self.last_put = now
        self.play_end = max(now, self.play_end) + len(pcm_bytes) / 2 / RATE
        try:
            self.q.put_nowait(pcm_bytes)
        except queue.Full:
            pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.client.sock.settimeout(2)
            self.client.request("TEARDOWN")
        except Exception:
            pass
        self.client.close()

    def _drain(self):
        # The camera may send RTCP reports back; read and ignore them so its buffers don't fill.
        try:
            while not self.closed:
                if not self.client.sock.recv(4096):
                    break
        except OSError:
            pass
        self.closed = True

    def _send_loop(self):
        pending = np.zeros(0, dtype=np.int16)
        next_at = time.monotonic()
        last_keepalive = time.monotonic()
        try:
            while not self.closed:
                try:
                    data = self.q.get(timeout=0.1)
                    pending = np.concatenate([pending, np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")])
                except queue.Empty:
                    pass
                while len(pending) >= PACKET and not self.closed:
                    chunk, pending = pending[:PACKET], pending[PACKET:]
                    header = struct.pack(">BBHII", 0x80, self.pt & 0x7F, self.seq & 0xFFFF,
                                         self.ts & 0xFFFFFFFF, self.ssrc)
                    now = time.monotonic()
                    if next_at > now:
                        time.sleep(next_at - now)
                    elif now - next_at > 0.5:
                        next_at = now      # we fell behind (a pause in speaking): restart the clock
                    self.client.send_rtp(self.channel, header + self.encode(chunk))
                    self.seq += 1
                    self.ts += PACKET
                    next_at += PACKET / RATE
                if time.monotonic() - last_keepalive > 20:
                    last_keepalive = time.monotonic()
                    # Interleaved replies are read by _drain; the camera only needs to see traffic.
                    with self.client.lock:
                        self.client.cseq += 1
                        self.client.sock.sendall(
                            f"OPTIONS {self.client.url} RTSP/1.0\r\nCSeq: {self.client.cseq}\r\n"
                            f"Session: {self.client.session}\r\n\r\n".encode())
        except OSError as e:
            log.warning("Talk connection lost: %s", e)
        self.closed = True


class Talker:
    """Talk for one camera: whether it can, and the open session while someone is talking."""

    IDLE_CLOSE = 2.0

    def __init__(self, cam_id, url_fn):
        self.cam_id = cam_id
        self._url_fn = url_fn
        self.supported = None       # None until the camera has been asked
        self.problem = None
        self._session = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name=f"talk-{self.cam_id}", daemon=True).start()

    def stop(self):
        self._stop.set()
        with self._lock:
            if self._session:
                self._session.close()
                self._session = None

    def _loop(self):
        wait = 15
        while not self._stop.is_set():
            if self.supported is None:
                url = self._url_fn()
                if url and url.lower().startswith("rtsp"):
                    try:
                        self.supported = probe(url)
                        log.info("[%s] Talk through the camera: %s", self.cam_id,
                                 "available" if self.supported else "not offered by this camera")
                        if not self.supported:
                            self.problem = "This camera only allows talking from its own app."
                    except Exception as e:
                        log.info("[%s] Couldn't ask the camera about talk yet: %s", self.cam_id, e)
                        self._stop.wait(wait)
                        wait = min(wait * 2, 300)
                        continue
            with self._lock:
                s = self._session
                if s and (s.closed or time.monotonic() - max(s.last_put, s.play_end) > self.IDLE_CLOSE):
                    s.close()
                    self._session = None
            self._stop.wait(0.5)

    def put(self, pcm_bytes):
        with self._lock:
            if self._session is None or self._session.closed:
                url = self._url_fn()
                if not url:
                    raise RtspError("No camera address saved yet.")
                self._session = TalkSession(url)
                log.info("[%s] Talking through the camera", self.cam_id)
            self._session.put(pcm_bytes)
