"""Smart alerts: strangers and family, your cars, parcels, lines and loitering.

Everything runs on this PC, on the pictures the recorder already looks at.

- Faces: when someone comes into view, the PC takes a full quality picture, finds faces and
  compares them with the family faces you've named. Family: no alert (a quiet "known_person"
  event). Anyone else: a "stranger" alert instead of the usual "person seen".
- Cars: a vehicle that stops in view is compared with the cars you've marked as yours.
  Not yours: an "unknown vehicle" alert.
- Parcels: you draw the doorstep. After someone visits, if something new is left there,
  "parcel_arrived". If it then goes after another visit, "parcel_taken" (an alert when nobody
  is home).
- Lines: you draw a line, such as across the drive. Someone (or a car) crossing it alerts.
- Loitering: you draw an area, such as around the car. Someone staying there longer than the
  time you choose alerts.

Events published (all carry smart: true, camera, camera_name, time, title, message, picture):
  stranger, known_person, vehicle (known true/false), parcel_arrived, parcel_taken,
  loitering, line_crossed.

Settings live in config.json under "smart"; named people and cars, and pictures, in the
"smart" folder next to config.json.
"""
import collections
import copy
import datetime
import json
import logging
import os
import queue
import re
import threading

import cv2
import numpy as np

from . import faces as facemod

log = logging.getLogger("camhub.smart")

DEFAULT_SMART = {
    "faces": {
        "enabled": False,
        "strictness": "normal",       # relaxed, normal, strict
        "no_face": "alert",           # when no face can be seen: "alert" (person seen) or "quiet"
        "wait_seconds": 12,           # how long to look for a face before deciding
    },
    "vehicles": {
        "enabled": False,
        "strictness": "normal",
        "alert_known": False,         # also alert (quietly) when your own car arrives
    },
    "parcels": {
        "taken_alert": "away",        # "away": alert only when nobody is home; "always"
    },
    "cameras": {},                    # camera id -> {"parcel": {...}, "lines": [...], "areas": [...]}
}
DEFAULT_CAMERA_SMART = {"parcel": {"enabled": False, "area": []}, "lines": [], "areas": []}

VISIT_GAP = 30          # seconds without a person before a visit is over
LOOK_EVERY = 1.5        # seconds between face checks during a visit
TRACK_MEMORY = 15       # keep looking for people this long after the last one was seen
WHAT = ("person", "vehicle", "both")


# ---- settings --------------------------------------------------------------------------

