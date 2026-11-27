"""The sightings log: every person and number plate seen on your property, and the
recordings they're in, so you can search them.

- People: when someone has been and gone, one entry for each person, with the best picture
  of them (their face when it was seen, otherwise their build and clothes), who they were
  (family by face or build, a remembered visitor, or someone new), and the clip.
- Everyone seen is put with the earlier sightings of the same person, going by their face,
  or (no face) build and clothes, so you can see who has been before, how often, and every
  recording they're in. Name anyone and they can be searched by that name.
- Vehicles: when a vehicle stops, its number plate is read (plates.py) and logged with a
  picture of the vehicle and the plate. Search a plate, even with a character or two wrong.
- Search by a photo: the face (or the whole person) in a photo you give is compared with
  everyone in the log.

UK GDPR and privacy:
- Only people and vehicles on your property are logged: you draw it per camera (Smart tab),
  and nothing at all is logged by a camera until you have. People and vehicles in a privacy
  area are never logged.
- Entries are deleted after the number of days you choose (30 unless you change it), with
  their pictures. You can delete one entry, everything about one person or plate, or the lot.
- Everything stays on this PC: smart/sightings.db next to config.json, pictures in smart/pics.

Settings in config.json under smart.log: {"people": bool, "plates": bool, "days": 30}.
"""
import datetime
import json
import logging
import os
import queue
import sqlite3
import threading
import time

import numpy as np

from . import bodies as bodymod
from . import faces as facemod
from . import plates as platemod

log = logging.getLogger("camhub.sightings")

