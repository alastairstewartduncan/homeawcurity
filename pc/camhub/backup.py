"""Copies recordings to an off-site folder, such as OneDrive or Google Drive, so they
survive if the PC is stolen or broken.

The recorder copies each clip into "<chosen folder>/Home Camera recordings/<camera>/<day>/"
and the OneDrive or Google Drive app on the PC uploads it from there. This needs no
sign-in inside A.S.D Home Security and keeps working however the cloud app is set up.

- The picture from the start of a recording is copied within seconds, while the camera is
  still recording, so there is something off-site even if the PC is taken mid-recording.
- The video is copied as soon as the recording finishes.
- Deleting a clip in the viewer does not delete its backup, so an intruder can't remove it.
- Backups older than "keep_days" are removed, and the oldest are removed when the backup
  grows past "max_gb", so a free cloud plan doesn't fill up.
"""
import datetime
import logging
import os
import re
import shutil
import threading
import time

from fastapi import HTTPException, Request

log = logging.getLogger("camhub.backup")

SUBFOLDER = "Home Camera recordings"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CAM_RE = re.compile(r"^[A-Za-z0-9-]+$")
# Files being copied use this ending, which OneDrive and Google Drive don't upload.
TMP = ".tmp"
# A video with no details file this old is treated as finished (for example after a crash).
ORPHAN_SECONDS = 300

DEFAULT_BACKUP = {
    "enabled": False,
    "folder": "",
    "keep_days": 30,
    "max_gb": 4,
    "people_only": False,
    # Set by the recorder: clips up to this "YYYY-MM-DD/HHMMSS" were removed from the backup
    # to stay under max_gb, so they aren't copied again.
    "trimmed_up_to": "",
}


def cloud_folders(users_root=None, drives=None):
    """Finds OneDrive and Google Drive folders on this PC, for the Settings screen.

    The recorder runs in the background as Windows' system account, so it looks through
    every user's folder rather than just its own.
    """
    found = []
    if users_root is None:
        users_root = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")
    skip = {"public", "default", "default user", "all users"}
    try:
        users = sorted(os.listdir(users_root))
    except OSError:
        users = []
    for user in users:
        home = os.path.join(users_root, user)
        if user.lower() in skip or not os.path.isdir(home):
            continue
        try:
            entries = sorted(os.listdir(home))
        except OSError:
            continue
        for e in entries:
            p = os.path.join(home, e)
            if not os.path.isdir(p):
                continue
            if e.lower().startswith("onedrive"):
                label = f"OneDrive ({e.split(' - ', 1)[1]})" if " - " in e else "OneDrive"
                found.append({"kind": "onedrive", "label": f"{label} for {user}", "path": p})
            elif e.lower() in ("my drive", "google drive"):
                found.append({"kind": "google", "label": f"Google Drive for {user}", "path": p})
    # Google Drive in "Stream files" mode shows up as its own drive letter.
    if drives is None:
        drives = [f"{c}:{os.sep}" for c in "DEFGHIJKLMNOPQRSTUVWXYZ"] if os.name == "nt" else []
    for d in drives:
        p = os.path.join(d, "My Drive")
        if os.path.isdir(p):
            found.append({"kind": "google", "label": f"Google Drive ({d.rstrip(os.sep)})", "path": p})
    return found


def _inside(child, parent):
    child, parent = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