def _num(v, lo, hi, default, kind=float):
    try:
        v = kind(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _point(p):
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return [round(max(0.0, min(1.0, x)), 4), round(max(0.0, min(1.0, y)), 4)]


def _points(pts):
    out = [q for q in (_point(p) for p in (pts or [])) if q is not None]
    return out if len(out) >= 3 else []


def _name(v, default):
    v = re.sub(r"\s+", " ", str(v or "")).strip()[:40]
    return v or default


def _rule_id(v, prefix, i):
    v = str(v or "")
    return v if re.fullmatch(r"[\w-]{1,40}", v) else f"{prefix}{i + 1}"


def clean_camera(body):
    """Checks one camera's parcel area, lines and loitering areas from the viewer."""
    body = body if isinstance(body, dict) else {}
    parcel = body.get("parcel") if isinstance(body.get("parcel"), dict) else {}
    out = {"parcel": {"enabled": bool(parcel.get("enabled")), "area": _points(parcel.get("area"))},
           "lines": [], "areas": []}
    if not out["parcel"]["area"]:
        out["parcel"]["enabled"] = False
    for i, ln in enumerate(body.get("lines") or []):
        if not isinstance(ln, dict):
            continue
        a, b = _point(ln.get("a")), _point(ln.get("b"))
        if a is None or b is None or abs(a[0] - b[0]) + abs(a[1] - b[1]) < 0.02:
            continue
        out["lines"].append({
            "id": _rule_id(ln.get("id"), "line", i), "name": _name(ln.get("name"), f"Line {i + 1}"),
            "a": a, "b": b,
            "direction": ln.get("direction") if ln.get("direction") in ("both", "arrow") else "both",
            "what": ln.get("what") if ln.get("what") in WHAT else "person",
            "enabled": ln.get("enabled", True) is not False,
        })
    for i, ar in enumerate(body.get("areas") or []):
        if not isinstance(ar, dict):
            continue
        pts = _points(ar.get("points"))
        if not pts:
            continue
        out["areas"].append({
            "id": _rule_id(ar.get("id"), "area", i), "name": _name(ar.get("name"), f"Area {i + 1}"),
            "points": pts, "seconds": int(_num(ar.get("seconds"), 5, 3600, 30, int)),
            "what": ar.get("what") if ar.get("what") in WHAT else "person",
            "enabled": ar.get("enabled", True) is not False,
        })
    ids = [r["id"] for r in out["lines"] + out["areas"]]
    if len(ids) != len(set(ids)):
        for i, r in enumerate(out["lines"]):
            r["id"] = f"line{i + 1}"
        for i, r in enumerate(out["areas"]):
            r["id"] = f"area{i + 1}"
    return out


def clean_settings(body, current):
    """Checks the face, car and parcel settings from the viewer, keeping what isn't given."""
    out = copy.deepcopy(current)
    body = body if isinstance(body, dict) else {}
    f = body.get("faces")
    if isinstance(f, dict):
        if "enabled" in f:
            out["faces"]["enabled"] = bool(f["enabled"])
        if f.get("strictness") in facemod.FACE_MATCH:
            out["faces"]["strictness"] = f["strictness"]
        if f.get("no_face") in ("alert", "quiet"):
            out["faces"]["no_face"] = f["no_face"]
        if "wait_seconds" in f:
            out["faces"]["wait_seconds"] = int(_num(f["wait_seconds"], 3, 60, 12, int))
    v = body.get("vehicles")
    if isinstance(v, dict):
        if "enabled" in v:
            out["vehicles"]["enabled"] = bool(v["enabled"])
        if v.get("strictness") in facemod.CAR_MATCH:
            out["vehicles"]["strictness"] = v["strictness"]
        if "alert_known" in v:
            out["vehicles"]["alert_known"] = bool(v["alert_known"])
    p = body.get("parcels")
    if isinstance(p, dict) and p.get("taken_alert") in ("away", "always"):
        out["parcels"]["taken_alert"] = p["taken_alert"]
    return out


# ---- geometry --------------------------------------------------------------------------

def side(a, b, p):
    """Which side of the line a->b the point p is on: > 0 is the side the arrow points to."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def crossing(a, b, p, q):
    """Did moving from p to q cross the line a-b? 1 in the arrow's direction, -1 against it,
    0 not at all."""
    s1, s2 = side(a, b, p), side(a, b, q)
    if s1 == 0 or s2 == 0 or (s1 > 0) == (s2 > 0):
        return 0
    t1, t2 = side(p, q, a), side(p, q, b)
    if (t1 > 0) == (t2 > 0) and t1 != 0 and t2 != 0:
        return 0
    return 1 if s1 < 0 < s2 else -1


def inside(points, p):
    pts = np.array(points, np.float32)
    return len(pts) >= 3 and cv2.pointPolygonTest(pts, (float(p[0]), float(p[1])), False) >= 0


def feet(box):
    """Where a person (or vehicle) stands: the bottom middle of its box."""
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2, y2)


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def kind_matches(what, kind):
    return what == "both" or what == kind


class Tracker:
    """Follows people and vehicles from one look to the next, by where they stand."""

    def __init__(self, max_gap=4.0, max_jump=0.22):
        self.max_gap = max_gap
        self.max_jump = max_jump
        self.tracks = []
        self._next = 1

    def update(self, objects, now):
        """Returns [(track, previous point)] for tracks that moved on from an earlier look."""
        self.tracks = [t for t in self.tracks if now - t["t"] <= self.max_gap]
        moved = []
        free = list(self.tracks)
        for o in sorted(objects, key=lambda o: -o["conf"]):
            p = feet(o["box"])
            best, best_d = None, None
            for t in free:
                if t["kind"] != o["kind"]:
                    continue
                d = ((t["p"][0] - p[0]) ** 2 + (t["p"][1] - p[1]) ** 2) ** 0.5
                allowed = self.max_jump * max(1.0, now - t["t"])
                if d <= allowed and (best_d is None or d < best_d):
                    best, best_d = t, d
            if best is None:
                t = {"id": self._next, "kind": o["kind"], "p": p, "t": now, "born": now, "box": o["box"],
                     "crossed": set()}
                self._next += 1
                self.tracks.append(t)
            else:
                free.remove(best)
                prev = best["p"]
                best.update(p=p, t=now, box=o["box"])
                moved.append((best, prev))
        return moved

    def reset(self):
        self.tracks = []

    def recent(self, now, seconds):
        return any(now - t["t"] <= seconds for t in self.tracks)


# ---- parcels ---------------------------------------------------------------------------

class ParcelWatch:
    """Notices something new left on the doorstep after a visit, and it going again.

    Works on a small grey copy of the doorstep area. Changes only count once the doorstep has
    been still for a few seconds after someone was there, so shadows, rain and people walking
    past don't count.
    """

    WIDTH = 160
    CHANGE = 0.9        # how different a pixel must be (after evening out the brightness)
    MIN_BLOB = 0.025    # share of the doorstep a parcel must cover
    STEP = 1.0          # look once a second

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = "learning"     # learning, empty, parcel
        self.base = None            # the doorstep when empty
        self.ref = None             # the doorstep with the parcel
        self.blob = None            # where the parcel is
        self.since = None           # wall clock time the parcel arrived
        self.picture = None         # picture name of the parcel arrival
        self.visit = False
        self.confirm = 0
        self.prev = None
        self.calm_since = None
        self.odd_since = None
        self.last_step = 0.0
        self._key = None
        self._mask = None

    def _prepare(self, frame, area):
        H, W = frame.shape[:2]
        pts = np.array([[x * W, y * H] for x, y in area], np.float32)
        x, y, w, h = cv2.boundingRect(pts)
        x, y = max(0, x), max(0, y)
        w, h = min(W - x, w), min(H - y, h)
        if w < 4 or h < 4:
            return None
        crop = frame[y:y + h, x:x + w]
        scale = self.WIDTH / w
        size = (self.WIDTH, max(4, int(h * scale)))
        key = (frame.shape, json.dumps(area))
        if key != self._key:
            # A new doorstep area (or picture size): start again.
            last_step = self.last_step
            self.reset()
            self.last_step = last_step
            mask = np.zeros((size[1], size[0]), np.uint8)
            cv2.fillPoly(mask, [((pts - [x, y]) * scale).astype(np.int32)], 255)
            self._key, self._mask = key, mask > 0
        gray = cv2.cvtColor(cv2.resize(crop, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
        vals = gray[self._mask]
        if vals.size < 16:
            return None
        return (gray - float(vals.mean())) / max(8.0, float(vals.std()))

    def _changed(self, a, b):
        """Pixels that differ, cleaned of speckles, inside the doorstep."""
        d = (np.abs(a - b) > self.CHANGE).astype(np.uint8)
        d = cv2.morphologyEx(d, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        d = cv2.morphologyEx(d, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        return (d > 0) & self._mask

    def _largest(self, changed):
        n, labels, stats, _ = cv2.connectedComponentsWithStats(changed.astype(np.uint8), 8)
        if n <= 1:
            return None, 0.0
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        blob = labels == i
        return blob, float(blob.sum()) / max(1, int(self._mask.sum()))

    def step(self, frame, now, area, person_near):
        """Looks at the doorstep. Returns "arrived", "taken" or None."""
        if person_near:
            self.visit = True
            self.calm_since = None
        if now - self.last_step < self.STEP:
            return None
        self.last_step = now
        cur = self._prepare(frame, area)
        if cur is None:
            return None
        moving = self.prev is not None and float(np.abs(cur - self.prev)[self._mask].mean()) > 0.3
        self.prev = cur
        if person_near or moving:
            self.calm_since = None
            return None
        if self.calm_since is None:
            self.calm_since = now
        calm = now - self.calm_since
        area_px = max(1, int(self._mask.sum()))

        if self.state == "learning" or self.base is None:
            if calm >= 4:
                self.base = cur.copy()
                if self.state == "learning":
                    self.state = "empty"
                if self.ref is None:
                    self.ref = cur.copy()
                self.visit = False
            return None
        if calm < 3:
            return None

        if self.state == "empty":
            changed = self._changed(self.base, cur)
            frac = float(changed.sum()) / area_px
            blob, blob_frac = self._largest(changed)
            if self.visit and frac < 0.6 and blob_frac >= self.MIN_BLOB:
                self.confirm += 1
                if self.confirm >= 3:
                    self.state, self.ref, self.blob = "parcel", cur.copy(), blob
                    self.visit, self.confirm = False, 0
                    return "arrived"
                return None
            self.confirm = 0
            if self.visit and calm >= 8:
                self.visit = False  # someone came and went, and nothing was left
            if not self.visit:
                self._drift(frac, cur, now)
            return None

        # A parcel is waiting.
        changed = self._changed(self.ref, cur)
        frac = float(changed.sum()) / area_px
        in_blob = float((changed & self.blob).sum()) / max(1, int(self.blob.sum()))
        if self.visit and frac < 0.6 and in_blob >= 0.5:
            self.confirm += 1
            if self.confirm < 3:
                return None
            self.confirm, self.visit = 0, False
            vs_base = self._changed(self.base, cur)
            left = float((vs_base & self.blob).sum()) / max(1, int(self.blob.sum()))
            if left < 0.35:
                self.state, self.blob, self.since, self.picture = "empty", None, None, None
                self.base = cur.copy()
                self.ref = cur.copy()
                return "taken"
            # Something is still there (moved, or another one added): keep watching that.
            blob, blob_frac = self._largest(vs_base)
            self.ref = cur.copy()
            if blob is not None and blob_frac >= self.MIN_BLOB:
                self.blob = blob
            return None
        self.confirm = 0
        if self.visit and calm >= 8:
            self.visit = False
        if not self.visit and frac < 0.15:
            # Follow slow changes in the light, on the parcel and around it.
            self.ref = 0.9 * self.ref + 0.1 * cur
            outside = self._mask & ~self.blob
            self.base[outside] = 0.9 * self.base[outside] + 0.1 * cur[outside]
        return None

    def _drift(self, frac, cur, now):
        if frac < 0.15:
            self.base = 0.95 * self.base + 0.05 * cur
            self.odd_since = None
        else:
            # A big change nobody explains (a car parked in front, the camera moved, the
            # light changed sharply): accept it after a while.
            self.odd_since = self.odd_since or now
            if now - self.odd_since > 600:
                self.base, self.odd_since = cur.copy(), None

    def clear(self):
        """The user says the doorstep is clear: learn it afresh."""
        self.reset()

    def status(self):
        return {"state": self.state, "since": self.since, "picture": self.picture}


# ---- one camera ------------------------------------------------------------------------

class CameraWatcher:
    """The smart rules for one camera. Fed by the camera's analyser with each picture it looks
    at, and with people and vehicles whenever it looked for them."""

    def __init__(self, hub, cam):
        self.hub = hub
        self.cam = cam
        self.id = cam.id
        self._lock = threading.RLock()
        self.tracker = Tracker()
        self.loiter = {}            # area id -> {"since", "last", "alerted"}
        self.line_last = {}         # line id -> time of last alert
        self.visit = None
        self._visits = 0
        self.parked = []            # vehicles standing still: {"box", "sig", "last", "misses"}
        self.candidates = []        # vehicles that might be stopping
        self.departed = collections.deque(maxlen=10)
        # Looks still to take at vehicles already there when watching starts (or the camera
        # stops turning): those count as parked, not arriving.
        self.survey = 0
        self._vehicles_on = False
        self.parcel = ParcelWatch()
        self.last_people = 0.0
        self.last_people_boxes = []
        self.frame = None
        self.was_turning = False

    @property
    def name(self):
        return self.cam.cfg.get("name") or self.id

    @property
    def rules(self):
        return self.hub.camera_settings(self.id)

    # ---- what the camera asks ----

    def wants_detection(self, now):
        with self._lock:
            if self.tracker.recent(now, TRACK_MEMORY):
                return True
            if any(s.get("since") is not None for s in self.loiter.values()):
                return True
            if self.visit is not None and now - self.visit["last"] < VISIT_GAP:
                return True
            return bool(self.candidates) or self.survey > 0

    def detect_interval(self):
        r = self.rules
        return 0.5 if any(ln["enabled"] for ln in r["lines"]) else 1.0

    # ---- each picture ----

    def on_frame(self, frame, now, moving, objects, turning):
        with self._lock:
            self.frame = frame
            if turning:
                if not self.was_turning:
                    self._camera_turned()
                self.was_turning = True
                return
            self.was_turning = False
            on = bool(self.hub.settings["vehicles"]["enabled"])
            if on and not self._vehicles_on:
                self.survey = 3
            self._vehicles_on = on
            rules = self.rules
            if objects is not None:
                self._on_objects(frame, now, objects, rules)
            self._check_visit(now)
            p = rules["parcel"]
            if p["enabled"] and p["area"]:
                near = self._person_near(p["area"], now)
                result = self.parcel.step(frame, now, p["area"], near)
                if result:
                    self._parcel_event(result, frame, p["area"])

    def _camera_turned(self):
        self.survey = 3
        self.tracker.reset()
        self.loiter.clear()
        self.parked.clear()
        self.candidates.clear()
        self.parcel.visit = False
        self.parcel.calm_since = None

    def _person_near(self, area, now):
        if now - self.last_people > 6:
            return False
        xs, ys = [p[0] for p in area], [p[1] for p in area]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        mx, my = (x2 - x1) * 0.5, (y2 - y1) * 0.5
        near = (x1 - mx, y1 - my, x2 + mx, y2 + my)
        return any(b[0] < near[2] and b[2] > near[0] and b[1] < near[3] and b[3] > near[1]
                   for b in self.last_people_boxes)

    def _on_objects(self, frame, now, objects, rules):
        people = [o for o in objects if o["kind"] == "person"]
        vehicles = [o for o in objects if o["kind"] == "vehicle"]
        if people:
            self.last_people = now
            self.last_people_boxes = [o["box"] for o in people]
        moved = self.tracker.update(objects, now)

        for t, prev in moved:
            for ln in rules["lines"]:
                if not ln["enabled"] or not kind_matches(ln["what"], t["kind"]):
                    continue
                if ln["id"] in t["crossed"]:
                    continue
                c = crossing(ln["a"], ln["b"], prev, t["p"])
                if c == 0 or (ln["direction"] == "arrow" and c != 1):
                    continue
                t["crossed"].add(ln["id"])
                if now - self.line_last.get(ln["id"], 0) < 8:
                    continue
                self.line_last[ln["id"]] = now
                self._line_event(ln, t, frame)

        for ar in rules["areas"]:
            if not ar["enabled"]:
                continue
            st = self.loiter.setdefault(ar["id"], {"since": None, "last": 0.0, "alerted": False})
            here = [o for o in objects if kind_matches(ar["what"], o["kind"]) and inside(ar["points"], feet(o["box"]))]
            if here:
                if st["since"] is None:
                    st["since"], st["alerted"] = now, False
                st["last"] = now
                if not st["alerted"] and now - st["since"] >= ar["seconds"]:
                    st["alerted"] = True
                    self._loiter_event(ar, here, now - st["since"], frame)
            elif st["since"] is not None and now - st["last"] > 10:
                st["since"], st["alerted"] = None, False
        known = {a["id"] for a in rules["areas"]}
        for k in list(self.loiter):
            if k not in known:
                del self.loiter[k]

        counted = [o for o in people if not o["ignored"]]
        if counted:
            self._visit_seen(frame, now, counted)
        if self._vehicles_on:
            self._vehicles(frame, now, [o for o in vehicles if not o["ignored"]])

    # ---- faces ----

    def _visit_seen(self, frame, now, people):
        if not self.hub.faces_on():
            return
        v = self.visit
        if v is None or now - v["last"] > VISIT_GAP:
            self._end_visit()
            self._visits += 1
            wait = self.hub.settings["faces"]["wait_seconds"]
            v = self.visit = {"id": self._visits, "start": now, "last": now, "deadline": now + wait,
                              "next_look": now, "known": {}, "unknown": 0, "faces": 0, "best": None,
                              "decided": False, "stranger_sent": False, "frame": None, "boxes": []}
        v["last"] = now
        v["frame"], v["boxes"] = frame, [o["box"] for o in people]
        if now >= v["next_look"] and self.hub.look_for_faces(self, v["id"], frame, v["boxes"]):
            v["next_look"] = now + LOOK_EVERY

    def on_faces(self, visit_id, found, picture):
        """Called from the face worker with the faces it found in one look."""
        with self._lock:
            v = self.visit
            if v is None or v["id"] != visit_id:
                return
            store = self.hub.store
            threshold = facemod.FACE_MATCH[self.hub.settings["faces"]["strictness"]]
            for f in found:
                if f["size"] < facemod.MIN_FACE_PX:
                    continue
                v["faces"] += 1
                person, likeness = store.match_face(f["feature"], threshold)
                if person is not None:
                    first = person["name"] not in v["known"]
                    v["known"][person["name"]] = likeness
                    if first:
                        self._known_event(person["name"], f, picture)
                    continue
                if f["score"] < 0.85:
                    continue
                v["unknown"] += 1
                if v["best"] is None or f["size"] > v["best"]["size"]:
                    v["best"] = dict(f, picture=picture)
            if not v["stranger_sent"] and v["best"] is not None:
                sure = v["best"]["size"] >= facemod.GOOD_FACE_PX
                if v["unknown"] >= 2 and (sure or v["unknown"] >= 3):
                    self._stranger_event(v, confirmed=True)

    def _check_visit(self, now):
        v = self.visit
        if v is None:
            return
        if not v["decided"] and now >= v["deadline"]:
            v["decided"] = True
            if not v["stranger_sent"]:
                if v["unknown"] >= 1:
                    self._stranger_event(v, confirmed=True)
                elif not v["known"] and self.hub.settings["faces"]["no_face"] == "alert":
                    self._stranger_event(v, confirmed=False)
        if now - v["last"] > VISIT_GAP:
            self._end_visit()

    def _end_visit(self):
        v, self.visit = self.visit, None
        if v is None or v.get("saved") or v["best"] is None:
            return
        self._save_face(v)

    def _save_face(self, v):
        b = v["best"]
        v["saved"] = True
        self.hub.store.add_seen("face", self.id, self.name, b["crop"], feature=b["feature"])

    def _stranger_event(self, v, confirmed):
        v["stranger_sent"] = True
        v["decided"] = True
        b = v["best"]
        picture = b["picture"] if b else None
        if picture is None and v["frame"] is not None:
            picture = draw(v["frame"], boxes=v["boxes"])
        if confirmed and b is not None and not v.get("saved"):
            self._save_face(v)
        if confirmed:
            title, msg = f"{self.name}: stranger", "Someone you haven't named is at the camera. Tap to watch."
        else:
            title, msg = f"{self.name}: person seen", "Their face couldn't be seen, so it may be family. Tap to watch."
        self.cam.add_seen("Stranger" if confirmed else "Person")
        self.hub.publish(self, "stranger", title, msg, picture,
                         {"stranger": True, "person": True, "unconfirmed": not confirmed,
                          "known_people": sorted(v["known"])})

    def _known_event(self, name, face, picture):
        self.cam.add_seen(name)
        self.hub.publish(self, "known_person", f"{self.name}: {name}", f"{name} was recognised.", picture,
                         {"person_name": name, "alert": False})

    # ---- vehicles ----

    def _vehicles(self, frame, now, vehicles):
        if self.survey > 0:
            self.survey -= 1
            for o in vehicles:
                if not any(iou(p["box"], o["box"]) >= 0.4 for p in self.parked):
                    self.parked.append({"box": o["box"], "sig": facemod.vehicle_signature(frame, o["box"]),
                                        "last": now, "misses": 0})
            self.candidates.clear()
            return
        matched = set()
        for o in vehicles:
            box = o["box"]
            p = next((p for p in self.parked if iou(p["box"], box) >= 0.4), None)
            if p is not None:
                p["last"], p["misses"] = now, 0
                matched.add(id(p))
                continue
            c = next((c for c in self.candidates if iou(c["box"], box) >= 0.3), None)
            if c is None:
                self.candidates.append({"box": box, "prev": box, "first": now, "last": now, "hits": 1})
            else:
                c["prev"], c["box"], c["last"] = c["box"], box, now
                c["hits"] += 1
        for p in list(self.parked):
            if id(p) in matched:
                continue
            p["misses"] += 1
            if p["misses"] >= 4 and now - p["last"] >= 8:
                self.parked.remove(p)
                self.departed.append({"box": p["box"], "sig": p["sig"], "time": now})
        for c in list(self.candidates):
            if now - c["last"] > 6:
                self.candidates.remove(c)  # drove past
                continue
            still = iou(c["prev"], c["box"]) >= 0.6
            if c["hits"] >= 3 and now - c["first"] >= 2.5 and still:
                self.candidates.remove(c)
                self._vehicle_stopped(frame, now, c["box"])

    def _vehicle_stopped(self, frame, now, box):
        sig = facemod.vehicle_signature(frame, box)
        self.parked.append({"box": box, "sig": sig, "last": now, "misses": 0})
        for d in self.departed:
            if now - d["time"] < 900 and iou(d["box"], box) >= 0.3 and facemod.vehicle_likeness(d["sig"], sig) >= 0.8:
                return  # the same vehicle, lost for a moment
        vs = self.hub.settings["vehicles"]
        car, likeness = self.hub.store.match_car(sig, facemod.CAR_MATCH[vs["strictness"]])
        crop = facemod.box_crop(frame, box)
        picture = draw(frame, boxes=[box])
        if car is not None:
            self.cam.add_seen(car["name"])
            self.hub.publish(self, "vehicle", f"{self.name}: {car['name']} is here",
                             f"{car['name']} arrived.", picture,
                             {"known": True, "vehicle": car["name"], "alert": bool(vs["alert_known"])})
            return
        self.cam.add_seen("Unknown vehicle")
        self.hub.store.add_seen("vehicle", self.id, self.name, crop, sig=sig)
        self.hub.publish(self, "vehicle", f"{self.name}: unknown vehicle",
                         "A vehicle you haven't marked as yours has stopped. Tap to watch.", picture,
                         {"known": False})

    # ---- lines, areas and parcels ----

    def _line_event(self, ln, track, frame):
        who = "Someone" if track["kind"] == "person" else "A vehicle"
        self.cam.add_seen(f"Crossed {ln['name']}")
        self.hub.publish(self, "line_crossed", f"{self.name}: {ln['name']}",
                         f"{who} crossed {ln['name']}.", draw(frame, boxes=[track["box"]], line=ln),
                         {"rule": ln["name"], "what": track["kind"], "person": track["kind"] == "person"})

    def _loiter_event(self, ar, objs, seconds, frame):
        kind = "person" if any(o["kind"] == "person" for o in objs) else "vehicle"
        who = "Someone" if kind == "person" else "A vehicle"
        self.cam.add_seen(f"Loitering by {ar['name']}")
        self.hub.publish(self, "loitering", f"{self.name}: loitering by {ar['name']}",
                         f"{who} has been by {ar['name']} for {fmt_secs(seconds)}.",
                         draw(frame, boxes=[o["box"] for o in objs], area=ar["points"]),
                         {"rule": ar["name"], "what": kind, "seconds": int(seconds), "person": kind == "person"})

    def _parcel_event(self, result, frame, area):
        if result == "arrived":
            self.cam.add_seen("Parcel")
            pic = draw(frame, area=area)
            name = self.hub.publish(self, "parcel_arrived", f"{self.name}: parcel delivered",
                                    "Something was left on the doorstep. Tap to see.", pic, {})
            self.parcel.since = datetime.datetime.now().isoformat(timespec="seconds")
            self.parcel.picture = name
            return
        away = not self.hub.someone_home()
        always = self.hub.settings["parcels"]["taken_alert"] == "always"
        self.cam.add_seen("Parcel taken")
        self.hub.publish(self, "parcel_taken",
                         f"{self.name}: parcel taken" if away else f"{self.name}: parcel picked up",
                         ("The parcel on the doorstep was taken while nobody was home. Tap to watch."
                          if away else "The parcel on the doorstep was picked up."),
                         draw(frame, area=area), {"away": away, "alert": away or always})

    def parcel_status(self):
        with self._lock:
            return self.parcel.status()

    def clear_parcel(self):
        with self._lock:
            self.parcel.clear()


def fmt_secs(s):
    s = int(s)
    if s < 90:
        return f"{s} seconds"
    return f"{round(s / 60)} minutes"


def draw(frame, boxes=(), line=None, area=None, max_w=1280):
    """A copy of the picture with what was seen marked on it, as a JPEG."""
    if frame is None:
        return None
    img = frame.copy()
    h, w = img.shape[:2]
    s = min(1.0, max_w / w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)))
        h, w = img.shape[:2]
    t = max(2, w // 400)
    if area:
        pts = np.array([[int(x * w), int(y * h)] for x, y in area], np.int32)
        cv2.polylines(img, [pts], True, (255, 180, 60), t)
    if line:
        a = (int(line["a"][0] * w), int(line["a"][1] * h))
        b = (int(line["b"][0] * w), int(line["b"][1] * h))
        cv2.line(img, a, b, (255, 180, 60), t)
    for x1, y1, x2, y2 in boxes or []:
        cv2.rectangle(img, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 200, 255), t)
    return facemod.jpeg(img, 82)


# ---- the whole PC ----------------------------------------------------------------------

class SmartHub:
    ACTIVITY_KEEP = 150

    def __init__(self, hub, engine=None, start_worker=True):
        self.hub = hub
        self.config = hub.config
        self.events = hub.events
        folder = os.path.join(os.path.dirname(self.config.path), "smart")
        self.store = facemod.KnownStore(folder)
        self.engine = engine if engine is not None else facemod.FaceEngine()
        self._lock = threading.RLock()
        self.watchers = {}
        self.activity = collections.deque(maxlen=self.ACTIVITY_KEEP)
        self._log_path = os.path.join(folder, "activity.jsonl")
        self._load_activity()
        self._ensure_section()
        self._jobs = queue.Queue(maxsize=3)
        if start_worker:
            threading.Thread(target=self._work, name="smart-faces", daemon=True).start()

    # ---- settings ----

    def _ensure_section(self):
        cur = self.config.data.get("smart")
        cur = cur if isinstance(cur, dict) else {}
        merged = copy.deepcopy(DEFAULT_SMART)
        for k in ("faces", "vehicles", "parcels"):
            if isinstance(cur.get(k), dict):
                merged = clean_settings({k: cur[k]}, merged)
        cams = cur.get("cameras") if isinstance(cur.get("cameras"), dict) else {}
        merged["cameras"] = {str(k): clean_camera(v) for k, v in cams.items()}
        self.config.data["smart"] = merged

    @property
    def settings(self):
        return self.config.data["smart"]

    def camera_settings(self, cam_id):
        return self.settings["cameras"].get(cam_id) or DEFAULT_CAMERA_SMART

    def update_settings(self, body):
        with self._lock:
            new = clean_settings(body, self.settings)
            self.config.data["smart"] = new
            self.config.save()
            return new

    def update_camera(self, cam_id, body):
        with self._lock:
            cleaned = clean_camera(body)
            self.settings["cameras"][cam_id] = cleaned
            self.config.save()
            return cleaned

    def faces_on(self):
        return bool(self.settings["faces"]["enabled"]) and self.engine.available

    def someone_home(self):
        pr = getattr(self.hub, "presence", None)
        try:
            return pr is not None and pr.state()["where"] == "home"
        except Exception:
            return False

    # ---- cameras ----

    def watcher(self, cam):
        w = CameraWatcher(self, cam)
        self.watchers[cam.id] = w
        return w

    # ---- events ----

    def tag_event(self, event):
        """While faces are being recognised, the ordinary "person seen" alert waits for the face
        check, which sends "stranger" instead (or nothing for family)."""
        if not self.faces_on():
            return event
        t = event.get("type")
        if (t == "person" or (t == "clip_started" and event.get("person"))) and "alert" not in event:
            event["alert"] = False
            event["held"] = "face check"
        return event

    def publish(self, watcher, kind, title, message, picture_jpeg, extra):
        name = self.store.put_pic(picture_jpeg, "e") if picture_jpeg else None
        event = {"type": kind, "smart": True, "camera": watcher.id, "camera_name": watcher.name,
                 "time": datetime.datetime.now().isoformat(timespec="seconds"),
                 "title": title, "message": message,
                 "picture": f"/api/smart/pics/{name}" if name else None}
        clip = getattr(watcher.cam, "clip", None)
        if clip is not None:
            event["clip"] = clip.clip_id
        event.update(extra or {})
        log.info("[%s] %s", watcher.id, title)
        self.events.publish(event)
        rec = {k: event.get(k) for k in ("type", "camera", "camera_name", "time", "title", "message",
                                          "picture", "clip", "alert")}
        with self._lock:
            self.activity.appendleft(rec)
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass
        self.store.prune_event_pics()
        return name

    def _load_activity(self):
        try:
            with open(self._log_path, encoding="utf-8") as f:
                lines = f.readlines()[-self.ACTIVITY_KEEP:]
        except OSError:
            return
        for ln in lines:
            try:
                self.activity.appendleft(json.loads(ln))
            except ValueError:
                pass
        try:
            with open(self._log_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass

    # ---- face checks, one at a time in the background ----

    def look_for_faces(self, watcher, visit_id, frame, boxes):
        try:
            self._jobs.put_nowait((watcher, visit_id, frame, list(boxes)))
            return True
        except queue.Full:
            return False

    def _work(self):
        while True:
            job = self._jobs.get()
            try:
                self.run_face_job(*job)
            except Exception:
                log.exception("Face check failed")

    def run_face_job(self, watcher, visit_id, frame, boxes):
        img = None
        if hasattr(watcher.cam, "hires_frame"):
            img = watcher.cam.hires_frame()
        if img is None:
            img = frame
        H, W = img.shape[:2]
        found = []
        for x1, y1, x2, y2 in boxes:
            # The head is in the top part of the person. Allow for a little movement since.
            bw, bh = x2 - x1, y2 - y1
            a, b = max(0.0, x1 - bw * 0.15), max(0.0, y1 - bh * 0.1)
            c, d = min(1.0, x2 + bw * 0.15), min(1.0, y1 + bh * 0.65)
            px1, py1, px2, py2 = int(a * W), int(b * H), int(c * W), int(d * H)
            if px2 - px1 < 16 or py2 - py1 < 16:
                continue
            for f in self.engine.faces(img[py1:py2, px1:px2]):
                fx, fy, fw, fh = f["box"]
                f["box"] = (fx + px1, fy + py1, fw, fh)
                found.append(f)
        picture = draw(img, boxes=[(f["box"][0] / W, f["box"][1] / H, (f["box"][0] + f["box"][2]) / W,
                                    (f["box"][1] + f["box"][3]) / H) for f in found] or boxes)
        watcher.on_faces(visit_id, found, picture)

    # ---- enrolling from photos and the live picture ----

    def add_photo(self, person_id, data):
        img = facemod.decode_image(data)
        if img is None:
            raise ValueError("That file isn't a picture this PC can read")
        if not self.engine.available:
            raise ValueError("Face recognition isn't installed on this PC. Reinstall Home Camera.")
        face = self.engine.largest_face(img)
        if face is None or face["size"] < facemod.MIN_FACE_PX:
            raise ValueError("No clear face found in that picture. Try one where the face is larger and facing the camera.")
        f = self.store.add_face(person_id, face["feature"], face["crop"])
        if f is None:
            raise KeyError("Not found")
        return f

    def add_car_from_live(self, car_id, cam_id):
        cam = self.hub.cameras.get(cam_id)
        if cam is None:
            raise KeyError("No such camera")
        w = self.watchers.get(cam_id)
        frame = w.frame if w is not None else None
        if frame is None:
            raise ValueError("The camera's picture isn't ready yet")
        found = [o for o in cam.detector.detect_objects(frame, 0.9, 0.35) if o[0] == "vehicle"]
        if not found:
            raise ValueError("No vehicle can be seen in the live picture right now")
        _, _, box = max(found, key=lambda o: (o[2][2] - o[2][0]) * (o[2][3] - o[2][1]))
        sig = facemod.vehicle_signature(frame, box)
        s = self.store.add_car_sample(car_id, sig, facemod.box_crop(frame, box))
        if s is None:
            raise KeyError("Not found")
        return s

    # ---- for the viewer ----

    def state(self):
        cams = {}
        for cam_id, cam in self.hub.cameras.items():
            w = self.watchers.get(cam_id)
            cams[cam_id] = {"name": cam.cfg.get("name") or cam_id, **copy.deepcopy(self.camera_settings(cam_id)),
                            "parcel_status": w.parcel_status() if w else None}
        return {
            "settings": {k: v for k, v in copy.deepcopy(self.settings).items() if k != "cameras"},
            "faces_available": self.engine.available,
            "detector_available": bool(getattr(self.hub.detector, "available", False)),
            "cameras": cams,
            **self.store.public(),
            "activity": list(self.activity)[:60],
        }
