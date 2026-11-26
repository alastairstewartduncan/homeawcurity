"""Loading and saving the recorder's settings file (config.json)."""
import copy
import json
import os
import secrets
import threading

DEFAULT_MOTION = {
    # 1 (least sensitive) to 10 (most sensitive): how different a pixel must be to count as moving.
    "sensitivity": 6,
    # Share of the picture (after removing ignore zones) that must change to count as motion.
    "min_area_percent": 0.4,
    # Only start a recording when a person is seen. Motion alone is still used to keep it going.
    "person_only": True,
    "person_confidence": 0.5,
    "pre_seconds": 10,
    "post_seconds": 15,
    "max_clip_minutes": 5,
    # Each zone: {"name": "Road", "points": [[x, y], ...]} with x and y from 0 to 1.
    "ignore_zones": [],
}

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8080,
        "password_hash": "",
        "secret": "",
        "https_cert": "",
        "https_key": "",
    },
    "storage": {
        "path": "C:/CameraRecordings",
        "retention_days": 30,
        "min_free_gb": 20,
    },
    "ffmpeg_path": "",
    "cameras": [],
}

DEFAULT_CAMERA = {
    "id": "front",
    "name": "Front",
    "main_url": "",
    "sub_url": "",
    "onvif": {"host": "", "port": 8888, "username": "admin", "password": ""},
    "motion": DEFAULT_MOTION,
}


def _merge(defaults, value):
    if isinstance(defaults, dict) and isinstance(value, dict):
        out = copy.deepcopy(defaults)
        for k, v in value.items():
            out[k] = _merge(defaults.get(k), v) if k in defaults else v
        return out
    return copy.deepcopy(value) if value is not None else copy.deepcopy(defaults)


class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.data = _merge(DEFAULT_CONFIG, raw)
        self.data["cameras"] = [_merge(DEFAULT_CAMERA, c) for c in raw.get("cameras", [])]
        ids = [c["id"] for c in self.data["cameras"]]
        if len(ids) != len(set(ids)):
            raise ValueError("Each camera needs its own id in config.json")
        if not self.data["server"]["secret"]:
            self.data["server"]["secret"] = secrets.token_hex(32)
            self.save()

    @property
    def server(self):
        return self.data["server"]

    @property
    def storage(self):
        return self.data["storage"]

    @property
    def cameras(self):
        return self.data["cameras"]

    def camera(self, cam_id):
        for c in self.cameras:
            if c["id"] == cam_id:
                return c
        return None

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
