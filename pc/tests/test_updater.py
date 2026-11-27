"""Automatic updates.  Run:  python -m pytest tests"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.updater import GitHubError, Updater, in_hours, newer  # noqa: E402

EXE = b"new installer" * 100
APK = b"new phone app" * 100


def sha(b):
    return hashlib.sha256(b).hexdigest()


class FakeGitHub:
    """Stands in for GitHub's release page with windows.json and android.json."""

    def __init__(self, win="1.0.50", phone="1.0.50", exe=EXE, apk=APK, fail=None):
        self.files = {
            "windows.json": json.dumps({"version": win, "sha256": sha(EXE), "size": len(EXE)}).encode(),
            "android.json": json.dumps({"version": phone, "code": int(phone.split(".")[-1]),
                                        "sha256": sha(APK), "size": len(APK)}).encode(),
            "HomeCameraSetup.exe": exe,
            "HomeCamera.apk": apk,
        }
        self.fail = fail
        self.calls = []

    def __call__(self, url, token="", accept="", to_file=None, timeout=60):
        self.calls.append((url, token))
        if self.fail:
            raise GitHubError(self.fail)
        if url.endswith("/releases/tags/latest"):
            return json.dumps({"assets": [{"name": n, "url": "asset://" + n} for n in self.files]}).encode()
        data = self.files[url[len("asset://"):]]
        if to_file:
            with open(to_file, "wb") as f:
                f.write(data)
            return sha(data)
        return data


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Cam:
    clip = None


class Hub:
    def __init__(self):
        self.cameras = {"front": Cam()}


def make(tmp_path, gh, current="1.0.40", hour=3, can_install=True, clock=None, hub=None, launched=None):
    p = tmp_path / "config.json"
    if not p.exists():
        p.write_text(json.dumps({"server": {"password_hash": auth.hash_password("pw123456")}}))
    config = Config(str(p))
    launched = launched if launched is not None else []
    up = Updater(config, str(tmp_path), hub=hub or Hub(), current=current, get=gh, can_install=can_install,
                 launcher=lambda exe, logf: launched.append(exe), clock=clock or Clock(),
                 local_hour=lambda: hour)
    return up, launched


def test_versions():
    assert newer("1.0.10", "1.0.9")
    assert not newer("1.0.9", "1.0.10")
    assert not newer("1.0.9", "1.0.9")
    assert not newer("1.0.9", "dev")      # a copy run from source never updates itself
    assert in_hours(3, 2, 5) and not in_hours(5, 2, 5)
    assert in_hours(23, 22, 4) and in_hours(1, 22, 4) and not in_hours(12, 22, 4)


def test_downloads_both_and_installs_at_night(tmp_path):
    clock = Clock()
    up, launched = make(tmp_path, FakeGitHub(), clock=clock)
    st = up.check()
    assert st["state"] == "ready" and st["pc_ready"] == "1.0.50" and st["phone_version"] == "1.0.50"
    assert open(up.exe_path(), "rb").read() == EXE
    assert open(up.apk_path(), "rb").read() == APK
    assert not up.maybe_auto_install()          # just started: wait a while
    clock.t += up.STARTUP_GRACE + 1
    assert up.maybe_auto_install()
    assert launched == [up.exe_path()]
    assert up.status()["installing"]
    assert not up.maybe_auto_install()          # not again straight away


def test_waits_for_night_and_for_recordings_to_finish(tmp_path):
    clock = Clock()
    hub = Hub()
    up, launched = make(tmp_path, FakeGitHub(), hour=14, clock=clock, hub=hub)
    up.check()
    clock.t += up.STARTUP_GRACE + 1
    assert not up.maybe_auto_install()          # daytime
    up.local_hour = lambda: 3
    hub.cameras["front"].clip = object()
    assert not up.maybe_auto_install()          # recording
    hub.cameras["front"].clip = None
    up.settings["auto_install"] = False
    assert not up.maybe_auto_install()          # turned off
    assert launched == []
    up.install()                                # the Install button still works
    assert launched == [up.exe_path()]


def test_up_to_date_and_bad_checksum(tmp_path):
    up, _ = make(tmp_path, FakeGitHub(win="1.0.40"))
    st = up.check()
    assert st["state"] == "ok" and st["pc_ready"] is None and "up to date" in st["message"]

    up2, _ = make(tmp_path, FakeGitHub(win="1.0.60", exe=b"tampered"))
    st = up2.check()
    assert st["state"] == "error" and "checksum" in st["message"]
    assert not os.path.exists(up2.exe_path())


def test_no_key_message_and_token_kept_private(tmp_path):
    gh = FakeGitHub(fail="needs a key")
    up, _ = make(tmp_path, gh)
    assert up.check()["state"] == "error"
    st = up.update_settings({"token": "github_pat_abc123"})
    assert st["has_token"] and "token" not in json.dumps(st).replace("has_token", "")
    gh.fail = None
    up.check()
    assert all(tok == "github_pat_abc123" for _, tok in gh.calls[-1:])


def test_records_a_finished_update(tmp_path):
    up, _ = make(tmp_path, FakeGitHub())
    up.check()
    up.install()
    # A.S.D Home Security restarts as the new version.
    up2, _ = make(tmp_path, FakeGitHub(), current="1.0.50")
    st = up2.status()
    assert st["history"][0]["from"] == "1.0.40" and st["history"][0]["to"] == "1.0.50"
    assert st["pc_ready"] is None
    assert st["phone_version"] == "1.0.50"      # the phone app file is still offered


def test_not_windows_only_tells(tmp_path):
    up, launched = make(tmp_path, FakeGitHub(), can_install=False)
    st = up.check()
    assert st["pc_ready"] is None and "1.0.50" in st["message"]
    assert st["phone_version"] == "1.0.50"


def test_phone_routes(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.updater import add_routes
    from fastapi import FastAPI

    up, _ = make(tmp_path, FakeGitHub())
    hub = Hub()
    hub.updater = up
    app = FastAPI()
    add_routes(app, hub)
    c = TestClient(app)
    assert c.get("/api/updates/android").status_code == 404
    up.check()
    info = c.get("/api/updates/android").json()
    assert info["code"] == 50 and info["sha256"] == sha(APK)
    assert c.get("/api/updates/android/HomeCamera.apk").content == APK
    assert c.put("/api/updates", json={"token": "has space"}).status_code == 400
