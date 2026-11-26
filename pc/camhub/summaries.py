"""Night summaries: each morning, one short sped-up video of everything recorded overnight.

The night's recordings from every camera are played back to back, faster than real time, with
the camera name and time written on the picture. Gaps between recordings are skipped, so a night
with a fox at 02:10 and the post at 06:55 becomes a video of a few seconds.

Settings live in config.json under "night_summary":
    {"enabled": true, "start": "21:00", "end": "07:00", "ready": "07:30",
     "seconds": 60, "keep_days": 30}
Files: <storage>/_summaries/<YYYY-MM-DD>.mp4 with a .jpg picture and a .json of details. The
date is the morning the summary was made for. The folder name starts with "_" so it is never
mistaken for a camera.
"""
import datetime
import json
import logging
import math
import os
import re
import subprocess
import threading
import time

log = logging.getLogger("camhub.summaries")

DEFAULTS = {"enabled": True, "start": "21:00", "end": "07:00", "ready": "07:30", "seconds": 60, "keep_days": 30}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
FOLDER = "_summaries"
WIDTH, HEIGHT, FPS = 1280, 720, 15
MIN_SPEED, MAX_SPEED = 4, 120


def clean_settings(b):
    out = dict(DEFAULTS)
    if "enabled" in b:
        out["enabled"] = bool(b["enabled"])
    for k in ("start", "end", "ready"):
        v = str(b.get(k) or DEFAULTS[k])
        out[k] = v if TIME_RE.match(v) else DEFAULTS[k]
    for k, lo, hi in (("seconds", 15, 600), ("keep_days", 1, 365)):
        try:
            out[k] = max(lo, min(hi, int(b.get(k, DEFAULTS[k]))))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


def night_window(morning, s):
    """The night before a morning (a date): from the evening start time to the morning end time."""
    sh, sm = map(int, s["start"].split(":"))
    eh, em = map(int, s["end"].split(":"))
    end = datetime.datetime.combine(morning, datetime.time(eh, em))
    start = datetime.datetime.combine(morning, datetime.time(sh, sm))
    if start >= end:
        start -= datetime.timedelta(days=1)
    return start, end


def pick_speed(total_seconds, target_seconds):
    """How much faster than real time to play, so the summary lasts about target_seconds."""
    if total_seconds <= 0:
        return MIN_SPEED
    return int(max(MIN_SPEED, min(MAX_SPEED, math.ceil(total_seconds / max(1, target_seconds)))))


