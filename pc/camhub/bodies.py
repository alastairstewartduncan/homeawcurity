"""Knowing family from behind or in the dark: build, height, clothes and walk.

Everything here runs on this PC with a bundled open model, and nothing leaves the PC.

- Build and clothes: OpenCV Zoo's YoutuReID model (Apache 2.0, a 29 MB file in pc/models)
  turns a picture of a whole person into 768 numbers. Two pictures of the same person, from the
  front, side or back, give similar numbers. It mostly sees body shape and clothes.
- Clothes colours: the colours of the top half and the legs, compared by day. At night the
  camera sees in black and white, so only brightness is compared then.
- Height: the camera learns how tall people usually look at each spot in the picture (people
  further away look smaller). Someone's height is then how much taller or shorter than usual
  they look at that spot. A child and an adult are told apart easily.
- Walk: how fast someone walks, in body lengths per second. A weak hint, used only a little.

Family are learnt automatically: when a face is recognised, pictures of that person's body
from the same visit are kept, so the next time they're known from behind too. Clothes change,
so the newest pictures are kept and the oldest dropped.
"""
import base64
import collections
import logging
import math
import os
import threading

import cv2
import numpy as np

from . import faces as facemod
from .detector import MODEL_DIR

log = logging.getLogger("camhub.bodies")

BODY_MODEL = os.path.join(MODEL_DIR, "person_reid_youtu_2021nov_int8bq.onnx")

# How alike two people's bodies must be (0 to 1) to say it's the same family member. Tried on
# OpenCV's street video (vtest.avi): at "normal" one look picks the wrong person about 3 times
# in 100; two looks must agree, and height must fit, so a mix-up is much rarer than that.
BODY_MATCH = {"relaxed": 0.58, "normal": 0.65, "strict": 0.72}
# One look this much closer than the setting is enough on its own.
BODY_SURE = 0.12
# A returning visitor needs a closer match, because their clothes are all there is to go on.
VISITOR_BODY = {"relaxed": 0.68, "normal": 0.72, "strict": 0.78}
MIN_BODY_PX = 72        # a person shorter than this (in the full picture) is too small to tell
HEIGHT_VETO = 0.18      # people more than 18% taller or shorter than usual for them aren't them
INPUT = (128, 256)      # the model's picture size, width x height
MEAN = np.float32([0.485, 0.456, 0.406])
STD = np.float32([0.229, 0.224, 0.225])


def pack(v):
    """A feature as short text: 16-bit numbers in base64 (a 768 number body is 2 KB)."""
    return base64.b64encode(facemod.unit(v).astype(np.float16).tobytes()).decode("ascii")


def unpack(s):
    try:
        return np.frombuffer(base64.b64decode(s), np.float16).astype(np.float32)
    except (ValueError, TypeError):
        return None


class BodyEngine:
    """Turns a picture of one person into 768 numbers that describe their build and clothes."""

    def __init__(self, path=BODY_MODEL):
        self._net = None
        self._lock = threading.Lock()
        if not os.path.exists(path):
            log.warning("Body recognition model not found in %s, so people can't be known from behind", MODEL_DIR)
            return
        try:
            self._net = cv2.dnn.readNet(path)
            log.info("Body recognition model loaded")
        except cv2.error as e:
            log.error("Could not load the body recognition model: %s", e)

    @property
    def available(self):
        return self._net is not None

    def feature(self, crop):
        if not self.available or crop is None or crop.size == 0:
            return None
        img = cv2.resize(crop, INPUT, interpolation=cv2.INTER_AREA)
        img = (img[:, :, ::-1].astype(np.float32) / 255.0 - MEAN) / STD
        blob = cv2.dnn.blobFromImage(img)
        with self._lock:
            self._net.setInput(blob)
            out = self._net.forward()
        return facemod.unit(out.reshape(-1))

    def describe(self, img, box):
        """What one person looks like, from a 0-1 box in a picture. None if they're too small."""
        crop = person_crop(img, box)
        if crop is None:
            return None
        feat = self.feature(crop)
        if feat is None:
            return None
        return {"feature": feat, "clothes": clothes(crop), "night": facemod.is_night(crop),
                "crop": small_jpeg(crop)}


def person_crop(img, box):
    if img is None or img.size == 0:
        return None
    H, W = img.shape[:2]
    x1, y1, x2, y2 = box
    a, b = int(max(0.0, x1) * W), int(max(0.0, y1) * H)
    c, d = int(min(1.0, x2) * W), int(min(1.0, y2) * H)
    if d - b < MIN_BODY_PX or c - a < MIN_BODY_PX // 5:
        return None
    return img[b:d, a:c]


def small_jpeg(crop, max_side=220):
    s = min(1.0, max_side / max(crop.shape[:2]))
    if s < 1:
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * s)), max(1, int(crop.shape[0] * s))))
    return facemod.jpeg(crop, 85)


# ---- clothes colours -------------------------------------------------------------------

