"""Visitor memory: people you haven't named who keep coming back.

Each visit by someone the PC doesn't know is remembered: a face (when one was seen), a few
pictures of their build and clothes, how tall they are, and the clip. The next unknown
visitor is compared with those remembered. The same face, or (when no face was seen) the same
build, height and clothes, counts as the same visitor. Someone seen the chosen number of times
within the chosen days is a "returning stranger", and their visits are shown side by side.

Kept in smart/visitors.json next to config.json, with small pictures in smart/pics/. Visitors
not seen for a while (30 days unless you choose otherwise) are forgotten automatically, so
nothing is kept about people who were only passing.
"""
import datetime
import json
import os
import threading

import numpy as np

from . import bodies
from . import faces as facemod

MAX_VISITORS = 150
MAX_VISITS = 30       # per visitor, newest kept
MAX_FACES = 6
MAX_BODIES = 6
FACE_DIFFERENT = 0.2  # faces this unalike are different people, whatever their clothes


def _now():
    return datetime.datetime.now()


def _parse(iso):
    try:
        return datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


class VisitorStore:
    def __init__(self, folder, known):
        self.folder = folder
        self.known = known          # faces.KnownStore, for pictures
        self._lock = threading.RLock()
        self._cache = {}
        try:
            with open(os.path.join(folder, "visitors.json"), encoding="utf-8") as f:
                self.visitors = json.load(f).get("visitors", [])
        except (OSError, ValueError, AttributeError):
            self.visitors = []
        if not isinstance(self.visitors, list):
            self.visitors = []
        self._counter = max([v.get("number", 0) for v in self.visitors] + [0])

    def save(self):
        with self._lock:
            p = os.path.join(self.folder, "visitors.json")
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"visitors": self.visitors}, f)
            os.replace(p + ".tmp", p)

    # ---- comparing ----

    def _vec(self, packed):
        v = self._cache.get(packed)
        if v is None:
            v = bodies.unpack(packed)
            if v is not None:
                self._cache[packed] = v
        return v

    def likeness(self, visitor, sighting, strictness):
        """(same?, how sure) for a sighting and a remembered visitor."""
        face_sim = None
        if sighting.get("faces") and visitor.get("faces"):
            mine = np.float32([self._vec(f["f"]) for f in visitor["faces"] if self._vec(f["f"]) is not None])
            if len(mine):
                face_sim = max(float(np.max(mine @ facemod.unit(f))) for f in sighting["faces"])
        if face_sim is not None:
            if face_sim >= facemod.FACE_MATCH[strictness]:
                return True, 1.0 + face_sim
            if face_sim < FACE_DIFFERENT:
                return False, 0.0
        samples = [{"feature": self._vec(b["f"]), "clothes": b.get("clothes"), "night": b.get("night")}
                   for b in visitor.get("bodies", [])]
        samples = [s for s in samples if s["feature"] is not None]
        best = 0.0
        for obs in sighting.get("bodies", []):
            best = max(best, bodies.likeness(obs, samples, visitor.get("height")))
        return best >= bodies.VISITOR_BODY[strictness], best

    def match(self, sighting, strictness):
        """The remembered visitor most like this sighting, or None."""
        best, best_score = None, 0.0
        with self._lock:
            for v in self.visitors:
                same, score = self.likeness(v, sighting, strictness)
                if same and score > best_score:
                    best, best_score = v, score
        return best

    def visits_within(self, visitor, days, now=None):
        now = now or _now()
        start = now - datetime.timedelta(days=days)
        return sum(1 for x in visitor.get("visits", []) if (_parse(x.get("time")) or start) > start)

    # ---- remembering ----

    def record(self, sighting, strictness, visit_key, when=None):
        """Remembers a visit. Returns (visitor, new visitor?). The same visit_key (one visit by
        the camera) adds to a visitor only once, even if the person was lost and found again."""
        when = when or _now().isoformat(timespec="seconds")
        with self._lock:
            v = self.match(sighting, strictness)
            new = v is None
            if new:
                self._counter += 1
                v = {"id": facemod.new_id("vis_"), "number": self._counter, "label": "", "regular": False,
                     "first": when, "last": when, "faces": [], "bodies": [], "height": None, "visits": []}
                self.visitors.insert(0, v)
            for f in sighting.get("faces", [])[:2]:
                v["faces"].append({"f": bodies.pack(f)})
            v["faces"] = v["faces"][-MAX_FACES:]
            for b in sighting.get("bodies", [])[:2]:
                if b.get("feature") is not None:
                    v["bodies"].append({"f": bodies.pack(b["feature"]), "clothes": b.get("clothes"),
                                        "night": bool(b.get("night")), "height": b.get("height")})
            v["bodies"] = v["bodies"][-MAX_BODIES:]
            heights = [b["height"] for b in v["bodies"] if b.get("height")]
            v["height"] = round(float(np.median(heights)), 3) if heights else None
            if not v.get("face_pic") and sighting.get("face_crop"):
                v["face_pic"] = self.known.put_pic(sighting["face_crop"], "v")
            last = v["visits"][-1] if v["visits"] else None
            if last and last.get("key") == visit_key:
                if sighting.get("clip") and not last.get("clip"):
                    last["clip"] = sighting["clip"]
            else:
                pic = self.known.put_pic(sighting.get("face_crop") or sighting.get("body_crop"), "v")
                v["visits"].append({"key": visit_key, "time": when, "camera": sighting.get("camera"),
                                    "camera_name": sighting.get("camera_name"), "clip": sighting.get("clip"),
                                    "pic": pic})
                while len(v["visits"]) > MAX_VISITS:
                    self.known._drop_pic(v["visits"].pop(0).get("pic"))
            v["last"] = when
            if not v.get("pic"):
                v["pic"] = v.get("face_pic") or (v["visits"][-1].get("pic") if v["visits"] else None)
            self.visitors.sort(key=lambda x: x.get("last") or "", reverse=True)
            while len(self.visitors) > MAX_VISITORS:
                self._drop(self.visitors.pop())
            self.save()
            return v, new

    def _drop(self, v, keep=()):
        pics = {v.get("pic"), v.get("face_pic")} | {x.get("pic") for x in v.get("visits", [])}
        for p in pics - set(keep) - {None}:
            self.known._drop_pic(p)

    def forget_old(self, days, now=None):
        """Forgets visitors not seen for `days` days."""
        now = now or _now()
        cut = now - datetime.timedelta(days=days)
        with self._lock:
            old = [v for v in self.visitors if (_parse(v.get("last")) or cut) <= cut]
            for v in old:
                self.visitors.remove(v)
                self._drop(v)
            if old:
                self.save()
            return len(old)

    # ---- what you do in the viewer ----

    def get(self, vid):
        return next((v for v in self.visitors if v["id"] == vid), None)

    def update(self, vid, label=None, regular=None):
        with self._lock:
            v = self.get(vid)
            if not v:
                raise KeyError("That visitor has gone. Refresh and try again.")
            if label is not None:
                v["label"] = " ".join(str(label).split())[:40]
            if regular is not None:
                v["regular"] = bool(regular)
            self.save()
            return v

    def forget(self, vid):
        with self._lock:
            v = self.get(vid)
            if not v:
                return False
            self.visitors.remove(v)
            self._drop(v)
            self.save()
            return True

    def make_family(self, vid, person_id=None, name=None):
        """Says who a visitor is: their face and body pictures go to that person (or a new one),
        and the visitor is forgotten."""
        with self._lock:
            v = self.get(vid)
            if not v:
                raise KeyError("That visitor has gone. Refresh and try again.")
            store = self.known
            p = store._find("people", person_id) if person_id else store.add_person(name)
            if not p:
                raise KeyError("Not found")
            kept = []
            for i, f in enumerate(v.get("faces", [])):
                vec = self._vec(f["f"])
                if vec is not None:
                    pic = v.get("face_pic") if i == 0 else None
                    store.add_face(p["id"], vec, pic=pic)
                    if pic:
                        kept.append(pic)
            for b in v.get("bodies", []):
                vec = self._vec(b["f"])
                if vec is not None:
                    store.add_body(p["id"], dict(b, feature=vec))
            self.visitors.remove(v)
            self._drop(v, keep=kept)
            self.save()
            return p

    def public(self, days):
        now = _now()
        out = []
        with self._lock:
            for v in self.visitors:
                out.append({
                    "id": v["id"], "number": v.get("number"), "label": v.get("label") or "",
                    "regular": bool(v.get("regular")), "first": v.get("first"), "last": v.get("last"),
                    "pic": v.get("pic"), "face": bool(v.get("faces")), "height": v.get("height"),
                    "recent": self.visits_within(v, days, now), "total": len(v.get("visits", [])),
                    "visits": [{k: x.get(k) for k in ("time", "camera", "camera_name", "clip", "pic")}
                               for x in reversed(v.get("visits", []))],
                })
        return out
