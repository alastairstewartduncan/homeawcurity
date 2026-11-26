"""Checks for off-site backup.  Run:  python -m pytest tests"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub.backup import SUBFOLDER, Backup, cloud_folders  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.storage import Storage  # noqa: E402


def make(tmp_path, **backup):
    cfg_path = tmp_path / "config.json"
    cloud = tmp_path / "OneDrive"
    cloud.mkdir()
    settings = {"enabled": True, "folder": str(cloud)}
    settings.update(backup)
    cfg_path.write_text(json.dumps({"storage": {"path": str(tmp_path / "rec")}, "backup": settings}))
    config = Config(str(cfg_path))
    storage = Storage(config)
    events = EventBus()
    return config, storage, Backup(config, storage, events), cloud / SUBFOLDER, events


def clip(storage, cam, day, stem, size=1000, person=False, finished=True):
    folder = os.path.join(storage.root, cam, day)
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, stem)
    with open(base + ".jpg", "wb") as f:
        f.write(b"j" * 100)
    with open(base + (".mp4" if finished else ".mp4.part"), "wb") as f:
        f.write(b"v" * size)
    if finished:
        with open(base + ".json", "w") as f:
            json.dump({"person": person}, f)
    return base


def today(n=0):
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def test_copies_finished_clips_and_pictures_of_ones_still_recording(tmp_path):
    config, storage, bk, target, _ = make(tmp_path)
    clip(storage, "front", today(), "080000")
    clip(storage, "front", today(), "090000", finished=False)
    bk.run_once()
    day = target / "front" / today()
    assert (day / "080000.mp4").read_bytes() == b"v" * 1000
    assert (day / "080000.json").exists() and (day / "080000.jpg").exists()
    assert (day / "090000.jpg").exists() and not (day / "090000.mp4").exists()
    assert not list(day.glob("*.tmp"))
    assert bk.status()["state"] == "ok" and bk.status()["clips"] == 1

    # The recording finishes: its video is copied on the next pass.
    base = os.path.join(storage.root, "front", today(), "090000")
    os.replace(base + ".mp4.part", base + ".mp4")
    with open(base + ".json", "w") as f:
        json.dump({}, f)
    bk.run_once()
    assert (day / "090000.mp4").exists()


def test_deleting_a_clip_keeps_its_backup(tmp_path):
    config, storage, bk, target, _ = make(tmp_path)
    clip(storage, "front", today(), "080000")
    bk.run_once()
    assert storage.delete_clip(f"front_{today().replace('-', '')}_080000")
    bk.kick()
    bk.run_once()
    assert (target / "front" / today() / "080000.mp4").exists()


def test_people_only(tmp_path):
    config, storage, bk, target, _ = make(tmp_path, people_only=True)
    clip(storage, "front", today(), "080000", person=False)
    clip(storage, "front", today(), "081000", person=True)
    bk.run_once()
    day = target / "front" / today()
    assert not (day / "080000.mp4").exists()
    assert (day / "081000.mp4").exists()


def test_old_backups_removed_and_size_limit_kept(tmp_path):
    config, storage, bk, target, _ = make(tmp_path, keep_days=5, max_gb=3500 / 1024 ** 3)
    old = target / "front" / today(10)
    old.mkdir(parents=True)
    (old / "120000.mp4").write_bytes(b"x")
    for n, stem in enumerate(["070000", "080000", "090000"]):
        clip(storage, "front", today(1), stem)
    clip(storage, "front", today(), "060000")
    bk.run_once()
    assert not old.exists()
    # 4 clips of 1100 bytes each is over 3500 bytes, so the oldest goes.
    left = sorted(p.name for p in target.rglob("*.mp4"))
    assert left == ["060000.mp4", "080000.mp4", "090000.mp4"]
    assert config.data["backup"]["trimmed_up_to"] == f"{today(1)}/070000"
    # It isn't copied back again on the next full pass, even after a restart.
    bk2 = Backup(Config(config.path), storage)
    bk2.run_once()
    assert sorted(p.name for p in target.rglob("*.mp4")) == left


def test_missing_folder_reported_once(tmp_path):
    config, storage, bk, target, events = make(tmp_path)
    q = events.subscribe()
    config.data["backup"]["folder"] = str(tmp_path / "gone")
    bk.run_once()
    bk.run_once()
    assert bk.status()["state"] == "error"
    assert q.qsize() == 1 and q.get()["ok"] is False


def test_check_folder(tmp_path):
    config, storage, bk, target, _ = make(tmp_path)
    assert bk.check_folder(str(tmp_path / "OneDrive")) is None
    assert bk.check_folder("relative/path")
    assert bk.check_folder(str(tmp_path / "nope"))
    assert bk.check_folder(storage.root)
    assert bk.check_folder(str(tmp_path))  # recordings are inside it


def test_finds_cloud_folders(tmp_path):
    users = tmp_path / "Users"
    for p in ("alastair/OneDrive", "alastair/OneDrive - Acme Ltd", "alastair/My Drive", "Public/OneDrive",
              "bob/Documents"):
        (users / p).mkdir(parents=True)
    g = tmp_path / "G"
    (g / "My Drive").mkdir(parents=True)
    found = cloud_folders(str(users), [str(g) + os.sep])
    labels = [f["label"] for f in found]
    assert labels == ["Google Drive for alastair", "OneDrive for alastair", "OneDrive (Acme Ltd) for alastair",
                      "Google Drive (" + str(g) + ")"]


def test_backup_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub import auth
    from camhub.server import create_app

    config, storage, bk, target, _ = make(tmp_path, enabled=False)

    class Hub:
        pass

    hub = Hub()
    hub.config, hub.storage, hub.backup, hub.cameras = config, storage, bk, {}
    client = TestClient(create_app(hub))
    client.headers["Authorization"] = "Bearer " + auth.make_token(config.server["secret"])
    r = client.get("/api/backup")
    assert r.status_code == 200 and r.json()["status"]["enabled"] is False
    r = client.put("/api/backup", json={"enabled": True, "folder": str(tmp_path / "missing")})
    assert r.status_code == 400 and "doesn't exist" in r.json()["detail"]
    r = client.put("/api/backup", json={"enabled": True, "folder": str(tmp_path / "OneDrive"), "keep_days": 7,
                                        "max_gb": "2.5", "people_only": True})
    assert r.status_code == 200, r.text
    saved = json.loads(open(config.path).read())["backup"]
    assert saved["enabled"] and saved["keep_days"] == 7 and saved["max_gb"] == 2.5 and saved["people_only"]
    assert target.is_dir()
    assert client.post("/api/backup/run").status_code == 200