def _colours(region):
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    coloured = (sat >= 60) & (val >= 40)
    hues = np.bincount(np.minimum(hue[coloured] * facemod.HUE_BINS // 180, facemod.HUE_BINS - 1),
                       minlength=facemod.HUE_BINS)
    greys = np.bincount(np.minimum(val[~coloured] * facemod.GREY_BINS // 256, facemod.GREY_BINS - 1),
                        minlength=facemod.GREY_BINS)
    c = np.concatenate([hues, greys]).astype(np.float64)
    v = np.bincount(np.minimum(val * facemod.V_BINS // 256, facemod.V_BINS - 1),
                    minlength=facemod.V_BINS).astype(np.float64)
    c /= max(1.0, c.sum())
    v /= max(1.0, v.sum())
    return [round(float(x), 4) for x in c], [round(float(x), 4) for x in v]


def clothes(crop):
    """Colours of the top (chest to waist) and the legs, from the middle of the person (to
    leave out the background either side)."""
    h, w = crop.shape[:2]
    a, b = int(w * 0.2), max(int(w * 0.8), int(w * 0.2) + 1)
    top = crop[int(h * 0.18):max(int(h * 0.5), int(h * 0.18) + 1), a:b]
    legs = crop[int(h * 0.5):max(int(h * 0.88), int(h * 0.5) + 1), a:b]
    out = {}
    for name, region in (("top", top), ("legs", legs)):
        if region.size == 0:
            return None
        out[name], out[name + "_v"] = _colours(region)
    return out


def clothes_likeness(a, b, night):
    """0 to 1, or None when they can't be compared."""
    if not a or not b:
        return None
    sims = []
    for part in ("top", "legs"):
        if night:
            sims.append(facemod._alike(facemod._soften(a[part + "_v"], False), facemod._soften(b[part + "_v"], False)))
            continue
        ca, cb = np.float32(a[part]), np.float32(b[part])
        hb = facemod.HUE_BINS
        sa = np.concatenate([facemod._soften(ca[:hb], True), facemod._soften(ca[hb:], False)])
        sb = np.concatenate([facemod._soften(cb[:hb], True), facemod._soften(cb[hb:], False)])
        sims.append(facemod._alike(sa, sb))
    return float(np.mean(sims))


# ---- comparing people ------------------------------------------------------------------

def likeness(obs, samples, height=None, pace=None):
    """How alike a person just seen is to someone's saved body pictures, 0 to 1.

    obs: {"feature", "clothes", "night", "height", "pace"}; samples: the same, with the
    feature as numbers. height and pace: the person's usual height and walking pace.
    """
    feat = obs.get("feature")
    if feat is None or not samples:
        return 0.0
    night = bool(obs.get("night"))
    same = [s for s in samples if bool(s.get("night")) == night] or samples
    feats = np.float32([s["feature"] for s in same])
    sims = feats @ facemod.unit(feat)
    i = int(np.argmax(sims))
    score = float(sims[i])
    # Clothes colours from the pictures that look most alike, when taken in the same light.
    if bool(same[i].get("night")) == night:
        col = clothes_likeness(obs.get("clothes"), same[i].get("clothes"), night)
        if col is not None:
            score = 0.78 * score + 0.22 * col
    h = obs.get("height")
    if h and height:
        d = abs(h - height) / height
        if d > HEIGHT_VETO:
            return 0.0
        score -= 0.15 * d / HEIGHT_VETO
    p = obs.get("pace")
    if p and pace and p > 0.2 and pace > 0.2:
        score -= 0.05 * min(1.0, abs(math.log(p / pace)) / 0.5)
    return max(0.0, min(1.0, score))


# ---- how tall people look at each spot -------------------------------------------------

class HeightModel:
    """Learns how tall people usually look where they stand in one camera's picture.

    Someone further away looks smaller, so a person's height is compared with others who stood
    near the same spot (by how far down the picture their feet are). Needs the camera kept
    still: when it turns, it starts learning again.
    """

    KEEP = 600
    NEAR = 0.06     # feet this close (share of the picture's height) count as the same spot
    NEED = 12       # people seen near a spot before heights there can be compared

    def __init__(self, data=None):
        self.obs = collections.deque(maxlen=self.KEEP)
        for o in (data or []):
            try:
                self.obs.append((float(o[0]), float(o[1])))
            except (TypeError, ValueError, IndexError):
                pass

    @staticmethod
    def usable(box):
        x1, y1, x2, y2 = box
        # Cut off at the top or bottom of the picture, or lying down: their height is unknown.
        return y1 > 0.01 and y2 < 0.99 and (y2 - y1) > 0.05 and (y2 - y1) > (x2 - x1) * 1.2

    def add(self, box):
        if self.usable(box):
            self.obs.append((round(box[3], 4), round(box[3] - box[1], 4)))

    def ratio(self, box):
        """How tall this person looks compared with people usually seen at that spot (1 is
        usual, 0.6 a child). None until enough people have been seen there."""
        if not self.usable(box) or len(self.obs) < self.NEED:
            return None
        arr = np.float32(self.obs)
        near = arr[np.abs(arr[:, 0] - box[3]) <= self.NEAR, 1]
        if len(near) < self.NEED:
            return None
        usual = float(np.median(near))
        return round((box[3] - box[1]) / usual, 3) if usual > 0 else None

    def clear(self):
        self.obs.clear()

    def data(self):
        return [list(o) for o in self.obs]
