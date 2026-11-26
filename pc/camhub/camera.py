"""One camera: reads its streams, watches for motion and people, records clips.

Two background threads per camera:
- StreamReader copies the main (high quality) stream through ffmpeg as MPEG-TS and
  keeps the last few seconds in memory. When a clip is open it also feeds that clip.
- Analyzer decodes the small sub stream with OpenCV, looks for motion outside the
  ignore zones and, if asked, checks for people. It also serves the live picture.

Nothing is written to disk unless a clip is being recorded.
"""
import datetime
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

from . import privacy
from .tsbuffer import PreRollBuffer, TsPacketizer, TsStream

log = logging.getLogger("camhub.camera")

FFMPEG = os.environ.get("CAMHUB_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # hide console windows on Windows


def redact(text):
    """Hides camera passwords before anything goes in the log."""
    text = re.sub(r"(rtsps?://)[^/@\s]+@", r"\1****@", text)
    return re.sub(r"(password=)[^&_\s]+", r"\1****", text, flags=re.I)


def _is_rtsp(url):
    return url.lower().startswith(("rtsp://", "rtsps://"))


def _input_args(url, wallclock=False):
    # Some cameras (Hiseeu/XMeye) send packets without timestamps, which MPEG-TS can't
    # hold. Stamping them with the PC's clock fixes that.
    extra = ["-use_wallclock_as_timestamps", "1"] if wallclock else []
    if _is_rtsp(url):
        return ["-rtsp_transport", "tcp", "-timeout", "10000000", *extra, "-i", url]
    # Anything else (a test video file) is played in real time, on a loop.
    return ["-re", "-stream_loop", "-1", *extra, "-i", url]


def _plain_error(text):
    """Turns an ffmpeg error line into something a person can act on."""
    text = re.sub(r"^\[[^\]]*\]\s*", "", text)
    low = text.lower()
    if "401" in low or "unauthorized" in low:
        return "the camera refused the user name or password."
    if "connection refused" in low:
        return "the camera refused the connection. Check its IP address."
    if "timed out" in low or "timeout" in low:
        return "the camera didn't answer in time. Check it's on and connected."
    if "pts" in low or "mux" in low:
        return "the camera's video has missing timestamps. Trying another way to read it."
    return text[:200]


class FfmpegCapture:
    """Decodes a stream with our own ffmpeg, for when OpenCV can't open it itself.

    Offers the parts of cv2.VideoCapture the analyzer uses: read(), get(), release().
    """

    MAX_WIDTH = 960

    def __init__(self, url):
        from .discovery import probe

        self.proc = None
        info = probe(url, timeout=10) if _is_rtsp(url) else self._probe_file(url)
        if not info:
            return
        w, h = info[0], info[1]
        if w > self.MAX_WIDTH:
            h = int(h * self.MAX_WIDTH / w) // 2 * 2
            w = self.MAX_WIDTH
        self.size = (w, h)
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", *_input_args(url, wallclock=_is_rtsp(url)),
               "-map", "0:v:0", "-an",
               "-vf", f"scale={w}:{h}", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError:
            self.proc = None
            return
        threading.Thread(target=self._log_stderr, daemon=True).start()

    def _log_stderr(self):
        for line in self.proc.stderr:
            text = line.decode(errors="replace").strip()
            if text and not text.startswith("Last message repeated"):
                log.warning("ffmpeg (live picture): %s", redact(text))

    @staticmethod
    def _probe_file(path):
        from .discovery import _ffprobe

        try:
            r = subprocess.run([_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", path],
                               capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
            w, h = r.stdout.strip().split(",")[:2]
            return int(w), int(h), None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def isOpened(self):
        return self.proc is not None

    def read(self):
        w, h = self.size
        need = w * h * 3
        buf = bytearray()
        while len(buf) < need:
            chunk = self.proc.stdout.read(need - len(buf))
            if not chunk:
                return False, None
            buf += chunk
        return True, np.frombuffer(bytes(buf), np.uint8).reshape(h, w, 3)

    def get(self, prop):
        return 0

    def set(self, *a):
        return False

    def release(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class ClipWriter:
    """Pipes MPEG-TS into ffmpeg, which writes an MP4 without re-encoding the video."""

    def __init__(self, base_path, clip_id, cam_id, started, trigger, person, thumb_jpeg, header_bytes,
                 started_mono=None, privacy_settings=None):
        self.base = base_path
        self.privacy = privacy_settings  # areas to cover once the clip is finished, or None
        self.clip_id = clip_id
        self.cam_id = cam_id
        self.started = started
        self.started_mono = started_mono or time.monotonic()
        self.trigger = trigger
        self.person = person
        self.part = base_path + ".mp4.part"
        if thumb_jpeg is not None:
            with open(base_path + ".jpg", "wb") as f:
                f.write(thumb_jpeg)
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt", "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            "-f", "mp4", "-y", self.part,
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.q = queue.Queue(maxsize=2000)
        self.closed = False
        self.thread = threading.Thread(target=self._pump, name=f"clip-{clip_id}", daemon=True)
        self.thread.start()
        if header_bytes:
            self.write(header_bytes)

    def write(self, data):
        if self.closed:
            return
        try:
            self.q.put_nowait(data)
        except queue.Full:
            log.warning("Clip writer for %s is falling behind, dropping data", self.clip_id)

    def _pump(self):
        try:
            while True:
                data = self.q.get()
                if data is None:
                    break
                self.proc.stdin.write(data)
        except (BrokenPipeError, OSError, ValueError):
            log.error("Clip writer for %s stopped early", self.clip_id)
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def close(self, on_done=None):
        """Finishes the file in the background, then calls on_done(meta or None)."""
        if self.closed:
            return
        self.closed = True
        duration = round(time.monotonic() - self.started_mono, 1)
        self.q.put(None)

        def finish():
            self.thread.join(timeout=30)
            try:
                _, err = self.proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                _, err = self.proc.communicate()
            meta = None
            if self.proc.returncode == 0 and os.path.exists(self.part) and os.path.getsize(self.part) > 0:
                masked = self._cover_privacy_areas()
                os.replace(self.part, self.base + ".mp4")
                meta = {
                    "id": self.clip_id,
                    "camera": self.cam_id,
                    "start": self.started.isoformat(timespec="seconds"),
                    "duration": duration,
                    "person": self.person,
                    "trigger": self.trigger,
                }
                if masked:
                    meta["privacy_masked"] = True
                with open(self.base + ".json", "w", encoding="utf-8") as f:
                    json.dump(meta, f)
                log.info("Saved clip %s (%.0f s)", self.clip_id, duration)
            else:
                log.error("Clip %s failed: %s", self.clip_id, (err or b"").decode(errors="replace")[-500:])
                for p in (self.part, self.base + ".jpg"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            if on_done:
                on_done(meta)

        threading.Thread(target=finish, name=f"finish-{self.clip_id}", daemon=True).start()

    def _cover_privacy_areas(self):
        """Makes the finished clip again with the privacy areas covered. True if it did."""
        if not self.privacy:
            return False
        tmp = self.base + ".private.mp4.part"
        if privacy.mask_clip(self.part, tmp, self.privacy):
            os.replace(tmp, self.part)
            log.info("Covered the privacy areas in clip %s", self.clip_id)
            return True
        log.error("Clip %s is kept without its privacy areas covered", self.clip_id)
        return False

    def mark_person(self):
        self.person = True


class Camera:
    ANALYZE_FPS = 5
    LIVE_FPS = 10

    def __init__(self, cfg, storage, events, detector, ptz=None):
        self.cfg = cfg  # this camera's dict inside the shared config (edited in place)
        self.id = cfg["id"]
        self.storage = storage
        self.events = events
        self.detector = detector
        self.ptz = ptz

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.ts = TsStream()
        self.preroll = PreRollBuffer(self.motion["pre_seconds"])
        self.clip = None
        self.last_activity = 0.0
        self.stream_online = False
        self.analyzer_online = False
        self.stream_problem = None    # why the main stream isn't running, shown in the viewer
        self.analyzer_problem = None
        self._use_ffmpeg_decoder = False
        self._connecting_since = None

        self._frame = None          # latest sub stream frame (BGR)
        self._frame_id = 0
        self._frame_cond = threading.Condition()
        self._jpeg_cache = (None, None)
        self._mask = None
        self._mask_key = None
        self.motion_level = 0.0
        self.last_person = None     # (time, boxes)

    @property
    def motion(self):
        return self.cfg["motion"]

    # ---- lifecycle ----------------------------------------------------------

    def start(self):
        self._connecting_since = time.monotonic()
        threading.Thread(target=self._stream_loop, name=f"stream-{self.id}", daemon=True).start()
        threading.Thread(target=self._analyze_loop, name=f"analyze-{self.id}", daemon=True).start()
        if self.ptz is not None:
            threading.Thread(target=self.ptz.connect, name=f"ptz-{self.id}", daemon=True).start()

    def stop(self):
        self._stop.set()
        proc = getattr(self, "_proc", None)
        if proc and proc.poll() is None:
            proc.kill()
        with self._lock:
            if self.clip:
                self._close_clip()

    def status(self):
        with self._lock:
            return {
                "id": self.id,
                "name": self.cfg.get("name") or self.id,
                "online": self.stream_online and self.analyzer_online,
                "stream_online": self.stream_online,
                "analyzer_online": self.analyzer_online,
                "problem": self.problem(),
                "recording": self.clip is not None,
                "recording_clip": self.clip.clip_id if self.clip else None,
                "motion_level": round(self.motion_level, 2),
                "ptz": bool(self.ptz and self.ptz.available),
                "ptz_error": self.ptz.error if self.ptz else "Not configured",
                "person_detection": self.detector.available,
            }

    def problem(self):
        """A short reason the camera is offline, or None."""
        if self.stream_online and self.analyzer_online:
            return None
        if not self.cfg.get("main_url"):
            return "No video address saved yet. Open Settings > Cameras and press Find and save."
        parts = []
        if not self.stream_online and self.stream_problem:
            parts.append("Recording stream: " + self.stream_problem)
        if not self.analyzer_online and self.analyzer_problem:
            parts.append("Live picture: " + self.analyzer_problem)
        if not parts and self._connecting_since and time.monotonic() - self._connecting_since > 30:
            return ("Still waiting for video from the camera after 30 seconds. "
                    "Check it isn't in use by too many apps, and see the log for details.")
        return " ".join(parts) or "Connecting to the camera…"

    # ---- main stream: memory buffer and clip writing ------------------------

    # Ways of reading the main stream, tried in order until one keeps running.
    STREAM_MODES = [
        {"wallclock": False, "audio": True},
        {"wallclock": True, "audio": True},
        {"wallclock": True, "audio": False},
        # Last resort: make new video. Uses more of the PC's processor, but every frame gets a
        # proper timestamp.
        {"wallclock": True, "audio": False, "encode": True},
    ]

    def _stream_cmd(self, url, mode):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", *_input_args(url, mode["wallclock"]),
               "-map", "0:v:0"]
        if mode.get("encode"):
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "23",
                    "-g", "50", "-fps_mode", "passthrough"]
        else:
            cmd += ["-c:v", "copy"]
        if mode["audio"]:
            # Cameras often send G.711 audio, which MP4 can't hold. AAC is tiny to make.
            cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k", "-ar", "16000"]
        else:
            cmd += ["-an"]
        return cmd + ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]

    def _stream_loop(self):
        backoff = 2
        mode_i = 0
        while not self._stop.is_set():
            url = self.cfg.get("main_url")
            if not url:
                time.sleep(5)
                continue
            if url != getattr(self, "_mode_url", None):
                self._mode_url, mode_i = url, 0
            mode = self.STREAM_MODES[mode_i]
            cmd = self._stream_cmd(url, mode)
            self._stream_errors = []
            log.info("[%s] Connecting to main stream%s%s%s", self.id,
                     " (timestamps from the PC clock)" if mode["wallclock"] else "",
                     ", without sound" if not mode["audio"] else "",
                     ", re-encoding the video" if mode.get("encode") else "")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
                self._proc = proc
            except FileNotFoundError:
                self.stream_problem = "ffmpeg is missing. Reinstall Home Camera."
                log.error("ffmpeg was not found. Install it and try again.")
                time.sleep(30)
                continue
            err_thread = threading.Thread(target=self._log_stderr, args=(proc, "main stream"), daemon=True)
            err_thread.start()
            packets = TsPacketizer()
            self.ts = TsStream()
            self.preroll = PreRollBuffer(self.motion["pre_seconds"])
            got_data = False
            started = time.monotonic()
            while not self._stop.is_set():
                data = proc.stdout.read(188 * 256)
                if not data:
                    break
                now = time.monotonic()
                with self._lock:
                    self.preroll.seconds = self.motion["pre_seconds"]
                    for pkt in packets.feed(data):
                        key = self.ts.inspect(pkt)
                        self.preroll.add(pkt, key, now)
                        if self.clip:
                            self.clip.write(pkt)
                    self._check_clip_end(now)
                # Online once real video (a keyframe) has arrived, not just the stream headers.
                if not got_data and self.preroll.gops:
                    got_data = True
                    self.stream_online = True
                    self.stream_problem = None
                    backoff = 2
                    log.info("[%s] Main stream connected", self.id)
            ran_for = time.monotonic() - started
            self.stream_online = False
            proc.kill()
            proc.wait()
            with self._lock:
                if self.clip:
                    self._close_clip()
            err_thread.join(timeout=2)
            if not self._stop.is_set():
                errors = list(self._stream_errors)
                if errors:
                    self.stream_problem = _plain_error(errors[0])
                elif not got_data:
                    self.stream_problem = "the camera sent no video."
                # Failing straight away on timestamps or muxing means this way of reading the
                # stream doesn't suit the camera: try the next one.
                muxing = any("pts" in e or "mux" in e.lower() for e in errors)
                if muxing and ran_for < 30 and mode_i < len(self.STREAM_MODES) - 1:
                    mode_i += 1
                    log.warning("[%s] The camera's stream needs a different setting, trying again", self.id)
                    backoff = 1
                log.warning("[%s] Main stream lost, retrying in %d s", self.id, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _log_stderr(self, proc, what):
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                text = redact(text)
                log.warning("[%s] ffmpeg (%s): %s", self.id, what, text)
                if not text.startswith("Last message repeated"):
                    errs = getattr(self, "_stream_errors", None)
                    if errs is not None and len(errs) < 20:
                        errs.append(text)

    def _start_clip(self, trigger, person, frame, boxes):
        """Called with the lock held."""
        if self.clip or not self.stream_online or not self.preroll.gops:
            return
        started = datetime.datetime.now() - datetime.timedelta(
            seconds=max(0.0, time.monotonic() - self.preroll.gops[0][0]))
        clip_id, base = self.storage.new_clip_paths(self.id, started)
        thumb = None
        if frame is not None:
            img = frame.copy()
            h, w = img.shape[:2]
            for _, (x1, y1, x2, y2) in boxes or []:
                cv2.rectangle(img, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 200, 255), 2)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            thumb = buf.tobytes() if ok else None
        header = self.ts.headers() + self.preroll.snapshot()
        self.clip = ClipWriter(base, clip_id, self.id, started, trigger, person, thumb, header,
                               started_mono=self.preroll.gops[0][0],
                               privacy_settings=privacy.for_recording(self.cfg))
        self.last_activity = time.monotonic()
        log.info("[%s] Recording started (%s)", self.id, trigger)
        self.events.publish({
            "type": "clip_started", "camera": self.id, "camera_name": self.cfg.get("name") or self.id,
            "clip": clip_id, "trigger": trigger, "person": person,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
        })

    def _check_clip_end(self, now):
        if not self.clip:
            return
        quiet_for = now - self.last_activity
        length = now - self.clip.started_mono - self.motion["pre_seconds"]
        if quiet_for > self.motion["post_seconds"]:
            self._close_clip()
        elif length > self.motion["max_clip_minutes"] * 60:
            # Long activity: finish this clip and carry straight on in a new one.
            self._close_clip()
            self._start_clip("continued", False, self._frame, None)

    def _close_clip(self):
        clip = self.clip
        self.clip = None
        if clip is None:
            return
        log.info("[%s] Recording stopped", self.id)

        def done(meta):
            self.events.publish({"type": "clip_saved" if meta else "clip_failed", "camera": self.id,
                                 "clip": clip.clip_id, "meta": meta})

        clip.close(done)

    # ---- sub stream: motion, people and live view ---------------------------

    def _open_capture(self, url):
        """Opens the stream with OpenCV, or with our own ffmpeg if OpenCV can't."""
        if not self._use_ffmpeg_decoder:
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                return cap
            cap.release()
            log.warning("[%s] OpenCV couldn't open the stream, so ffmpeg will decode it instead", self.id)
            self._use_ffmpeg_decoder = True
        cap = FfmpegCapture(url)
        if cap.isOpened():
            return cap
        # Try OpenCV again next time, in case the camera was just busy.
        self._use_ffmpeg_decoder = False
        return None

    def _analyze_loop(self):
        backoff = 2
        while not self._stop.is_set():
            url = self.cfg.get("sub_url") or self.cfg.get("main_url")
            if not url:
                time.sleep(5)
                continue
            cap = self._open_capture(url)
            if cap is None:
                self.analyzer_problem = "couldn't open the camera's video. Check its IP address and login."
                log.warning("[%s] Could not open the stream for motion detection, retrying in %d s", self.id, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            log.info("[%s] Motion detection running", self.id)
            got_frame = False
            is_file = not _is_rtsp(url)
            src_fps = cap.get(cv2.CAP_PROP_FPS) or 25
            if not 1 <= src_fps <= 120:
                src_fps = 25
            bg = None
            bg_key = None
            streak = 0
            last_analyze = 0.0
            last_person_check = 0.0
            failures = 0
            while not self._stop.is_set():
                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    if is_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        failures += 1
                        if failures < 3:
                            continue
                    break
                failures = 0
                # Cover the privacy areas before the picture is used for anything.
                frame = privacy.apply_to_frame(frame, privacy.settings_of(self.cfg))
                if not got_frame:
                    got_frame = True
                    backoff = 2
                    self.analyzer_online = True
                    self.analyzer_problem = None
                with self._frame_cond:
                    self._frame = frame
                    self._frame_id += 1
                    self._frame_cond.notify_all()
                now = time.monotonic()
                if now - last_analyze >= 1.0 / self.ANALYZE_FPS:
                    last_analyze = now
                    m = self.motion
                    key = (m["sensitivity"],)
                    if bg is None or key != bg_key:
                        var = float(np.interp(m["sensitivity"], [1, 10], [60, 8]))
                        bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=var, detectShadows=False)
                        bg_key = key
                        streak = 0
                    level = self._motion_level(bg, frame)
                    self.motion_level = level
                    moving = level >= float(m["min_area_percent"])
                    streak = streak + 1 if moving else 0
                    if streak >= 2:
                        self._on_motion(frame, now, last_person_check)
                        if self.detector.available and now - last_person_check >= 1.0:
                            last_person_check = now
                if is_file:
                    delay = 1.0 / src_fps - (time.monotonic() - t0)
                    if delay > 0:
                        time.sleep(delay)
            cap.release()
            self.analyzer_online = False
            if not got_frame:
                self.analyzer_problem = "the camera's video opened but no pictures could be decoded."
                if not isinstance(cap, FfmpegCapture):
                    log.warning("[%s] OpenCV gave no pictures, so ffmpeg will decode it instead", self.id)
                    self._use_ffmpeg_decoder = True
            if not self._stop.is_set():
                log.warning("[%s] Motion stream lost, retrying in %d s", self.id, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _zone_mask(self, shape):
        zones = self.motion.get("ignore_zones") or []
        key = (shape, json.dumps(zones, sort_keys=True))
        if key != self._mask_key:
            h, w = shape
            mask = np.full((h, w), 255, np.uint8)
            for z in zones:
                pts = np.array([[int(x * w), int(y * h)] for x, y in z.get("points", [])], np.int32)
                if len(pts) >= 3:
                    cv2.fillPoly(mask, [pts], 0)
            self._mask = mask
            self._mask_key = key
        return self._mask

    def _motion_level(self, bg, frame):
        h, w = frame.shape[:2]
        small_w = 320
        small = cv2.resize(frame, (small_w, max(1, int(h * small_w / w))), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        fg = bg.apply(gray)
        mask = self._zone_mask(gray.shape)
        fg = cv2.bitwise_and(fg, mask)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        area = max(1, int(np.count_nonzero(mask)))
        return 100.0 * np.count_nonzero(fg) / area

    def _in_ignore_zone(self, box):
        x1, y1, x2, y2 = box
        # Use the feet (bottom middle of the box): that is where the person stands.
        px, py = (x1 + x2) / 2, y2
        for z in self.motion.get("ignore_zones") or []:
            pts = np.array(z.get("points", []), np.float32)
            if len(pts) >= 3 and cv2.pointPolygonTest(pts, (float(px), float(py)), False) >= 0:
                return True
        return False

    def _find_people(self, frame):
        found = self.detector.detect(frame, float(self.motion.get("person_confidence", 0.5)))
        return [d for d in found if not self._in_ignore_zone(d[1])]

    def _on_motion(self, frame, now, last_person_check):
        m = self.motion
        with self._lock:
            if self.clip is not None:
                self.last_activity = now
        check_person = self.detector.available and now - last_person_check >= 1.0
        people = self._find_people(frame) if check_person else []
        if people:
            self.last_person = (now, people)
        with self._lock:
            if self.clip:
                self.last_activity = now
                if people and not self.clip.person:
                    self.clip.mark_person()
                    self.events.publish({"type": "person", "camera": self.id,
                                         "camera_name": self.cfg.get("name") or self.id,
                                         "clip": self.clip.clip_id,
                                         "time": datetime.datetime.now().isoformat(timespec="seconds")})
                return
            if m.get("person_only") and self.detector.available:
                if people:
                    self._start_clip("person", True, frame, people)
            else:
                self._start_clip("person" if people else "motion", bool(people), frame, people)

    # ---- live view ----------------------------------------------------------

    def latest_jpeg(self, quality=75):
        with self._frame_cond:
            frame, fid = self._frame, self._frame_id
        if frame is None or frame.size == 0:
            return None, 0
        cached_id, cached = self._jpeg_cache
        if cached_id == fid:
            return cached, fid
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None, fid
        data = buf.tobytes()
        self._jpeg_cache = (fid, data)
        return data, fid

    def wait_frame(self, after_id, timeout=2.0):
        with self._frame_cond:
            self._frame_cond.wait_for(lambda: self._frame_id != after_id, timeout=timeout)
