"""Where clips live on disk, listing them, and deleting old ones.

Layout: <storage>/<camera id>/<YYYY-MM-DD>/<HHMMSS>.mp4 with a .jpg thumbnail and a
small .json with details. A clip id is "<camera>_<YYYYMMDD>_<HHMMSS>".
"""
import datetime
import json
import logging
import os
import re
import shutil
import threading
import time

log = logging.getLogger("camhub.storage")

ID_RE = re.compile(r"^([A-Za-z0-9-]+)_(\d{8})_(\d{6})$")


class Storage:
    def __init__(self, config):
        self.config = config
        os.makedirs(self.root, exist_ok=True)

    @property
    def root(self):
        return os.path.abspath(self.config.storage["path"])

    def new_clip_paths(self, cam_id, when):
        day = when.strftime("%Y-%m-%d")
        folder = os.path.join(self.root, cam_id, day)
        os.makedirs(folder, exist_ok=True)
        stem = when.strftime("%H%M%S")
        # Two clips in the same second: add a suffix to the time.
        base = os.path.join(folder, stem)
        n = 0
        while os.path.exists(base + ".mp4") or os.path.exists(base + ".mp4.part"):
            n += 1
            when = when + datetime.timedelta(seconds=1)
            stem = when.strftime("%H%M%S")
            base = os.path.join(folder, stem)
        clip_id = f"{cam_id}_{when.strftime('%Y%m%d')}_{stem}"
        return clip_id, base

    def clip_base(self, clip_id):
        m = ID_RE.match(clip_id or "")
        if not m:
            return None
        cam, d, t = m.groups()
        day = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        base = os.path.join(self.root, cam, day, t)
        # Keep paths inside the storage folder.
        if not os.path.abspath(base).startswith(self.root + os.sep):
            return None
        return base

    def days(self, cam_id=None):
        out = {}
        for cam in self._cams(cam_id):
            cam_dir = os.path.join(self.root, cam)
            for day in os.listdir(cam_dir):
                p = os.path.join(cam_dir, day)
                if os.path.isdir(p) and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                    count = sum(1 for f in os.listdir(p) if f.endswith(".mp4"))
                    if count:
                        out[day] = out.get(day, 0) + count
        return [{"date": d, "count": c} for d, c in sorted(out.items(), reverse=True)]

    def clips(self, day, cam_id=None):
        result = []
        for cam in self._cams(cam_id):
            folder = os.path.join(self.root, cam, day)
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or not os.path.isdir(folder):
                continue
            for f in os.listdir(folder):
                if not f.endswith(".mp4"):
                    continue
                stem = f[:-4]
                info = self._read_meta(os.path.join(folder, stem + ".json"))
                clip_id = f"{cam}_{day.replace('-', '')}_{stem}"
                start = info.get("start") or f"{day}T{stem[0:2]}:{stem[2:4]}:{stem[4:6]}"
                result.append({
                    "id": clip_id,
                    "camera": cam,
                    "start": start,
                    "duration": info.get("duration"),
                    "person": bool(info.get("person")),
                    "trigger": info.get("trigger", "motion"),
                    "summary": info.get("ai_summary", ""),
                    "tags": info.get("ai_tags", []),
                    "seen": info.get("seen", []),
                    "size": os.path.getsize(os.path.join(folder, f)),
                })
        result.sort(key=lambda c: c["start"], reverse=True)
        return result

    def clip_meta(self, clip_id):
        base = self.clip_base(clip_id)
        return self._read_meta(base + ".json") if base else {}

    def update_meta(self, clip_id, values):
        """Adds fields (such as the AI description) to a clip's .json file."""
        base = self.clip_base(clip_id)
        if not base or not os.path.exists(base + ".mp4"):
            return False
        info = self._read_meta(base + ".json")
        info.update(values)
        tmp = base + ".json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
        os.replace(tmp, base + ".json")
        return True

    def delete_clip(self, clip_id):
        base = self.clip_base(clip_id)
        if not base or not os.path.exists(base + ".mp4"):
            return False
        for ext in (".mp4", ".jpg", ".json"):
            try:
                os.remove(base + ext)
            except FileNotFoundError:
                pass
        self._remove_if_empty(os.path.dirname(base))
        return True

    def usage(self):
        total, used, free = shutil.disk_usage(self.root)
        clips_bytes = 0
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                if f.endswith(".mp4"):
                    try:
                        clips_bytes += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        return {"disk_total": total, "disk_free": free, "recordings_bytes": clips_bytes}

    def _cams(self, cam_id):
        if cam_id:
            p = os.path.join(self.root, cam_id)
            return [cam_id] if re.match(r"^[A-Za-z0-9-]+$", cam_id) and os.path.isdir(p) else []
        return [c for c in os.listdir(self.root) if os.path.isdir(os.path.join(self.root, c))]

    @staticmethod
    def _read_meta(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _remove_if_empty(self, folder):
        try:
            if not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass

    # ---- automatic cleanup -------------------------------------------------

    def cleanup(self):
        keep_days = int(self.config.storage.get("retention_days") or 0)
        min_free = float(self.config.storage.get("min_free_gb") or 0) * 1024 ** 3
        day_dirs = []
        for cam in self._cams(None):
            cam_dir = os.path.join(self.root, cam)
            for day in os.listdir(cam_dir):
                p = os.path.join(cam_dir, day)
                if os.path.isdir(p) and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                    day_dirs.append((day, p))
        day_dirs.sort()
        if keep_days > 0:
            cutoff = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
            for day, p in list(day_dirs):
                if day < cutoff:
                    log.info("Deleting recordings from %s (older than %d days)", p, keep_days)
                    shutil.rmtree(p, ignore_errors=True)
                    day_dirs.remove((day, p))
        # Low on space: delete the oldest clips one at a time, never today's folder first.
        while min_free and shutil.disk_usage(self.root).free < min_free:
            oldest = None
            for day, p in day_dirs:
                clips = sorted(f for f in os.listdir(p) if f.endswith(".mp4")) if os.path.isdir(p) else []
                if clips:
                    oldest = os.path.join(p, clips[0][:-4])
                    break
            if not oldest:
                break
            log.info("Disk nearly full, deleting %s", oldest)
            for ext in (".mp4", ".jpg", ".json"):
                try:
                    os.remove(oldest + ext)
                except FileNotFoundError:
                    pass
            self._remove_if_empty(os.path.dirname(oldest))
        # Leftover partial files from a crash or power cut.
        now = time.time()
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                if f.endswith(".part"):
                    fp = os.path.join(dirpath, f)
                    try:
                        if now - os.path.getmtime(fp) > 3600:
                            os.remove(fp)
                    except OSError:
                        pass

    def start_cleanup_thread(self, interval=600):
        def loop():
            while True:
                try:
                    self.cleanup()
                except Exception:
                    log.exception("Cleanup failed")
                time.sleep(interval)

        threading.Thread(target=loop, name="cleanup", daemon=True).start()
