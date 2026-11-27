"""Learning as you go: when the PC isn't sure who someone is, it asks.

A face that is close to someone you've named, but not close enough to be sure, becomes a
question: "Is this Sarah?", sent to your phone with the picture. Your answer teaches it:

- Yes: the face is added to Sarah's faces, so next time she's known straight away.
- No: the face is kept as "not Sarah", so a face like it is never taken for her again, and it
  goes to "Faces seen lately" so you can say who it was.
- Someone else (pick a name or type a new one): the face is added to them.

Faces it is very sure about are also added by themselves now and then (when they show the
person in a new light, such as at night), so it keeps up as people change. Up to AUTO_KEEP
of these per person, and the photos you added yourself are never replaced by them.

Questions are kept in smart/questions.json for QUESTION_DAYS days. Nothing leaves the PC.
"""
import json
import logging
import os
import threading
import time

import numpy as np

from . import faces as facemod

log = logging.getLogger("camhub.learning")

DEFAULT_LEARN = {
    "ask": True,        # ask "Is this ...?" when a face is close but not certain
    "auto": True,       # add faces it is very sure of by itself
}
UNSURE_MARGIN = 0.08    # a face this far below the matching level counts as "close"
AUTO_MARGIN = 0.14      # a face this far above it is sure enough to learn from by itself
AUTO_NEW = 0.80         # ... if it's less alike than this to every face already kept
AUTO_KEEP = 10          # faces learnt by itself, per person
QUESTION_DAYS = 7
MAX_QUESTIONS = 30


def clean_learn(body, current):
    out = dict(current or DEFAULT_LEARN)
    body = body if isinstance(body, dict) else {}
    for k in ("ask", "auto"):
        if k in body:
            out[k] = bool(body[k])
    return out


class QuestionStore:
    """The open "Is this ...?" questions."""

    def __init__(self, folder, store):
        self.folder = folder
        self.store = store          # faces.KnownStore
        self.path = os.path.join(folder, "questions.json")
        self._lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = json.load(f)
            if not isinstance(self.items, list):
                self.items = []
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f)
        os.replace(tmp, self.path)

    def _tidy(self):
        cutoff = time.time() - QUESTION_DAYS * 86400
        keep = [q for q in self.items if q.get("ts", 0) >= cutoff]
        for q in self.items:
            if q not in keep:
                self.store.drop_pic(q.get("pic"))
        self.items = keep[-MAX_QUESTIONS:]
        for q in keep[:-MAX_QUESTIONS]:
            self.store.drop_pic(q.get("pic"))

    def ask(self, person, likeness, feature, crop_jpeg, camera, camera_name, clip=None):
        """Makes a question about a face that looks like `person`. Returns it, or None when
        an open question already asks the same."""
        feature = facemod.unit(feature)
        with self._lock:
            self._tidy()
            for q in self.items:
                if q["person_id"] == person["id"] and float(np.dot(np.float32(q["feature"]), feature)) > 0.85:
                    return None    # already asked about this face
            pic = self.store.put_pic(crop_jpeg, "q")
            if not pic:
                return None
            q = {"id": facemod.new_id("q_"), "ts": time.time(),
                 "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "camera": camera, "camera_name": camera_name,
                 "person_id": person["id"], "person_name": person["name"], "likeness": round(float(likeness), 3),
                 "feature": [round(float(v), 5) for v in feature], "pic": pic, "clip": clip}
            self.items.append(q)
            self._save()
            return q

    def public(self):
        with self._lock:
            names = {p["id"]: p["name"] for p in self.store.known["people"]}
            return [{k: v for k, v in q.items() if k != "feature"} | {"person_name": names.get(q["person_id"], q["person_name"])}
                    for q in reversed(self.items) if q["person_id"] in names]

    def answer(self, qid, answer, target_id=None, name=None):
        """answer: "yes", "no" or "other" (with target_id or a new name). Returns a short
        sentence saying what was learnt."""
        with self._lock:
            q = next((x for x in self.items if x["id"] == qid), None)
            if q is None:
                raise KeyError("That question has already been answered")
            person = self.store.find_person(q["person_id"])
            if answer == "yes":
                if person is None:
                    raise KeyError("That person has been removed")
                self.store.add_face(person["id"], q["feature"], pic=q["pic"])
                said = f"Thanks. {person['name']} will be known from this face from now on."
            elif answer == "no":
                if person is not None:
                    self.store.add_not_face(person["id"], q["feature"])
                data = self._pic_bytes(q["pic"])
                self.store.add_seen("face", q["camera"], q["camera_name"], data, feature=q["feature"], when=q["time"])
                self.store.drop_pic(q["pic"])
                who = person["name"] if person else "them"
                said = f"Thanks. A face like this won't be taken for {who} again. It's in Faces seen lately if you'd like to name them."
            elif answer == "other":
                if target_id:
                    target = self.store.find_person(target_id)
                    if target is None:
                        raise KeyError("Not found")
                else:
                    target = self.store.add_person(name)
                if person is not None and target["id"] != person["id"]:
                    self.store.add_not_face(person["id"], q["feature"])
                self.store.add_face(target["id"], q["feature"], pic=q["pic"])
                said = f"Thanks. That was {target['name']}, and they'll be known from this face from now on."
            else:
                raise ValueError("Answer yes, no or other")
            self.items.remove(q)
            self._save()
            log.info("Question about %s answered: %s", q["person_name"], answer)
            return said

    def forget(self, qid):
        with self._lock:
            q = next((x for x in self.items if x["id"] == qid), None)
            if q is None:
                return False
            self.items.remove(q)
            self.store.drop_pic(q["pic"])
            self._save()
            return True

    def _pic_bytes(self, name):
        p = self.store.pic_path(name)
        if not p:
            return None
        with open(p, "rb") as f:
            return f.read()

    def person_removed(self, person_id):
        with self._lock:
            for q in [x for x in self.items if x["person_id"] == person_id]:
                self.items.remove(q)
                self.store.drop_pic(q["pic"])
            self._save()


def auto_learn(store, person, feature, likeness, threshold, face):
    """Adds a face it is very sure of to `person`, when it shows them differently from the
    faces already kept. Returns True if it was added."""
    if likeness < threshold + AUTO_MARGIN or face["score"] < 0.9 or face["size"] < facemod.GOOD_FACE_PX:
        return False
    if face.get("enhanced"):
        return False    # only learn from faces seen clearly as they are
    feats = [f["feature"] for f in person.get("faces", [])]
    if feats and float(np.max(np.float32(feats) @ facemod.unit(feature))) >= AUTO_NEW:
        return False
    return store.add_face(person["id"], feature, face.get("crop"), auto=True) is not None
