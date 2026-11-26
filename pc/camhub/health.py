"""Health alerts: tell the phone when something needs looking at.

- a camera has been offline for a few minutes (and again when it's back)
- the recordings disk is nearly full, or the recordings folder can't be reached
- a recording couldn't be saved

These are sent whether you're home or away. The phone also warns by itself if it can't reach
the PC at all (for example after a power cut), since the PC can't send anything then.

Settings (config.json, "health"):
  camera_offline_minutes  how long a camera must be offline before an alert (0 turns it off)
  disk_alerts             warn about disk space and the recordings folder
  recording_alerts        warn when a recording couldn't be saved
"""
import logging
import os
import queue
import shutil
import threading
import time

log = logging.getLogger("camhub.health")

DEFAULT_HEALTH = {
    "camera_offline_minutes": 5,
    "disk_alerts": True,
    "recording_alerts": True,
}
GB = 1024 ** 3


def low_disk_threshold(min_free_gb):
    """Free space below which we warn. Old clips are deleted to keep `min_free_gb` free,
    so falling well below it means something else is filling the disk."""
    return max(2.0, float(min_free_gb or 0) / 2) * GB


class HealthMonitor:
    CHECK_SECONDS = 30
    DISK_CHECK_SECONDS = 300

    def __init__(self, hub, clock=time.monotonic):
        self.hub = hub
        self.config = hub.config
        self.clock = clock
        cur = self.config.data.get("health") or {}
        merged = dict(DEFAULT_HEALTH)
        merged.update({k: v for k, v in cur.items() if k in DEFAULT_HEALTH})
        self.config.data["health"] = merged
        self._lock = threading.RLock()
        self.problems = {}           # key -> {"key", "kind", "camera", "title", "message", "since"}
        self._offline_since = {}     # camera id -> clock time it went offline
        self._last_disk = None
        self._stop = threading.Event()
        self._events = None
        self._started_at = time.time()

    @property
    def settings(self):
        return self.config.data["health"]

    # ---- running -----------------------------------------------------------------

    def start(self):
        self._events = self.hub.events.subscribe()
        threading.Thread(target=self._loop, name="health", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._events is not None:
            self.hub.events.unsubscribe(self._events)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._drain_events()
                self.check()
                presence = getattr(self.hub, "presence", None)
                if presence is not None:
                    presence.check()
            except Exception:
                log.exception("Health check failed")
            self._stop.wait(self.CHECK_SECONDS)

    def _drain_events(self):
        while self._events is not None:
            try:
                e = self._events.get_nowait()
            except queue.Empty:
                return
            self.on_event(e)

    # ---- checks ------------------------------------------------------------------

    def check(self):
        self.check_cameras()
        now = self.clock()
        if self._last_disk is None or now - self._last_disk >= self.DISK_CHECK_SECONDS:
            self._last_disk = now
            self.check_disk()

    def check_cameras(self):
        now = self.clock()
        minutes = float(self.settings.get("camera_offline_minutes") or 0)
        cams = dict(self.hub.cameras)
        for cam_id in list(self._offline_since):
            if cam_id not in cams:
                self._offline_since.pop(cam_id, None)
                self._clear(f"offline:{cam_id}", quiet=True)
        for cam_id, cam in cams.items():
            st = cam.status()
            key = f"offline:{cam_id}"
            if st.get("online"):
                self._offline_since.pop(cam_id, None)
                self._clear(key, title=f"{st['name']} is back online",
                            message="The camera's video is coming through again.")
                continue
            since = self._offline_since.setdefault(cam_id, now)
            if minutes > 0 and now - since >= minutes * 60:
                mins = int((now - since) // 60)
                took = "1 minute" if mins == 1 else f"{mins} minutes"
                why = st.get("problem") or "It isn't sending video."
                self._raise(key, "camera_offline", cam_id,
                            f"{st['name']} is offline",
                            f"No video for {took}, so nothing is being recorded. {why}")
            elif minutes <= 0:
                self._clear(key, quiet=True)

    def check_disk(self):
        if not self.settings.get("disk_alerts", True):
            self._clear("disk:folder", quiet=True)
            self._clear("disk:low", quiet=True)
            return
        root = self.hub.storage.root
        try:
            usage = shutil.disk_usage(root)
            reachable = os.path.isdir(root) and os.access(root, os.W_OK)
        except OSError:
            usage, reachable = None, False
        if not reachable:
            self._raise("disk:folder", "disk_folder", None, "Recordings can't be saved",
                        f"The recordings folder {root} can't be reached or written to. If it's on a "
                        "USB or network drive, check it's plugged in and switched on.")
            return
        self._clear("disk:folder", title="Recordings folder is back",
                    message="Recordings can be saved again.")
        limit = low_disk_threshold(self.config.storage.get("min_free_gb"))
        free_gb = usage.free / GB
        if usage.free < limit:
            self._raise("disk:low", "disk_low", None, "Disk nearly full",
                        f"Only {free_gb:.1f} GB is free on the recordings disk. Old clips are being "
                        "deleted to make room, so you may lose older recordings. Free up space on the PC "
                        "or keep recordings for fewer days.")
        elif usage.free >= limit * 1.2:  # a little gap so it doesn't flap on and off
            self._clear("disk:low", title="Disk space is fine again",
                        message=f"{free_gb:.1f} GB is free on the recordings disk.")

    def on_event(self, e):
        t = e.get("type")
        cam_id = e.get("camera")
        if t == "clip_failed" and self.settings.get("recording_alerts", True):
            cam = self.hub.cameras.get(cam_id)
            name = (cam.cfg.get("name") if cam else None) or cam_id or "A camera"
            self._raise(f"recording:{cam_id}", "recording_failed", cam_id,
                        f"A recording on {name} wasn't saved",
                        "The video couldn't be written to the disk. Check the recordings disk has "
                        "space and is working.")
        elif t == "clip_saved":
            self._clear(f"recording:{cam_id}", title="Recordings are saving again",
                        message="The last recording was saved properly.")

    # ---- problems --------------------------------------------------------------------

    def _raise(self, key, kind, cam_id, title, message):
        with self._lock:
            if key in self.problems:
                # Keep the wording fresh in the viewer (minutes offline), but don't alert again.
                self.problems[key].update(title=title, message=message)
                return
            self.problems[key] = {"key": key, "kind": kind, "camera": cam_id, "title": title,
                                  "message": message, "since": time.time()}
        log.warning("Health: %s. %s", title, message)
        self.hub.events.publish({"type": "health", "state": "problem", "key": key, "kind": kind,
                                 "camera": cam_id, "title": title, "message": message})

    def _clear(self, key, title=None, message=None, quiet=False):
        with self._lock:
            p = self.problems.pop(key, None)
        if p is None:
            return
        log.info("Health: %s is fixed", p["title"])
        if quiet:
            self.hub.events.publish({"type": "health", "state": "cleared", "key": key, "kind": p["kind"],
                                     "camera": p["camera"], "title": "", "message": ""})
            return
        self.hub.events.publish({"type": "health", "state": "ok", "key": key, "kind": p["kind"],
                                 "camera": p["camera"], "title": title or f"{p['title']}: fixed",
                                 "message": message or ""})

    def status(self):
        with self._lock:
            return sorted(self.problems.values(), key=lambda p: p["since"])

    def update(self, body):
        s = self.settings
        if "camera_offline_minutes" in body:
            try:
                s["camera_offline_minutes"] = max(0, min(1440, int(body["camera_offline_minutes"])))
            except (TypeError, ValueError):
                raise ValueError("Minutes must be a whole number")
        for k in ("disk_alerts", "recording_alerts"):
            if k in body:
                s[k] = bool(body[k])
        self.config.save()
        self._last_disk = None  # check the disk again on the next pass
        return dict(s)
