"""Automatic updates for the PC app, and the phone app's update file.

Every few hours the PC looks at the "latest" download page on GitHub. Each build puts two small
files there next to the downloads: windows.json and android.json, holding the version number and
a checksum of HomeCameraSetup.exe and HomeCamera.apk.

- PC: a newer HomeCameraSetup.exe is downloaded, checked against its checksum, and installed
  quietly (at night by default, and never while a camera is recording). Installing over the
  top keeps the settings and recordings, then starts Home Camera again.
- Phone: a newer HomeCamera.apk is downloaded and kept here. The phone app asks the PC for it
  (it already signs in to the PC), so only the PC needs to reach GitHub.

The project is public on GitHub, so no key is needed. If it is made private again, reading the
download page needs a read-only GitHub key ("token"), entered once in Settings > Updates.

Settings (config.json, "updates"):
  auto_install   install PC updates by itself (otherwise press Install in Settings > Updates)
  token          the read-only GitHub key (never sent back to the browser)
  night_start / night_end   hours (0-23, this PC's clock) in which automatic installs happen
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__

log = logging.getLogger("camhub.updater")

REPO = "alastairstewartduncan/homeawcurity"
TAG = "latest"
API = "https://api.github.com"
TASK = "Home Camera Update"

DEFAULT_UPDATES = {"auto_install": True, "token": "", "night_start": 2, "night_end": 5}


def parse_version(v):
    """'1.0.42' -> (1, 0, 42). Anything else (like 'dev') -> None."""
    try:
        parts = tuple(int(p) for p in str(v).strip().lstrip("v").split("."))
        return parts if parts else None
    except ValueError:
        return None


def newer(candidate, current):
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def in_hours(hour, start, end):
    """Is `hour` inside start..end (end not included)? Handles windows over midnight."""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class GitHubError(Exception):
    pass


def http_get(url, token="", accept="application/vnd.github+json", to_file=None, timeout=60):
    """Fetches a URL. Returns bytes, or writes to `to_file` and returns its sha256."""
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "HomeCamera-updater",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        # Not passed on when GitHub redirects the download to its file store, which refuses it.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if to_file is None:
                return r.read()
            h = hashlib.sha256()
            with open(to_file, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
            return h.hexdigest()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            if token:
                raise GitHubError("GitHub didn't accept the update key. It may have expired or not have "
                                  "read access to the project. Make a new one and paste it below.")
            raise GitHubError("Home Camera needs a read-only GitHub key to find updates, because the "
                              "project is private. Add one below.")
        raise GitHubError(f"GitHub answered {e.code} {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise GitHubError(f"Couldn't reach GitHub: {getattr(e, 'reason', e)}")


class Updater:
    CHECK_SECONDS = 6 * 3600
    LOOP_SECONDS = 600
    STARTUP_GRACE = 600          # don't install in the first minutes after starting
    RETRY_SECONDS = 24 * 3600    # after an install that didn't take, wait before trying again

    def __init__(self, config, data_dir, hub=None, current=None, get=http_get, can_install=None,
                 launcher=None, clock=time.time, local_hour=None):
        self.config = config
        self.dir = os.path.join(data_dir, "updates")
        self.hub = hub
        self.current = current or __version__
        self.get = get
        self.can_install = (sys.platform == "win32" and getattr(sys, "frozen", False)) \
            if can_install is None else can_install
        self.launcher = launcher or launch_installer
        self.clock = clock
        self.local_hour = local_hour or (lambda: time.localtime().tm_hour)
        cur = config.data.get("updates") or {}
        merged = dict(DEFAULT_UPDATES)
        merged.update({k: v for k, v in cur.items() if k in DEFAULT_UPDATES})
        config.data["updates"] = merged
        self._lock = threading.RLock()
        self._busy = False
        self._stop = threading.Event()
        self._started = clock()
        self.state = {"state": "idle", "message": "", "checked_at": None}
        self.pc = None       # {"version", "sha256", "size", "file"} of a downloaded newer installer
        self.phone = None    # {"version", "code", "sha256", "size"} of the kept phone app
        os.makedirs(self.dir, exist_ok=True)
        self._load()

    @property
    def settings(self):
        return self.config.data["updates"]

    # ---- saved state -----------------------------------------------------------------

    def _state_file(self):
        return os.path.join(self.dir, "state.json")

    def _load(self):
        try:
            with open(self._state_file(), encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        self.history = saved.get("history", [])
        self.attempt = saved.get("attempt")    # {"from", "to", "at"}
        if self.attempt and self.attempt.get("from") != self.current:
            # We started after an install: note whether it worked.
            ok = self.attempt.get("to") == self.current
            self.history = ([{"from": self.attempt["from"], "to": self.current, "at": self.clock(), "ok": ok}]
                            + self.history)[:10]
            log.info("Updated from %s to %s", self.attempt["from"], self.current)
            self.attempt = None
            self._save()
        info = self._read_json(os.path.join(self.dir, "android.json"))
        if info and os.path.isfile(self.apk_path()):
            self.phone = info
        info = self._read_json(os.path.join(self.dir, "windows.json"))
        if info and newer(info.get("version"), self.current) and os.path.isfile(self.exe_path()):
            self.pc = info

    def _save(self):
        tmp = self._state_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"history": self.history, "attempt": self.attempt}, f, indent=2)
        os.replace(tmp, self._state_file())

    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write_json(self, name, data):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def exe_path(self):
        return os.path.join(self.dir, "HomeCameraSetup.exe")

    def apk_path(self):
        return os.path.join(self.dir, "HomeCamera.apk")

    # ---- settings and status -----------------------------------------------------------

    def update_settings(self, body):
        s = self.settings
        if "auto_install" in body:
            s["auto_install"] = bool(body["auto_install"])
        if body.get("clear_token"):
            s["token"] = ""
        elif body.get("token"):
            tok = str(body["token"]).strip()
            if len(tok) > 255 or any(c.isspace() for c in tok):
                raise ValueError("That doesn't look like a GitHub key. Copy the whole key and paste it again.")
            s["token"] = tok
        for k in ("night_start", "night_end"):
            if k in body:
                try:
                    s[k] = max(0, min(23, int(body[k])))
                except (TypeError, ValueError):
                    raise ValueError("Use an hour from 0 to 23")
        self.config.save()
        return self.status()

    def status(self):
        s = self.settings
        with self._lock:
            stuck = bool(self.attempt and self.attempt.get("from") == self.current
                         and self.clock() - self.attempt.get("at", 0) >= 900)
            return {
                "last_failed": (f"Installing version {self.attempt['to']} didn't finish. It will be tried "
                                "again tomorrow. Details are in the updates folder (install.log)."
                                if stuck else ""),
                "current": self.current,
                "state": "checking" if self._busy else self.state["state"],
                "message": self.state["message"],
                "checked_at": self.state["checked_at"],
                "latest": self.state.get("latest"),
                "pc_ready": self.pc["version"] if self.pc else None,
                "phone_version": self.phone["version"] if self.phone else None,
                "can_install": self.can_install,
                "auto_install": s["auto_install"],
                "night_start": s["night_start"],
                "night_end": s["night_end"],
                "has_token": bool(s["token"]),
                "history": self.history[:5],
                "installing": bool(self.attempt and self.attempt.get("to") != self.current
                                   and self.clock() - self.attempt.get("at", 0) < 900),
            }

    # ---- checking ------------------------------------------------------------------------

    def check_in_background(self):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        threading.Thread(target=self._check_guarded, name="update-check", daemon=True).start()
        return True

    def _check_guarded(self):
        try:
            self.check()
        finally:
            with self._lock:
                self._busy = False

    def check(self):
        """Looks for newer downloads and fetches them. Returns the status."""
        token = self.settings.get("token", "")
        try:
            rel = json.loads(self.get(f"{API}/repos/{REPO}/releases/tags/{TAG}", token))
            assets = {a["name"]: a for a in rel.get("assets", [])}

            def manifest(name):
                a = assets.get(name)
                if not a:
                    return None
                return json.loads(self.get(a["url"], token, accept="application/octet-stream"))

            win, android = manifest("windows.json"), manifest("android.json")
            latest = (win or {}).get("version")
            msg = ""
            if win and newer(win.get("version"), self.current):
                if not self.can_install:
                    msg = f"Version {win['version']} is out. Install it by running the newest HomeCameraSetup.exe."
                elif not (self.pc and self.pc.get("version") == win["version"]):
                    self._download(assets, "HomeCameraSetup.exe", win, self.exe_path())
                    self._write_json("windows.json", win)
                    self.pc = win
                if self.pc:
                    msg = f"Version {win['version']} is downloaded and ready to install."
            else:
                self.pc = None
                for leftover in (self.exe_path(), os.path.join(self.dir, "windows.json")):
                    if os.path.exists(leftover):
                        os.remove(leftover)
                msg = "Home Camera is up to date." if win else "No PC update information on GitHub yet."
            if android and android.get("version") and (
                    not self.phone or newer(android["version"], self.phone.get("version"))
                    or not os.path.isfile(self.apk_path())):
                self._download(assets, "HomeCamera.apk", android, self.apk_path())
                self._write_json("android.json", android)
                self.phone = android
            with self._lock:
                self.state = {"state": "ready" if self.pc else "ok", "message": msg,
                              "checked_at": self.clock(), "latest": latest}
        except GitHubError as e:
            log.warning("Update check: %s", e)
            with self._lock:
                self.state = {"state": "error", "message": str(e), "checked_at": self.clock(),
                              "latest": self.state.get("latest")}
        except (ValueError, KeyError, OSError) as e:
            log.exception("Update check failed")
            with self._lock:
                self.state = {"state": "error", "message": f"The update couldn't be downloaded: {e}",
                              "checked_at": self.clock(), "latest": self.state.get("latest")}
        return self.status()

    def _download(self, assets, name, info, dest):
        a = assets.get(name)
        if not a:
            raise GitHubError(f"{name} is missing from the download page")
        log.info("Downloading %s %s", name, info.get("version"))
        part = dest + ".part"
        digest = self.get(a["url"], self.settings.get("token", ""), accept="application/octet-stream",
                          to_file=part, timeout=600)
        if digest != str(info.get("sha256", "")).lower():
            os.remove(part)
            raise GitHubError(f"The downloaded {name} didn't match its checksum, so it wasn't used. "
                              "It will be tried again later.")
        os.replace(part, dest)

    # ---- installing ----------------------------------------------------------------------

    def recording(self):
        cams = getattr(self.hub, "cameras", {}) if self.hub else {}
        return any(getattr(c, "clip", None) is not None for c in cams.values())

    def install(self, reason="asked"):
        """Starts the downloaded installer. Home Camera stops a moment later and comes back updated."""
        with self._lock:
            if not self.can_install:
                raise RuntimeError("Updates can only install themselves on the Windows PC app.")
            if not self.pc or not os.path.isfile(self.exe_path()):
                raise RuntimeError("There's no update downloaded to install.")
            version = self.pc["version"]
            self.attempt = {"from": self.current, "to": version, "at": self.clock()}
            self._save()
        log.info("Installing version %s (%s)", version, reason)
        try:
            self.launcher(self.exe_path(), os.path.join(self.dir, "install.log"))
        except Exception as e:
            with self._lock:
                self.attempt = None
                self._save()
            raise RuntimeError(f"The installer couldn't be started: {e}")
        return self.status()

    def maybe_auto_install(self):
        s = self.settings
        if not (s["auto_install"] and self.can_install and self.pc):
            return False
        now = self.clock()
        if now - self._started < self.STARTUP_GRACE:
            return False
        if self.attempt and now - self.attempt.get("at", 0) < self.RETRY_SECONDS:
            return False
        if not in_hours(self.local_hour(), s["night_start"], s["night_end"]):
            return False
        if self.recording():
            return False
        try:
            self.install("automatic, at night")
        except RuntimeError as e:
            log.warning("Automatic update: %s", e)
            return False
        return True

    # ---- running -------------------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # Wait a little after starting, so start-up isn't slowed by a download.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                last = self.state.get("checked_at") or 0
                if self.clock() - last >= self.CHECK_SECONDS and self._claim():
                    self._check_guarded()
                self.maybe_auto_install()
            except Exception:
                log.exception("Update loop")
            self._stop.wait(self.LOOP_SECONDS)

    def _claim(self):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True


def launch_installer(exe, log_file):
    """Runs the installer quietly from a one-off scheduled task.

    It must not be a child of camhub.exe, because the installer stops camhub.exe (and
    everything it started) before replacing its files.
    """
    cmd = f'"{exe}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="{log_file}"'
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "ONCE", "/ST", "00:00",
                    "/SD", "01/01/2030", "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", cmd],
                   check=True, capture_output=True, creationflags=flags, timeout=60)
    subprocess.run(["schtasks", "/Run", "/TN", TASK], check=True, capture_output=True,
                   creationflags=flags, timeout=60)


def add_routes(app, hub):
    """Settings > Updates, and the phone app's update download."""
    from fastapi import HTTPException, Request
    from fastapi.responses import FileResponse

    up = hub.updater

    @app.get("/api/updates")
    def get_updates():
        return up.status()

    @app.put("/api/updates")
    async def put_updates(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        try:
            return up.update_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/updates/check")
    def check_updates():
        up.check_in_background()
        return up.status()

    @app.post("/api/updates/install")
    def install_update():
        try:
            return up.install()
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/updates/android")
    def phone_update():
        """The newest phone app the PC has: {"version", "code", "sha256", "size"}."""
        if not up.phone or not os.path.isfile(up.apk_path()):
            raise HTTPException(404, "No phone app update downloaded yet")
        return {k: up.phone.get(k) for k in ("version", "code", "sha256", "size")}

    @app.get("/api/updates/android/HomeCamera.apk")
    def phone_update_file():
        if not up.phone or not os.path.isfile(up.apk_path()):
            raise HTTPException(404, "No phone app update downloaded yet")
        return FileResponse(up.apk_path(), media_type="application/vnd.android.package-archive",
                            filename="HomeCamera.apk", headers={"Cache-Control": "no-store"})