class Backup:
    def __init__(self, config, storage, events=None):
        self.config = config
        self.storage = storage
        self.events = events
        if not isinstance(config.data.get("backup"), dict):
            config.data["backup"] = dict(DEFAULT_BACKUP)
        for k, v in DEFAULT_BACKUP.items():
            config.data["backup"].setdefault(k, v)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._copied = {}  # source path -> (size, mtime) already copied
        self._last_sweep = 0.0
        self._was_failing = False
        self.state = {"state": "off", "message": "", "last_copy": None, "last_check": None, "waiting": 0}

    @property
    def settings(self):
        return self.config.data["backup"]

    def target(self, folder=None):
        folder = self.settings.get("folder") if folder is None else folder
        return os.path.join(folder, SUBFOLDER) if folder else ""

    # ---- checking a folder --------------------------------------------------

    def check_folder(self, folder):
        """Returns an error message for a folder that can't be used, or None if it's fine."""
        folder = (folder or "").strip()
        if not folder:
            return "Choose a folder to back up to."
        if not os.path.isabs(folder):
            return "Enter the full path of the folder, for example C:\\Users\\you\\OneDrive."
        if not os.path.isdir(folder):
            return ("That folder doesn't exist on this PC. If it's Google Drive, open Google Drive's "
                    "settings and choose \"Mirror files\" so it has a normal folder.")
        rec = self.storage.root
        if _inside(folder, rec) or _inside(rec, folder):
            return "The backup can't go inside the recordings folder, or the other way round."
        target = self.target(folder)
        try:
            os.makedirs(target, exist_ok=True)
            test = os.path.join(target, "write-test" + TMP)
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
        except OSError as e:
            return f"A.S.D Home Security couldn't save files there ({e.strerror or e})."
        return None

    # ---- copying ------------------------------------------------------------

    def start(self, interval=10):
        def loop():
            while True:
                try:
                    self.run_once()
                except Exception:
                    log.exception("Backup failed")
                self._wake.wait(interval)
                self._wake.clear()

        threading.Thread(target=loop, name="backup", daemon=True).start()

    def kick(self):
        self._last_sweep = 0.0
        self._copied = {}
        self._wake.set()

    def run_once(self):
        with self._lock:
            s = self.settings
            if not s.get("enabled") or not s.get("folder"):
                self.state.update(state="off", message="", waiting=0)
                return
            target = self.target()
            if not os.path.isdir(s["folder"]):
                self._fail("The backup folder can't be found. Is OneDrive or Google Drive still installed "
                           "and signed in?")
                return
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as e:
                self._fail(f"Can't write to the backup folder ({e.strerror or e}).")
                return
            # Recent days every few seconds; everything still kept, every 10 minutes.
            full = time.time() - self._last_sweep > 600
            days = None if full else {(datetime.date.today() - datetime.timedelta(days=n)).isoformat()
                                      for n in (0, 1)}
            waiting, errors = 0, []
            for src_base, dst_base, finished in self._pending(target, days):
                try:
                    if self._copy_clip(src_base, dst_base, finished):
                        self.state["last_copy"] = datetime.datetime.now().isoformat(timespec="seconds")
                except OSError as e:
                    waiting += 1
                    errors.append(f"{os.path.basename(src_base)}: {e.strerror or e}")
            if full:
                self._last_sweep = time.time()
                self._prune(target)
            self.state["last_check"] = datetime.datetime.now().isoformat(timespec="seconds")
            self.state["waiting"] = waiting
            if errors:
                self._fail("Some clips couldn't be copied: " + "; ".join(errors[:3]))
            else:
                if self._was_failing:
                    log.info("Backup working again")
                    self._notify("Off-site backup is working again.", ok=True)
                self._was_failing = False
                self.state.update(state="ok", message="")

    def _fail(self, message):
        if not self._was_failing:
            log.warning("Backup problem: %s", message)
            self._notify(message, ok=False)
        self._was_failing = True
        self.state.update(state="error", message=message)

    def _notify(self, message, ok):
        if self.events:
            try:
                self.events.publish({"type": "backup", "ok": ok, "message": message,
                                     "time": datetime.datetime.now().isoformat(timespec="seconds")})
            except Exception:
                pass

    def _pending(self, target, days):
        """Yields (recording path without extension, backup path, finished) for clips to copy."""
        root = self.storage.root
        keep_days = int(self.settings.get("keep_days") or 0)
        cutoff = ((datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
                  if keep_days > 0 else "")
        try:
            cams = [c for c in os.listdir(root) if CAM_RE.match(c) and os.path.isdir(os.path.join(root, c))]
        except OSError:
            return
        for cam in cams:
            try:
                day_list = os.listdir(os.path.join(root, cam))
            except OSError:
                continue
            for day in sorted(day_list):
                if not DAY_RE.match(day) or (days is not None and day not in days) or day < cutoff:
                    continue
                folder = os.path.join(root, cam, day)
                try:
                    files = set(os.listdir(folder))
                except OSError:
                    continue
                stems = {f[:-4] for f in files if f.endswith(".jpg")} | {f[:-4] for f in files if f.endswith(".mp4")}
                trimmed = self.settings.get("trimmed_up_to") or ""
                for stem in sorted(stems):
                    if trimmed and f"{day}/{stem}" <= trimmed:
                        continue
                    src = os.path.join(folder, stem)
                    has_mp4 = stem + ".mp4" in files
                    recording = stem + ".mp4.part" in files
                    if not has_mp4 and not recording:
                        continue
                    finished = has_mp4 and (stem + ".json" in files or self._old(src + ".mp4"))
                    if finished and self.settings.get("people_only"):
                        meta = self.storage._read_meta(src + ".json")
                        if not meta.get("person"):
                            continue
                    yield src, os.path.join(target, cam, day, stem), finished

    @staticmethod
    def _old(path):
        try:
            return time.time() - os.path.getmtime(path) > ORPHAN_SECONDS
        except OSError:
            return False

    def _copy_clip(self, src, dst, finished):
        """Copies the picture now, and the video and details once the recording is done."""
        exts = (".jpg", ".json", ".mp4") if finished else (".jpg",)
        copied = False
        for ext in exts:
            if self._copy_file(src + ext, dst + ext):
                copied = True
        return copied

    def _copy_file(self, src, dst):
        try:
            st = os.stat(src)
        except FileNotFoundError:
            return False
        sig = (st.st_size, int(st.st_mtime))
        if self._copied.get(src) == sig:
            return False
        try:
            if os.path.getsize(dst) == st.st_size:
                self._copied[src] = sig
                return False
        except OSError:
            pass
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + TMP
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        self._copied[src] = sig
        if len(self._copied) > 20000:
            self._copied.clear()
        return True

    # ---- removing old backups -------------------------------------------------

    def _days_in(self, target):
        out = []
        try:
            cams = os.listdir(target)
        except OSError:
            return out
        for cam in cams:
            cdir = os.path.join(target, cam)
            if not CAM_RE.match(cam) or not os.path.isdir(cdir):
                continue
            for day in os.listdir(cdir):
                p = os.path.join(cdir, day)
                if DAY_RE.match(day) and os.path.isdir(p):
                    out.append((day, p))
        return sorted(out)

    def _prune(self, target):
        keep_days = int(self.settings.get("keep_days") or 0)
        max_bytes = float(self.settings.get("max_gb") or 0) * 1024 ** 3
        days = self._days_in(target)
        if keep_days > 0:
            cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
            for day, p in list(days):
                if day < cutoff:
                    log.info("Removing backup %s (older than %d days)", p, keep_days)
                    shutil.rmtree(p, ignore_errors=True)
                    days.remove((day, p))
                    self._remove_if_empty(os.path.dirname(p))
        if max_bytes <= 0:
            return
        # Oldest first, across all cameras.
        clips = []
        for day, p in days:
            for f in os.listdir(p):
                if f.endswith(".mp4") or f.endswith(".jpg"):
                    clips.append((day, f[:-4], p))
        clips = sorted(set(clips))
        total = self.usage(target)["bytes"]
        for day, stem, p in clips:
            if total <= max_bytes:
                break
            for ext in (".mp4", ".jpg", ".json"):
                fp = os.path.join(p, stem + ext)
                try:
                    total -= os.path.getsize(fp)
                    os.remove(fp)
                except OSError:
                    pass
            if f"{day}/{stem}" > (self.settings.get("trimmed_up_to") or ""):
                self.settings["trimmed_up_to"] = f"{day}/{stem}"
                self.config.save()
            log.info("Backup over %.1f GB, removed %s", max_bytes / 1024 ** 3, os.path.join(p, stem))
            self._remove_if_empty(p)
            self._remove_if_empty(os.path.dirname(p))

    @staticmethod
    def _remove_if_empty(folder):
        try:
            if not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass

    def usage(self, target=None):
        target = target or self.target()
        n, total, oldest = 0, 0, None
        if target and os.path.isdir(target):
            for day, p in self._days_in(target):
                for f in os.listdir(p):
                    if f.endswith(TMP):
                        continue
                    try:
                        total += os.path.getsize(os.path.join(p, f))
                    except OSError:
                        pass
                    if f.endswith(".mp4"):
                        n += 1
                        oldest = oldest or day
        return {"clips": n, "bytes": total, "oldest_day": oldest}

    def status(self):
        s = self.settings
        out = dict(self.state)
        out.update({k: s.get(k) for k in DEFAULT_BACKUP})
        out["target"] = self.target()
        if s.get("enabled") and s.get("folder"):
            out.update(self.usage())
        return out


def add_routes(app, hub):
    """Adds the backup settings to the web server."""
    backup = getattr(hub, "backup", None)
    config = hub.config
    if backup is None:
        return

    @app.get("/api/backup")
    def get_backup():
        return {"status": backup.status(), "suggestions": cloud_folders()}

    @app.put("/api/backup")
    async def put_backup(request: Request):
        body = await request.json()
        s = dict(backup.settings)
        try:
            if "enabled" in body:
                s["enabled"] = bool(body["enabled"])
            if "folder" in body:
                folder = str(body["folder"] or "").strip().rstrip("\\/")
                if re.match(r"^[A-Za-z]:$", folder):
                    folder += "\\"
                if folder != s.get("folder"):
                    s["trimmed_up_to"] = ""
                s["folder"] = folder
            if "keep_days" in body:
                s["keep_days"] = max(0, min(3650, int(body["keep_days"])))
            if "max_gb" in body:
                s["max_gb"] = max(0.0, min(100000.0, float(body["max_gb"])))
            if "people_only" in body:
                s["people_only"] = bool(body["people_only"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Use numbers for the days and size")
        if s["enabled"]:
            import asyncio
            problem = await asyncio.to_thread(backup.check_folder, s["folder"])
            if problem:
                raise HTTPException(400, problem)
        config.data["backup"] = s
        config.save()
        backup.kick()
        return {"status": backup.status()}

    @app.post("/api/backup/run")
    def run_backup():
        if not backup.settings.get("enabled"):
            raise HTTPException(409, "Turn on off-site backup first")
        backup.kick()
        return {"ok": True}