DEFAULT_LOG = {"people": False, "plates": False, "days": 30}
TIDY_EVERY = 3600
# Grouping people seen with earlier sightings of the same person. A face must be at least as
# alike as for recognising family; build and clothes alone must be very alike.
GROUP_FACE = facemod.FACE_MATCH["normal"]
GROUP_BODY = bodymod.VISITOR_BODY["strict"]
FACE_DIFFERENT = 0.2
# Searching with a photo is a little more forgiving: you then look at what it found.
PHOTO_FACE = facemod.FACE_MATCH["relaxed"] - 0.04
PHOTO_BODY = bodymod.VISITOR_BODY["relaxed"] - 0.03
PLATE_SAME = 0.85       # plates this alike (one character in seven) are taken as the same car
PLATE_SEARCH = 0.7

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, time TEXT NOT NULL, camera TEXT, camera_name TEXT,
    clip TEXT, grp TEXT, known INTEGER DEFAULT 0, by TEXT, pic TEXT, quality REAL DEFAULT 0,
    face BLOB, body BLOB, clothes TEXT, night INTEGER DEFAULT 0, plate TEXT, plate_sure REAL,
    plate_pic TEXT, visit TEXT
);
CREATE INDEX IF NOT EXISTS sightings_time ON sightings(time);
CREATE INDEX IF NOT EXISTS sightings_grp ON sightings(grp);
CREATE INDEX IF NOT EXISTS sightings_plate ON sightings(plate);
CREATE TABLE IF NOT EXISTS groups (
    grp TEXT PRIMARY KEY, kind TEXT, label TEXT, number INTEGER
);
"""


def clean_settings(body, current=None):
    out = dict(DEFAULT_LOG)
    out.update(current or {})
    body = body if isinstance(body, dict) else {}
    for k in ("people", "plates"):
        if k in body:
            out[k] = bool(body[k])
    if "days" in body:
        try:
            out["days"] = max(1, min(365, int(body["days"])))
        except (TypeError, ValueError):
            pass
    return out


def _blob(v):
    return None if v is None else facemod.unit(v).astype(np.float16).tobytes()


def _vec(b):
    return None if not b else np.frombuffer(b, np.float16).astype(np.float32)


def _now():
    return datetime.datetime.now()


def _day_start(day):
    try:
        return datetime.date.fromisoformat(str(day)).isoformat()
    except ValueError:
        return None


class SightingLog:
    def __init__(self, folder, known, threaded=True):
        self.folder = folder
        self.known = known              # faces.KnownStore: pictures, and family names
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "sightings.db")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._jobs = queue.Queue(maxsize=50)
        self._threaded = threaded
        self._last_tidy = 0.0
        self.days = DEFAULT_LOG["days"]
        if threaded:
            threading.Thread(target=self._work, name="sightings", daemon=True).start()

    # ---- background work (pictures and plates are slow-ish) ----

    def submit(self, fn, *args):
        if not self._threaded:
            fn(*args)
            return True
        try:
            self._jobs.put_nowait((fn, args))
            return True
        except queue.Full:
            log.warning("Sightings log is busy, skipped one")
            return False

    def _work(self):
        while True:
            try:
                fn, args = self._jobs.get(timeout=60)
            except queue.Empty:
                fn = None
            try:
                if fn is not None:
                    fn(*args)
                if time.time() - self._last_tidy > TIDY_EVERY:
                    self.tidy(self.days)
            except Exception:
                log.exception("Sightings log job failed")

    # ---- groups: the same person, or the same plate ----

    def _next_number(self, kind):
        r = self.db.execute("SELECT MAX(number) FROM groups WHERE kind = ?", (kind,)).fetchone()
        return (r[0] or 0) + 1

    def _ensure_group(self, grp, kind, label=None):
        r = self.db.execute("SELECT label FROM groups WHERE grp = ?", (grp,)).fetchone()
        if r is None:
            self.db.execute("INSERT INTO groups (grp, kind, label, number) VALUES (?, ?, ?, ?)",
                            (grp, kind, label or "", self._next_number(kind)))
        elif label and grp.startswith(("p:", "car:")) and r["label"] != label:
            self.db.execute("UPDATE groups SET label = ? WHERE grp = ?", (label, grp))  # family renamed

    def _person_group(self, face, body, clothes, night):
        """The earlier unknown person this one is, going by face (or build and clothes)."""
        best, best_score = None, 0.0
        rows = self.db.execute("SELECT grp, face, body, clothes, night FROM sightings WHERE kind = 'person' "
                               "AND (grp LIKE 'u:%' OR grp LIKE 'v:%')").fetchall()
        face_vetoes = set()
        if face is not None:
            fu = facemod.unit(face)
            for r in rows:
                f = _vec(r["face"])
                if f is None:
                    continue
                s = float(f @ fu)
                if s < FACE_DIFFERENT:
                    face_vetoes.add(r["grp"])
                if s >= GROUP_FACE and s > best_score:
                    best, best_score = r["grp"], s
            if best:
                return best
        if body is None:
            return None
        obs = {"feature": body, "clothes": clothes, "night": night}
        for r in rows:
            b = _vec(r["body"])
            if b is None or r["grp"] in face_vetoes:
                continue
            s = bodymod.likeness(obs, [{"feature": b, "clothes": _clothes(r["clothes"]), "night": bool(r["night"])}])
            if s >= GROUP_BODY and s > best_score:
                best, best_score = r["grp"], s
        return best

    # ---- adding ----

    def add_person(self, s):
        """s: {"time", "camera", "camera_name", "clip", "visit", "person_id", "person_name", "by",
        "visitor_id", "visitor_label", "face": features or None, "face_crop", "face_size",
        "body": features or None, "body_crop", "clothes", "night"}. Returns the entry's id."""
        with self._lock:
            if s.get("person_id"):
                grp, kind, label, known = f"p:{s['person_id']}", "person", s.get("person_name"), 1
            elif s.get("visitor_id"):
                grp, kind, label, known = f"v:{s['visitor_id']}", "person", s.get("visitor_label"), 0
            else:
                grp = self._person_group(s.get("face"), s.get("body"), s.get("clothes"), s.get("night"))
                grp = grp or f"u:{facemod.new_id('')}"
                kind, label, known = "person", None, 0
            self._ensure_group(grp, kind, label)
            face_crop = s.get("face_crop")
            pic = self.known.put_pic(face_crop or s.get("body_crop"), "l")
            quality = float(s.get("face_size") or 0) if face_crop else 0.0
            sid = facemod.new_id("s_")
            self.db.execute(
                "INSERT INTO sightings (id, kind, time, camera, camera_name, clip, grp, known, by, pic, quality, "
                "face, body, clothes, night, visit) VALUES (?, 'person', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, s.get("time") or _now().isoformat(timespec="seconds"), s.get("camera"), s.get("camera_name"),
                 s.get("clip"), grp, known, s.get("by"), pic, quality, _blob(s.get("face")), _blob(s.get("body")),
                 _clothes_text(s.get("clothes")), int(bool(s.get("night"))), s.get("visit")))
            self.db.commit()
            return sid

    def add_vehicle(self, s):
        """s: {"id" (optional), "time", "camera", "camera_name", "clip", "crop", "car_id", "car_name"}.
        The plate is added by set_plate once read."""
        with self._lock:
            sid = s.get("id") or facemod.new_id("s_")
            grp = f"car:{s['car_id']}" if s.get("car_id") else None
            if grp:
                self._ensure_group(grp, "vehicle", s.get("car_name"))
            self.db.execute(
                "INSERT INTO sightings (id, kind, time, camera, camera_name, clip, grp, known, pic) "
                "VALUES (?, 'vehicle', ?, ?, ?, ?, ?, ?, ?)",
                (sid, s.get("time") or _now().isoformat(timespec="seconds"), s.get("camera"), s.get("camera_name"),
                 s.get("clip"), grp, int(bool(grp)), self.known.put_pic(s.get("crop"), "l")))
            self.db.commit()
            return sid

    def set_plate(self, sid, plate, sure, plate_crop=None, cars=()):
        """A plate was read for a vehicle sighting. cars: your cars, [{"id", "name", "plate"}]:
        a car whose plate matches names the sighting. Keeps the surest reading."""
        plate = platemod.clean(plate)
        if not plate:
            return False
        with self._lock:
            r = self.db.execute("SELECT plate_sure, plate_pic, grp FROM sightings WHERE id = ?", (sid,)).fetchone()
            if r is None or (r["plate_sure"] or 0) >= sure:
                return False
            # The same car seen before, allowing for a misread character.
            same = None
            for c in cars:
                if c.get("plate") and platemod.similar(c["plate"], plate) >= PLATE_SAME:
                    same = (f"car:{c['id']}", c["name"])
                    break
            if same is None:
                seen = self.db.execute("SELECT plate, COUNT(*) n FROM sightings WHERE plate IS NOT NULL AND id != ? "
                                       "GROUP BY plate ORDER BY n DESC", (sid,)).fetchall()
                for x in seen:
                    if x["plate"] == plate or platemod.similar(x["plate"], plate) >= PLATE_SAME:
                        g = self.db.execute("SELECT grp FROM sightings WHERE plate = ? AND grp IS NOT NULL "
                                            "ORDER BY time DESC LIMIT 1", (x["plate"],)).fetchone()
                        if g:
                            same = (g["grp"], None)
                            break
            grp, label = same or (r["grp"] if r["grp"] and r["grp"].startswith("car:") else f"plate:{plate}", None)
            self._ensure_group(grp, "vehicle", label)
            self.known._drop_pic(r["plate_pic"])
            self.db.execute("UPDATE sightings SET plate = ?, plate_sure = ?, plate_pic = ?, grp = ?, known = ? "
                            "WHERE id = ?", (plate, round(float(sure), 3), self.known.put_pic(plate_crop, "l"),
                                             grp, int(grp.startswith("car:")), sid))
            self.db.commit()
            return True

    # ---- tidying and deleting ----

    def _delete_rows(self, rows):
        for r in rows:
            self.known._drop_pic(r["pic"])
            self.known._drop_pic(r["plate_pic"])
        self.db.executemany("DELETE FROM sightings WHERE id = ?", [(r["id"],) for r in rows])
        self.db.execute("DELETE FROM groups WHERE grp NOT IN (SELECT DISTINCT grp FROM sightings WHERE grp IS NOT NULL)")
        self.db.commit()
        return len(rows)

    def tidy(self, days):
        """Deletes entries older than `days` days, with their pictures."""
        self._last_tidy = time.time()
        cut = (_now() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self.db.execute("SELECT id, pic, plate_pic FROM sightings WHERE time < ?", (cut,)).fetchall()
            return self._delete_rows(rows)

    def delete(self, sid):
        with self._lock:
            rows = self.db.execute("SELECT id, pic, plate_pic FROM sightings WHERE id = ?", (sid,)).fetchall()
            return self._delete_rows(rows) > 0

    def delete_group(self, grp):
        with self._lock:
            rows = self.db.execute("SELECT id, pic, plate_pic FROM sightings WHERE grp = ?", (grp,)).fetchall()
            n = self._delete_rows(rows)
            self.db.execute("DELETE FROM groups WHERE grp = ?", (grp,))
            self.db.commit()
            return n

    def clear(self):
        with self._lock:
            rows = self.db.execute("SELECT id, pic, plate_pic FROM sightings").fetchall()
            n = self._delete_rows(rows)
            self.db.execute("DELETE FROM groups")
            self.db.commit()
            return n

    def rename_group(self, grp, label):
        label = " ".join(str(label or "").split())[:40]
        with self._lock:
            r = self.db.execute("UPDATE groups SET label = ? WHERE grp = ?", (label, grp))
            self.db.commit()
            if not r.rowcount:
                raise KeyError("Not found")
            return label

    # ---- searching ----

    def _group_info(self):
        return {r["grp"]: dict(r) for r in self.db.execute("SELECT * FROM groups")}

    @staticmethod
    def _name(g, grp):
        if g and g.get("label"):
            return g["label"]
        if not grp:
            return "Vehicle"
        if grp.startswith("plate:"):
            return platemod.pretty(grp[6:])
        n = g.get("number") if g else None
        if grp.startswith(("u:", "v:")):
            return f"Person {n}" if n else "Someone"
        if grp.startswith("p:"):
            return "Family (removed)"
        return f"Vehicle {n}" if n else "Vehicle"

    def _public(self, r, groups, clip_exists=None):
        g = groups.get(r["grp"])
        out = {"id": r["id"], "kind": r["kind"], "time": r["time"], "camera": r["camera"],
               "camera_name": r["camera_name"], "clip": r["clip"], "group": r["grp"],
               "name": self._name(g, r["grp"]), "known": bool(r["known"]), "by": r["by"], "pic": r["pic"],
               "face": r["face"] is not None, "plate": platemod.pretty(r["plate"]) if r["plate"] else None,
               "plate_sure": r["plate_sure"], "plate_pic": r["plate_pic"]}
        if clip_exists is not None and r["clip"]:
            out["clip_exists"] = clip_exists(r["clip"])
        return out

    def search(self, kind=None, q=None, group=None, day_from=None, day_to=None, camera=None,
               limit=200, clip_exists=None):
        """Entries, newest first. q is a name, or a plate (allowing for misreads)."""
        sql, args = ["SELECT * FROM sightings WHERE 1 = 1"], []
        if kind in ("person", "vehicle"):
            sql.append("AND kind = ?")
            args.append(kind)
        if group:
            sql.append("AND grp = ?")
            args.append(group)
        if camera:
            sql.append("AND camera = ?")
            args.append(camera)
        a, b = _day_start(day_from), _day_start(day_to)
        if a:
            sql.append("AND time >= ?")
            args.append(a)
        if b:
            sql.append("AND time < ?")
            args.append((datetime.date.fromisoformat(b) + datetime.timedelta(days=1)).isoformat())
        sql.append("ORDER BY time DESC, rowid DESC")
        with self._lock:
            groups = self._group_info()
            rows = self.db.execute(" ".join(sql), args).fetchall()
        q = " ".join(str(q or "").split()).lower()
        out = []
        for r in rows:
            if q and not self._matches(r, groups, q):
                continue
            out.append(self._public(r, groups, clip_exists))
            if len(out) >= limit:
                break
        return out

    def _matches(self, r, groups, q):
        name = self._name(groups.get(r["grp"]), r["grp"]).lower()
        if q in name or q in (r["camera_name"] or "").lower():
            return True
        plate, wanted = r["plate"], platemod.clean(q)
        if plate and wanted:
            if wanted in plate:
                return True
            if len(wanted) >= 5 and platemod.similar(plate, wanted) >= PLATE_SEARCH:
                return True
        return False

    def who(self, kind=None, q=None):
        """Everyone (and every vehicle) in the log: how often, first and last time, and the
        best picture. Newest first."""
        with self._lock:
            groups = self._group_info()
            sql = ("SELECT grp, kind, COUNT(*) n, MIN(time) first, MAX(time) last, "
                   "COUNT(DISTINCT substr(time, 1, 10)) days, MAX(known) known FROM sightings WHERE grp IS NOT NULL")
            args = []
            if kind in ("person", "vehicle"):
                sql += " AND kind = ?"
                args.append(kind)
            rows = self.db.execute(sql + " GROUP BY grp ORDER BY last DESC, MAX(rowid) DESC", args).fetchall()
            best = {r["grp"]: r for r in self.db.execute(
                "SELECT grp, pic, plate FROM sightings s WHERE pic IS NOT NULL AND grp IS NOT NULL "
                "ORDER BY quality ASC, time ASC")}   # the last one written per group wins: best, then newest
            plates = {r["grp"]: r["plate"] for r in self.db.execute(
                "SELECT grp, plate FROM sightings WHERE plate IS NOT NULL ORDER BY plate_sure ASC")}
        q = " ".join(str(q or "").split()).lower()
        out = []
        for r in rows:
            g = groups.get(r["grp"])
            item = {"group": r["grp"], "kind": r["kind"], "name": self._name(g, r["grp"]),
                    "named": bool(g and g.get("label")), "known": bool(r["known"]), "count": r["n"],
                    "days": r["days"], "first": r["first"], "last": r["last"],
                    "pic": best[r["grp"]]["pic"] if r["grp"] in best else None,
                    "plate": platemod.pretty(plates[r["grp"]]) if r["grp"] in plates else None,
                    "family": r["grp"].startswith("p:"), "visitor": r["grp"].startswith("v:")}
            if q:
                hay = (item["name"] + " " + (item["plate"] or "")).lower()
                pq = platemod.clean(q)
                if q not in hay and not (pq and item["plate"] and (pq in platemod.clean(item["plate"]) or (
                        len(pq) >= 5 and platemod.similar(item["plate"], pq) >= PLATE_SEARCH))):
                    continue
            out.append(item)
        return out

    def by_photo(self, face=None, body=None, limit=12):
        """People in the log most like a photo: [{"group", "likeness"}], best first."""
        scores = {}
        with self._lock:
            rows = self.db.execute("SELECT grp, face, body, clothes, night FROM sightings "
                                   "WHERE kind = 'person' AND grp IS NOT NULL").fetchall()
        if face is not None:
            fu = facemod.unit(face)
            for r in rows:
                f = _vec(r["face"])
                if f is not None:
                    s = float(f @ fu)
                    if s >= PHOTO_FACE:
                        scores[r["grp"]] = max(scores.get(r["grp"], 0.0), 1.0 + s)
        elif body is not None:
            obs = {"feature": body, "clothes": None, "night": False}
            for r in rows:
                b = _vec(r["body"])
                if b is not None:
                    s = bodymod.likeness(obs, [{"feature": b, "clothes": None, "night": bool(r["night"])}])
                    if s >= PHOTO_BODY:
                        scores[r["grp"]] = max(scores.get(r["grp"], 0.0), s)
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:limit]
        return [{"group": g, "likeness": round(s - 1.0 if s > 1 else s, 3), "by": "face" if s > 1 else "body"}
                for g, s in ranked]

    def stats(self):
        with self._lock:
            r = self.db.execute("SELECT COUNT(*) n, SUM(kind = 'person') people, SUM(kind = 'vehicle') vehicles, "
                                "MIN(time) oldest FROM sightings").fetchone()
        return {"entries": r["n"] or 0, "people": r["people"] or 0, "vehicles": r["vehicles"] or 0,
                "oldest": r["oldest"]}


def _clothes_text(c):
    if c is None:
        return None
    try:
        return json.dumps(c)
    except (TypeError, ValueError):
        return None


def _clothes(t):
    if not t:
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None