class NightSummaries:
    def __init__(self, hub):
        self.hub = hub
        self.config = hub.config
        self.config.data["night_summary"] = clean_settings(self.config.data.get("night_summary") or {})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.busy = None          # the date being made now
        self.progress = 0.0
        self.last_error = ""

    @property
    def settings(self):
        return self.config.data["night_summary"]

    @property
    def folder(self):
        return os.path.join(self.hub.storage.root, FOLDER)

    def path(self, date, ext):
        if not DATE_RE.match(date or ""):
            return None
        return os.path.join(self.folder, date + ext)

    # ---- background -----------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._clock, name="night-summary", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _clock(self):
        # A short wait first, so the cameras are running and a restart doesn't rush into it.
        if self._stop.wait(60):
            return
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Night summary check failed")
            if self._stop.wait(60):
                return

    def tick(self, now=None):
        """Makes this morning's summary once the ready time has passed, and tidies old ones."""
        s = self.settings
        now = now or datetime.datetime.now()
        if not s["enabled"] or now.strftime("%H:%M") < s["ready"]:
            return False
        today = now.date().isoformat()
        if os.path.exists(self.path(today, ".json")) or self.busy:
            return False
        self.make(today)
        self.prune(now.date())
        return True

    def make_in_background(self, date):
        if self.busy:
            return False
        threading.Thread(target=self._make_quietly, args=(date,), name="night-summary-make", daemon=True).start()
        return True

    def _make_quietly(self, date):
        try:
            self.make(date, manual=True)
        except Exception as e:
            log.exception("Night summary for %s failed", date)
            self.last_error = str(e)

    # ---- making one -----------------------------------------------------------------

    def clips_for(self, date):
        start, end = night_window(datetime.date.fromisoformat(date), self.settings)
        out = []
        day = start.date()
        while day <= end.date():
            for c in self.hub.storage.clips(day.isoformat()):
                try:
                    t = datetime.datetime.fromisoformat(c["start"])
                except ValueError:
                    continue
                if start <= t < end:
                    out.append(dict(c, t=t))
            day += datetime.timedelta(days=1)
        out.sort(key=lambda c: c["t"])
        return out, start, end

    def make(self, date, manual=False):
        """Builds the summary for the night before `date`. Returns its details."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A night summary is already being made")
        self.busy, self.progress, self.last_error = date, 0.0, ""
        try:
            return self._make(date, manual)
        finally:
            self.busy = None
            self._lock.release()

    def _make(self, date, manual):
        os.makedirs(self.folder, exist_ok=True)
        clips, start, end = self.clips_for(date)
        total = sum(float(c.get("duration") or 30) for c in clips)
        speed = pick_speed(total, self.settings["seconds"])
        info = {"date": date, "from": start.isoformat(timespec="minutes"), "to": end.isoformat(timespec="minutes"),
                "count": len(clips), "people": sum(1 for c in clips if c.get("person")),
                "cameras": sorted({c["camera"] for c in clips}), "speed": speed, "seconds": 0,
                "made": datetime.datetime.now().isoformat(timespec="seconds"), "manual": manual,
                "chapters": [], "video": False}
        for ext in (".mp4", ".jpg"):
            try:
                os.remove(self.path(date, ext))
            except OSError:
                pass
        if clips:
            log.info("Making the night summary for %s: %d recordings, %d s, %dx", date, len(clips), total, speed)
            started = time.monotonic()
            chapters, seconds = self._render(date, clips, speed, start, end)
            info.update(chapters=chapters, seconds=round(seconds, 1), video=seconds > 0)
            log.info("Night summary for %s done in %d s (%.0f s long)", date, time.monotonic() - started, seconds)
        tmp = self.path(date, ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
        os.replace(tmp, self.path(date, ".json"))
        self.hub.events.publish({"type": "night_summary", "date": date, "count": info["count"],
                                 "people": info["people"], "seconds": info["seconds"], "text": headline(info)})
        return info

    def _render(self, date, clips, speed, start, end):
        import cv2
        import numpy as np

        from . import privacy
        from .camera import FFMPEG, NO_WINDOW

        part = self.path(date, ".mp4.part")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", "-threads", "2",
               "-movflags", "+faststart", "-f", "mp4", part]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                creationflags=NO_WINDOW)
        written = 0
        chapters = []
        thumb = None
        cam_names = {c["id"]: c.get("name") or c["id"] for c in self.config.cameras}

        def put(frame):
            nonlocal written
            proc.stdin.write(frame.tobytes())
            written += 1

        try:
            # A title card for the first second.
            card = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
            people = sum(1 for c in clips if c.get("person"))
            title = f"Night of {start:%a %d %b}"
            sub = (f"{start:%H:%M} to {end:%H:%M}  ·  {len(clips)} recording{'s' if len(clips) != 1 else ''}"
                   + (f", {people} with people" if people else ""))
            text(card, title, (WIDTH // 2, HEIGHT // 2 - 20), 1.6, centre=True)
            text(card, sub.replace("·", "-"), (WIDTH // 2, HEIGHT // 2 + 40), 0.9, centre=True)
            for _ in range(FPS):
                put(card)

            step = speed / FPS          # seconds of recording per frame of summary
            for i, c in enumerate(clips):
                base = self.hub.storage.clip_base(c["id"])
                if not base or not os.path.isfile(base + ".mp4"):
                    continue
                cam_cfg = self.config.camera(c["camera"]) or {}
                mask = privacy.settings_of(cam_cfg)
                cap = cv2.VideoCapture(base + ".mp4")
                try:
                    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15
                    if not (1 <= src_fps <= 120):
                        src_fps = 15
                    chapters.append({"t": round(written / FPS, 2), "clip": c["id"], "camera": c["camera"],
                                     "start": c["start"], "person": bool(c.get("person"))})
                    n, want, got_any = 0, 0.0, False
                    name = cam_names.get(c["camera"], c["camera"])
                    while True:
                        if not cap.grab():
                            break
                        at = n / src_fps
                        n += 1
                        if at + 1e-6 < want:
                            continue
                        ok, frame = cap.retrieve()
                        if not ok or frame is None or frame.size == 0:
                            continue
                        want += step
                        frame = privacy.apply_to_frame(frame, mask)
                        frame = fit(frame)
                        if thumb is None and (c.get("person") or i == len(clips) // 2):
                            thumb = frame.copy()
                        clock = c["t"] + datetime.timedelta(seconds=at)
                        text(frame, f"{name}  {clock:%a %H:%M:%S}", (16, 40), 0.9)
                        text(frame, f"{speed}x", (WIDTH - 16, 40), 0.9, right=True)
                        put(frame)
                        got_any = True
                    if not got_any:
                        chapters.pop()
                        log.warning("Night summary: couldn't read %s", c["id"])
                finally:
                    cap.release()
                self.progress = (i + 1) / len(clips)
                if self._stop.is_set():
                    raise RuntimeError("Stopped")
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait(timeout=600) != 0:
                raise RuntimeError("FFmpeg couldn't make the video: " + err.strip()[-300:])
        except BaseException:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        if not chapters:
            os.remove(part)
            return [], 0
        os.replace(part, self.path(date, ".mp4"))
        if thumb is not None:
            ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                with open(self.path(date, ".jpg"), "wb") as f:
                    f.write(buf.tobytes())
        return chapters, written / FPS

    # ---- the list ---------------------------------------------------------------------

    def items(self):
        out = []
        try:
            names = sorted((f for f in os.listdir(self.folder) if f.endswith(".json")), reverse=True)
        except OSError:
            return out
        for f in names:
            info = self.get(f[:-5])
            if info:
                info = {k: v for k, v in info.items() if k != "chapters"}
                info["text"] = headline(info)
                out.append(info)
        return out

    def get(self, date):
        p = self.path(date, ".json")
        if not p:
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return None
        info["video"] = bool(info.get("video")) and os.path.isfile(self.path(date, ".mp4"))
        info["picture"] = os.path.isfile(self.path(date, ".jpg"))
        return info

    def delete(self, date):
        found = False
        for ext in (".mp4", ".jpg", ".json"):
            p = self.path(date, ext)
            if p and os.path.exists(p):
                os.remove(p)
                found = True
        return found

    def prune(self, today=None):
        today = today or datetime.date.today()
        cutoff = (today - datetime.timedelta(days=self.settings["keep_days"])).isoformat()
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for f in names:
            date = f.split(".")[0]
            if DATE_RE.match(date) and date < cutoff:
                try:
                    os.remove(os.path.join(self.folder, f))
                except OSError:
                    pass


def headline(info):
    n = info.get("count") or 0
    if not n:
        return "Quiet night: nothing was recorded."
    people = info.get("people") or 0
    secs = round(info.get("seconds") or 0)
    s = f"{n} recording{'s' if n != 1 else ''} overnight"
    if people:
        s += f", {people} with people"
    if secs:
        s += f". Watch it all in {secs // 60} min {secs % 60} s" if secs >= 60 else f". Watch it all in {secs} s"
    return s + "."


def fit(frame):
    """Scales a picture to fit the summary, with black bars if its shape is different."""
    import cv2
    import numpy as np

    h, w = frame.shape[:2]
    if (w, h) == (WIDTH, HEIGHT):
        return frame
    scale = min(WIDTH / w, HEIGHT / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    small = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    if (nw, nh) == (WIDTH, HEIGHT):
        return small
    out = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    x, y = (WIDTH - nw) // 2, (HEIGHT - nh) // 2
    out[y:y + nh, x:x + nw] = small
    return out


def text(img, s, org, scale, centre=False, right=False):
    """Writes white text with a dark outline, so it shows on any picture."""
    import cv2

    font = cv2.FONT_HERSHEY_SIMPLEX
    thick = max(1, int(round(scale * 2)))
    (tw, th), _ = cv2.getTextSize(s, font, scale, thick)
    x, y = org
    if centre:
        x -= tw // 2
    elif right:
        x -= tw
    cv2.putText(img, s, (x, y), font, scale, (0, 0, 0), thick + 3, cv2.LINE_AA)
    cv2.putText(img, s, (x, y), font, scale, (255, 255, 255), thick, cv2.LINE_AA)


def add_routes(app, hub):
    from fastapi import HTTPException, Request
    from fastapi.responses import FileResponse

    ns = hub.summaries

    def need(date, ext):
        p = ns.path(date, ext)
        if not p or not os.path.isfile(p):
            raise HTTPException(404, "No summary for that morning")
        return p

    @app.get("/api/summaries")
    def list_summaries():
        return {"settings": ns.settings, "items": ns.items(), "busy": ns.busy,
                "progress": round(ns.progress, 2), "error": ns.last_error}

    @app.put("/api/summaries/settings")
    async def put_summary_settings(request: Request):
        body = await request.json()
        merged = dict(ns.settings)
        merged.update(body or {})
        hub.config.data["night_summary"] = clean_settings(merged)
        hub.config.save()
        return hub.config.data["night_summary"]

    @app.post("/api/summaries/make")
    async def make_summary(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        date = str((body or {}).get("date") or datetime.date.today().isoformat())
        if not DATE_RE.match(date):
            raise HTTPException(400, "Bad date")
        if not ns.make_in_background(date):
            raise HTTPException(409, "A summary is already being made. Try again in a minute.")
        return {"started": True, "date": date}

    @app.get("/api/summaries/{date}")
    def get_summary(date: str):
        info = ns.get(date)
        if not info:
            raise HTTPException(404, "No summary for that morning")
        info["text"] = headline(info)
        return info

    @app.get("/api/summaries/{date}/video")
    def summary_video(date: str, download: int = 0):
        p = need(date, ".mp4")
        headers = {"Cache-Control": "private, max-age=3600"}
        if download:
            return FileResponse(p, media_type="video/mp4", filename=f"night-{date}.mp4", headers=headers)
        return FileResponse(p, media_type="video/mp4", headers=headers)

    @app.get("/api/summaries/{date}/picture.jpg")
    def summary_picture(date: str):
        return FileResponse(need(date, ".jpg"), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.delete("/api/summaries/{date}")
    def delete_summary(date: str):
        if not ns.delete(date):
            raise HTTPException(404, "No summary for that morning")
        return {"ok": True}
