"""Watch on TV: a live stream that a Chromecast or smart TV can play, and the /tv page.

A Chromecast can't sign in or show the moving-JPEG live picture the viewer uses, so while
something is being cast the PC turns the live picture into a standard HLS video stream:
pictures go into FFmpeg, which makes H.264 pieces of two seconds. The pieces are kept in
memory only (FFmpeg hands them to a small server on 127.0.0.1), so casting writes nothing to
disk. The stream stops by itself soon after the TV stops asking for it.

The TV gets a link with a random key in it instead of a password. Keys are made by a signed-in
viewer or phone ("Cast to TV"), and stop working after 12 hours unused.

Sources: a camera id, or "all" for every camera side by side.
"""
import logging
import math
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("camhub.tv")

WIDTH, HEIGHT, FPS = 1280, 720, 10
SEGMENT = 2                   # seconds in each piece of video
IDLE_STOP = 40                # seconds without the TV asking before the stream stops
KEY_LIFE = 12 * 3600


class Pieces:
    """Receives FFmpeg's playlist and video pieces over HTTP PUT and keeps them in memory."""

    def __init__(self):
        self.files = {}           # "<stream>/<name>" -> bytes
        self.lock = threading.Lock()
        store = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _body(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    out = bytearray()
                    while True:
                        line = self.rfile.readline()
                        if not line:
                            break
                        size = int(line.split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        out += self.rfile.read(size)
                        self.rfile.readline()
                    return bytes(out)
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _done(self, code=200):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def do_PUT(self):
                data = self._body()
                with store.lock:
                    store.files[self.path.strip("/")] = data
                    if self.path.endswith(".m3u8"):
                        store._forget_old(self.path.strip("/"), data)
                self._done()

            do_POST = do_PUT

            def do_DELETE(self):
                with store.lock:
                    store.files.pop(self.path.strip("/"), None)
                self._done()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, name="tv-pieces", daemon=True).start()

    def _forget_old(self, playlist_path, data):
        """Drops pieces the playlist no longer lists (keeping a few for slow TVs)."""
        folder = playlist_path.rsplit("/", 1)[0]
        listed = {line.strip() for line in data.decode(errors="replace").splitlines()
                  if line.strip() and not line.startswith("#")}
        if not listed:
            return
        mine = sorted((k for k in self.files if k.startswith(folder + "/") and k.endswith(".ts")),
                      key=lambda k: int("".join(ch for ch in k.rsplit("/", 1)[1] if ch.isdigit()) or 0))
        old = [k for k in mine if k.rsplit("/", 1)[1] not in listed]
        for k in old[:-3]:
            self.files.pop(k, None)

    def get(self, path):
        with self.lock:
            return self.files.get(path)

    def clear(self, folder):
        with self.lock:
            for k in [k for k in self.files if k.startswith(folder + "/")]:
                del self.files[k]


class Stream:
    """One live stream: pictures from a camera (or all of them) made into HLS by FFmpeg."""

    def __init__(self, tv, source):
        self.tv = tv
        self.source = source
        self.folder = f"{source}-{secrets.token_hex(4)}"
        self.last_asked = time.monotonic()
        self.stopped = threading.Event()
        self.proc = None
        self.error = ""
        threading.Thread(target=self._run, name=f"tv-{source}", daemon=True).start()

    def _cmd(self):
        from .camera import FFMPEG
        return [FFMPEG, "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "main",
                "-pix_fmt", "yuv420p", "-g", str(FPS * SEGMENT), "-keyint_min", str(FPS * SEGMENT),
                "-sc_threshold", "0", "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "3000k", "-threads", "2",
                "-f", "hls", "-hls_time", str(SEGMENT), "-hls_list_size", "6",
                "-hls_flags", "delete_segments+omit_endlist+independent_segments",
                "-hls_segment_filename", f"http://127.0.0.1:{self.tv.pieces.port}/{self.folder}/piece%d.ts",
                "-method", "PUT", "-http_persistent", "0",
                f"http://127.0.0.1:{self.tv.pieces.port}/{self.folder}/live.m3u8"]

    def _run(self):
        from .camera import NO_WINDOW

        try:
            self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except OSError as e:
            self.error = f"Couldn't start FFmpeg: {e}"
            log.warning("TV stream: %s", self.error)
            self.stopped.set()
            return
        log.info("TV stream for %s started", self.source)
        next_at = time.monotonic()
        try:
            while not self.stopped.is_set():
                if time.monotonic() - self.last_asked > IDLE_STOP:
                    log.info("TV stream for %s stopped (nobody watching)", self.source)
                    break
                frame = self.tv.picture(self.source)
                try:
                    self.proc.stdin.write(frame.tobytes())
                except (OSError, ValueError):
                    err = self.proc.stderr.read().decode(errors="replace").strip()
                    self.error = "FFmpeg stopped: " + err[-300:]
                    log.warning("TV stream for %s: %s", self.source, self.error)
                    break
                next_at += 1.0 / FPS
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.monotonic()
        finally:
            self.stopped.set()
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.tv.pieces.clear(self.folder)

    def stop(self):
        self.stopped.set()


class TV:
    def __init__(self, hub):
        self.hub = hub
        self._pieces = None
        self.keys = {}            # key -> last used (time.time())
        self.streams = {}         # source -> Stream
        self.lock = threading.Lock()

    @property
    def pieces(self):
        if self._pieces is None:
            self._pieces = Pieces()
        return self._pieces

    # ---- keys for TVs ---------------------------------------------------------------

    def new_key(self):
        self._expire()
        key = secrets.token_urlsafe(18)
        self.keys[key] = time.time()
        return key

    def check_key(self, key):
        self._expire()
        if key in self.keys:
            self.keys[key] = time.time()
            return True
        return False

    def _expire(self):
        now = time.time()
        for k, t in list(self.keys.items()):
            if now - t > KEY_LIFE:
                del self.keys[k]

    # ---- streams ----------------------------------------------------------------------

    def sources(self):
        cams = [{"id": c.id, "name": c.cfg.get("name") or c.id} for c in self.hub.cameras.values()]
        return ([{"id": "all", "name": "All cameras"}] if len(cams) > 1 else []) + cams

    def stream(self, source):
        """The running stream for a source, started if needed."""
        if source != "all" and source not in self.hub.cameras:
            return None
        with self.lock:
            s = self.streams.get(source)
            if s is None or s.stopped.is_set():
                s = self.streams[source] = Stream(self, source)
            s.last_asked = time.monotonic()
            return s

    def stop_all(self):
        with self.lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()

    def picture(self, source):
        """The newest picture for a stream, sized for the TV."""
        import numpy as np

        if source != "all":
            cam = self.hub.cameras.get(source)
            return place(frame_of(cam), WIDTH, HEIGHT, cam.cfg.get("name") if cam else source, False)
        cams = list(self.hub.cameras.values())
        out = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        if not cams:
            return out
        cols = math.ceil(math.sqrt(len(cams)))
        rows = math.ceil(len(cams) / cols)
        w, h = WIDTH // cols, HEIGHT // rows
        for i, cam in enumerate(cams):
            r, c = divmod(i, cols)
            out[r * h:(r + 1) * h, c * w:(c + 1) * w] = place(frame_of(cam), w, h, cam.cfg.get("name") or cam.id, True)
        return out


def lan_ip():
    """This PC's address on the home network (no traffic is sent)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 9))
        ip = s.getsockname()[0]
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""
    finally:
        s.close()


def frame_of(cam):
    if cam is None:
        return None
    with cam._frame_cond:
        return cam._frame


def place(frame, w, h, name, label):
    """Fits a picture into w x h with black bars; says so if there's no picture yet."""
    import cv2
    import numpy as np

    out = np.zeros((h, w, 3), np.uint8)
    if frame is None or frame.size == 0:
        msg = f"{name}: no picture" if name else "No picture"
        (tw, _), _ = cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cv2.putText(out, msg, ((w - tw) // 2, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (170, 170, 170), 2, cv2.LINE_AA)
        return out
    fh, fw = frame.shape[:2]
    scale = min(w / fw, h / fh)
    nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
    small = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    x, y = (w - nw) // 2, (h - nh) // 2
    out[y:y + nh, x:x + nw] = small
    if label and name:
        cv2.putText(out, name, (x + 10, y + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(out, name, (x + 10, y + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def add_routes(app, hub):
    import asyncio

    from fastapi import HTTPException, Request
    from fastapi.responses import FileResponse, Response

    import os

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    tv = hub.tv
    cors = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}

    @app.get("/tv")
    def tv_page():
        return FileResponse(os.path.join(static_dir, "tv.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/api/tv/info")
    def tv_info(request: Request):
        """The /tv address other devices at home can use (this PC's address on the home network)."""
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        ip = lan_ip()
        default = (request.url.scheme == "https" and port == 443) or (request.url.scheme == "http" and port == 80)
        return {"lan_url": f"{request.url.scheme}://{ip}{'' if default else ':' + str(port)}/tv" if ip else ""}

    @app.post("/api/tv/link")
    def tv_link():
        """A key a Chromecast can use instead of signing in. Returns the paths to give it."""
        key = tv.new_key()
        return {"key": key, "sources": [dict(s, live=f"/tv/{key}/{s['id']}/live.m3u8") for s in tv.sources()],
                "summary": f"/tv/{key}/summary/"}

    def need_key(key):
        if not tv.check_key(key):
            raise HTTPException(403, "This TV link has expired. Cast again from the app.")

    @app.get("/tv/{key}/{source}/live.m3u8")
    async def tv_playlist(key: str, source: str):
        need_key(key)
        s = tv.stream(source)
        if s is None:
            raise HTTPException(404, "No such camera")
        # The first pieces take a few seconds to appear.
        for _ in range(100):
            data = tv.pieces.get(f"{s.folder}/live.m3u8")
            if data and data.count(b"#EXTINF") >= 2:
                break
            if s.stopped.is_set():
                raise HTTPException(503, s.error or "The stream stopped")
            await asyncio.sleep(0.15)
        else:
            raise HTTPException(503, "The stream didn't start in time")
        # Relative piece names in the playlist need this stream's folder in front.
        text = data.decode(errors="replace")
        lines = [ln if (not ln.strip() or ln.startswith("#")) else f"p/{s.folder}/{ln.strip()}" for ln in text.splitlines()]
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl", headers=cors)

    @app.get("/tv/{key}/{source}/p/{folder}/{name}")
    def tv_piece(key: str, source: str, folder: str, name: str):
        need_key(key)
        s = tv.streams.get(source)
        if s is not None:
            s.last_asked = time.monotonic()
        data = tv.pieces.get(f"{folder}/{name}")
        if data is None:
            raise HTTPException(404, "Gone")
        return Response(data, media_type="video/mp2t", headers=cors)

    @app.get("/tv/{key}/summary/{date}.mp4")
    def tv_summary(key: str, date: str):
        need_key(key)
        p = hub.summaries.path(date, ".mp4") if getattr(hub, "summaries", None) else None
        if not p or not os.path.isfile(p):
            raise HTTPException(404, "No summary")
        return FileResponse(p, media_type="video/mp4", headers=cors)

    @app.get("/tv/{key}/summary/{date}.jpg")
    def tv_summary_picture(key: str, date: str):
        need_key(key)
        p = hub.summaries.path(date, ".jpg") if getattr(hub, "summaries", None) else None
        if not p or not os.path.isfile(p):
            raise HTTPException(404, "No picture")
        return FileResponse(p, media_type="image/jpeg", headers=cors)
