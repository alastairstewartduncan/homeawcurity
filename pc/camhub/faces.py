"""Recognising family faces and your own cars, all on this PC.

Faces: OpenCV's YuNet finds faces and SFace turns each one into 128 numbers. Two pictures of
the same person give similar numbers, so a new face is compared with the faces you have named.
Both models are small ONNX files kept in pc/models (downloaded by the installer).

Cars: the colours of the car (hue and saturation by day, brightness at night, when the camera
sees in black and white) are compared with the cars you have marked as yours. This is a good
guide but not a number plate check, so similar cars of the same colour can be mistaken.

Nothing here leaves the PC.
"""
import base64
import json
import logging
import os
import re
import secrets
import threading
import time

import cv2
import numpy as np

from .detector import MODEL_DIR

log = logging.getLogger("camhub.faces")

FACE_DETECT = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
FACE_RECOGNISE = os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx")

# How alike two faces must be (cosine similarity of SFace features) to count as the same person.
# OpenCV suggests 0.363; "strict" asks for more.
FACE_MATCH = {"relaxed": 0.34, "normal": 0.40, "strict": 0.48}
# How alike two cars' colours must be (0 to 1).
CAR_MATCH = {"relaxed": 0.55, "normal": 0.65, "strict": 0.75}
MIN_FACE_PX = 36        # smaller faces are too blurry to recognise
MIN_ENHANCED_PX = 26    # ... unless the picture is enhanced first (see enhance)
GOOD_FACE_PX = 56       # a face this big gives a confident answer


def new_id(prefix):
    return f"{prefix}{secrets.token_hex(5)}"


def jpeg(img, quality=85):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def decode_image(data):
    """Bytes, or a data: URL / base64 text from the browser, to a BGR picture (or None)."""
    if isinstance(data, str):
        data = re.sub(r"^data:[^,]*,", "", data.strip())
        try:
            data = base64.b64decode(data, validate=False)
        except ValueError:
            return None
    if not data:
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return img if img is not None and img.size else None


def unit(v):
    v = np.asarray(v, np.float32).ravel()
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class FaceEngine:
    """Finds faces in a picture and works out their 128-number fingerprint."""

    def __init__(self, detect_path=FACE_DETECT, recognise_path=FACE_RECOGNISE):
        self._det = None
        self._rec = None
        self._lock = threading.Lock()
        if not (os.path.exists(detect_path) and os.path.exists(recognise_path)):
            log.warning("Face recognition models not found in %s, so faces can't be recognised", MODEL_DIR)
            return
        try:
            self._det = cv2.FaceDetectorYN.create(detect_path, "", (320, 320), 0.75, 0.3, 50)
            self._rec = cv2.FaceRecognizerSF.create(recognise_path, "")
            log.info("Face recognition models loaded")
        except (cv2.error, AttributeError) as e:
            self._det = self._rec = None
            log.error("Could not load the face recognition models: %s", e)

    @property
    def available(self):
        return self._rec is not None

    def faces(self, img, max_side=960):
        """All faces in a picture: [{"box": (x, y, w, h) in pixels of img, "score", "size",
        "feature": 128 numbers (unit length), "crop": small JPEG of the face}]."""
        if not self.available or img is None or img.size == 0:
            return []
        h, w = img.shape[:2]
        scale = min(1.0, max_side / max(h, w))
        small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale)))) if scale < 1 else img
        with self._lock:
            self._det.setInputSize((small.shape[1], small.shape[0]))
            _, rows = self._det.detect(small)
            out = []
            for row in rows if rows is not None else []:
                row = row.copy()
                row[:14] /= scale
                x, y, fw, fh = (float(v) for v in row[:4])
                size = min(fw, fh)
                if size < MIN_FACE_PX * 0.6:
                    continue
                aligned = self._rec.alignCrop(img, row)
                feature = unit(self._rec.feature(aligned))
                out.append({"box": (x, y, fw, fh), "score": float(row[14]), "size": size,
                            "feature": feature, "crop": face_crop(img, (x, y, fw, fh))})
        return out

    def faces_sharpened(self, img):
        """Like faces(), on an enhanced copy of a small or dark picture of someone's head and
        shoulders (see enhance). Boxes and sizes are in pixels of img, as they really are;
        each face found carries "enhanced": True."""
        if not self.available or img is None or img.size == 0:
            return []
        better, scale = enhance(img)
        out = []
        for f in self.faces(better, max_side=max(better.shape[:2])):
            x, y, w, h = f["box"]
            f["box"] = (x / scale, y / scale, w / scale, h / scale)
            f["size"] = f["size"] / scale
            f["enhanced"] = True
            out.append(f)
        return out

    def largest_face(self, img):
        found = self.faces(img, max_side=1280)
        return max(found, key=lambda f: f["size"]) if found else None


def enhance(img, want=320):
    """Makes a small, dark or grainy picture of a head easier to recognise.

    - Enlarged (up to 4 times) with smooth resampling, so the face finder sees enough of it.
    - At night (black and white infrared) or in the dark: the brightness is spread out locally
      (CLAHE), and the grain the camera adds in the dark is smoothed away.
    - Sharpened a little (unsharp mask), to undo some of the blur from enlarging.

    Returns (picture, how much it was enlarged)."""
    h, w = img.shape[:2]
    scale = max(1.0, min(4.0, want / max(1, max(h, w))))
    out = cv2.resize(img, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_CUBIC) \
        if scale > 1.0 else img.copy()
    night = is_night(img)
    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    light = lab[:, :, 0]
    dark = float(light.mean()) < 90
    if night or dark:
        light = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(4, 4)).apply(light)
        if float(light.mean()) < 70:
            # Still dark: lift the shadows more than the highlights.
            lut = np.uint8([min(255, int(255 * (i / 255) ** 0.7)) for i in range(256)])
            light = cv2.LUT(light, lut)
        lab[:, :, 0] = light
        out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        out = cv2.fastNlMeansDenoisingColored(out, None, 6, 6, 5, 15)
    blur = cv2.GaussianBlur(out, (0, 0), 1.2)
    out = cv2.addWeighted(out, 1.5, blur, -0.5, 0)
    return out, scale


def face_crop(img, box, out=160):
    x, y, w, h = box
    cx, cy, side = x + w / 2, y + h / 2, max(w, h) * 1.5
    H, W = img.shape[:2]
    x1, y1 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x2, y2 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
    if x2 <= x1 or y2 <= y1:
        return None
    return jpeg(cv2.resize(img[y1:y2, x1:x2], (out, out)), 88)


def box_crop(img, box, pad=0.08, max_side=320):
    """Crop of a 0-1 box from a picture, as a JPEG."""
    H, W = img.shape[:2]
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) * pad, (y2 - y1) * pad
    a, b = int(max(0, (x1 - dx) * W)), int(max(0, (y1 - dy) * H))
    c, d = int(min(W, (x2 + dx) * W)), int(min(H, (y2 + dy) * H))
    if c <= a or d <= b:
        return None
    crop = img[b:d, a:c]
    s = min(1.0, max_side / max(crop.shape[:2]))
    if s < 1:
        crop = cv2.resize(crop, (int(crop.shape[1] * s), int(crop.shape[0] * s)))
    return jpeg(crop)


# ---- cars ----------------------------------------------------------------------------

def is_night(img):
    """The camera has switched to black and white (infrared) when there's almost no colour."""
    if img is None or img.size == 0 or img.ndim < 3:
        return True
    small = cv2.resize(img, (64, 36))
    sat = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1]
    return float(sat.mean()) < 12


HUE_BINS, GREY_BINS, V_BINS = 18, 8, 16


def vehicle_signature(img, box):
    """A small colour summary of the vehicle inside a 0-1 box (centre part, to skip the road).

    "c": how much of each colour (18 hues for coloured paint, plus 8 shades of grey for white,
    silver, grey and black cars). "v": how bright it is overall, used at night.
    """
    H, W = img.shape[:2]
    x1, y1, x2, y2 = box
    mx, my = (x2 - x1) * 0.2, (y2 - y1) * 0.2
    a, b = int((x1 + mx) * W), int((y1 + my) * H)
    c, d = int((x2 - mx) * W), int((y2 - my) * H)
    if c - a < 4 or d - b < 4:
        return None
    hsv = cv2.cvtColor(img[b:d, a:c], cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    coloured = (sat >= 60) & (val >= 40)
    hues = np.bincount(np.minimum(hue[coloured] * HUE_BINS // 180, HUE_BINS - 1), minlength=HUE_BINS)
    greys = np.bincount(np.minimum(val[~coloured] * GREY_BINS // 256, GREY_BINS - 1), minlength=GREY_BINS)
    colour = np.concatenate([hues, greys]).astype(np.float64)
    bright = np.bincount(np.minimum(val * V_BINS // 256, V_BINS - 1), minlength=V_BINS).astype(np.float64)
    colour /= max(1.0, colour.sum())
    bright /= max(1.0, bright.sum())
    return {"c": [round(float(x), 5) for x in colour], "v": [round(float(x), 5) for x in bright],
            "night": is_night(img)}


def _soften(h, wrap):
    """Spreads each bar of a histogram a little into its neighbours, so a slightly different
    shade (other light, other time of day) still lands close."""
    h = np.float32(h)
    for _ in range(2):
        if wrap:  # hue goes round in a circle
            h = 0.25 * np.roll(h, 1) + 0.5 * h + 0.25 * np.roll(h, -1)
        else:
            p = np.concatenate([h[:1], h, h[-1:]])
            h = 0.25 * p[:-2] + 0.5 * p[1:-1] + 0.25 * p[2:]
    return h.astype(np.float32)


def _alike(a, b):
    a, b = np.float32(a), np.float32(b)
    if a.sum() <= 0 or b.sum() <= 0:
        return 0.0
    a, b = a / a.sum(), b / b.sum()
    return float(np.sum(np.sqrt(a * b)))  # 1 for the same, 0 for nothing in common


def vehicle_likeness(a, b):
    """0 (nothing alike) to 1 (same colours). Day pictures compare colour; night ones brightness."""
    if not a or not b:
        return 0.0
    v_sim = _alike(_soften(a["v"], False), _soften(b["v"], False))
    if a.get("night") or b.get("night"):
        if a.get("night") != b.get("night"):
            return 0.0  # a colour picture can't be compared with a black-and-white one
        return v_sim
    ca, cb = np.float32(a["c"]), np.float32(b["c"])
    sa = np.concatenate([_soften(ca[:HUE_BINS], True), _soften(ca[HUE_BINS:], False)])
    sb = np.concatenate([_soften(cb[:HUE_BINS], True), _soften(cb[HUE_BINS:], False)])
    return 0.75 * _alike(sa, sb) + 0.25 * v_sim


# ---- the people and cars you know -----------------------------------------------------

class KnownStore:
    """People and cars you've named, and the faces and vehicles seen lately that you haven't.

    Kept in smart/known.json and smart/seen.json next to config.json, with small pictures in
    smart/pics/.
    """

    SEEN_KEEP = 60
    MAX_FACES = 30   # per person
    MAX_NOT_FACES = 30  # per person: faces you said weren't them
    MAX_SAMPLES = 20  # per car
    MAX_BODIES = 40  # per person: the newest pictures of their build and clothes

    def __init__(self, folder):
        self.folder = folder
        self.pics = os.path.join(folder, "pics")
        os.makedirs(self.pics, exist_ok=True)
        self._lock = threading.RLock()
        self.known = self._read("known.json", {"people": [], "cars": []})
        self.known.setdefault("people", [])
        self.known.setdefault("cars", [])
        self.seen = self._read("seen.json", [])
        if not isinstance(self.seen, list):
            self.seen = []
        for p in self.known["people"]:
            p.setdefault("bodies", [])
            p.setdefault("not_faces", [])
        self._body_cache = {}    # body picture id -> its numbers, unpacked

    def _read(self, name, default):
        try:
            with open(os.path.join(self.folder, name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write(self, name, data):
        p = os.path.join(self.folder, name)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(p + ".tmp", p)

    def save(self):
        with self._lock:
            self._write("known.json", self.known)
            self._write("seen.json", self.seen)

    # ---- pictures ----

    def put_pic(self, data, prefix="p"):
        if not data:
            return None
        name = f"{prefix}_{int(time.time())}_{secrets.token_hex(4)}.jpg"
        with open(os.path.join(self.pics, name), "wb") as f:
            f.write(data)
        return name

    def pic_path(self, name):
        name = os.path.basename(name or "")
        if not re.fullmatch(r"[\w.-]+\.jpg", name):
            return None
        p = os.path.join(self.pics, name)
        return p if os.path.isfile(p) else None

    def drop_pic(self, name):
        self._drop_pic(name)

    def _drop_pic(self, name):
        p = self.pic_path(name)
        if p:
            try:
                os.remove(p)
            except OSError:
                pass

    def prune_event_pics(self, keep=400):
        """Alert pictures (e_...) are kept for the newest `keep` alerts."""
        try:
            names = sorted(n for n in os.listdir(self.pics) if n.startswith("e_"))
        except OSError:
            return
        for n in names[:-keep] if len(names) > keep else []:
            self._drop_pic(n)

    # ---- people ----

    def public(self):
        with self._lock:
            return {
                "people": [{"id": p["id"], "name": p["name"],
                            "faces": [{"id": f["id"], "pic": f.get("pic"), "auto": bool(f.get("auto"))}
                                      for f in p["faces"]],
                            "bodies": [{"id": b["id"], "pic": b.get("pic"), "night": b.get("night"),
                                        "time": b.get("time")} for b in p.get("bodies", [])],
                            "height": self.person_height(p)}
                           for p in self.known["people"]],
                "cars": [{"id": c["id"], "name": c["name"], "plate": c.get("plate") or "",
                          "samples": [{"id": s["id"], "pic": s.get("pic"), "night": s["sig"].get("night")}
                                      for s in c["samples"]]}
                         for c in self.known["cars"]],
                "seen": [{k: v for k, v in s.items() if k not in ("feature", "sig")} for s in self.seen],
            }

    def find_person(self, person_id):
        with self._lock:
            return self._find("people", person_id)

    def _find(self, kind, item_id):
        for x in self.known[kind]:
            if x["id"] == item_id:
                return x
        return None

    def add_person(self, name):
        name = clean_name(name)
        with self._lock:
            p = {"id": new_id("per_"), "name": name, "faces": [], "bodies": [], "not_faces": []}
            self.known["people"].append(p)
            self.save()
            return p

    def add_car(self, name):
        name = clean_name(name)
        with self._lock:
            c = {"id": new_id("car_"), "name": name, "samples": []}
            self.known["cars"].append(c)
            self.save()
            return c

    def rename(self, kind, item_id, name):
        with self._lock:
            x = self._find(kind, item_id)
            if not x:
                return None
            x["name"] = clean_name(name)
            self.save()
            return x

    def set_plate(self, car_id, plate):
        """Your car's number plate, so the sightings log knows it by its plate too."""
        from .plates import clean
        with self._lock:
            c = self._find("cars", car_id)
            if not c:
                return None
            plate = clean(plate)[:10]
            if plate and len(plate) < 2:
                raise ValueError("That doesn't look like a number plate")
            c["plate"] = plate
            self.save()
            return c

    def remove(self, kind, item_id):
        with self._lock:
            x = self._find(kind, item_id)
            if not x:
                return False
            for s in x.get("faces", []) + x.get("samples", []) + x.get("bodies", []):
                self._drop_pic(s.get("pic"))
            self.known[kind].remove(x)
            self.save()
            return True

    def remove_sample(self, kind, item_id, sample_id, key=None):
        with self._lock:
            x = self._find(kind, item_id)
            if not x:
                return False
            key = key or ("faces" if kind == "people" else "samples")
            for s in x[key]:
                if s["id"] == sample_id:
                    self._drop_pic(s.get("pic"))
                    x[key].remove(s)
                    self.save()
                    return True
            return False

    def add_face(self, person_id, feature, crop_jpeg=None, pic=None, auto=False):
        """auto: learnt by itself (learning.py). Those are the first to make room, and only
        AUTO_KEEP of them are kept, so the photos you chose stay."""
        from .learning import AUTO_KEEP
        with self._lock:
            p = self._find("people", person_id)
            if not p:
                return None
            f = {"id": new_id("f_"), "feature": [round(float(v), 5) for v in unit(feature)],
                 "pic": pic or self.put_pic(crop_jpeg, "f")}
            if auto:
                f["auto"] = True
            p["faces"].append(f)
            autos = [x for x in p["faces"] if x.get("auto")]
            while len(autos) > AUTO_KEEP or (len(p["faces"]) > self.MAX_FACES and autos):
                old = autos.pop(0)
                p["faces"].remove(old)
                self._drop_pic(old.get("pic"))
            while len(p["faces"]) > self.MAX_FACES:
                self._drop_pic(p["faces"].pop(0).get("pic"))
            # A face you've now said is them is no longer "not them".
            feat = unit(feature)
            p["not_faces"] = [n for n in p.get("not_faces", []) if float(np.dot(np.float32(n), feat)) < 0.9]
            self.save()
            return f

    def add_not_face(self, person_id, feature):
        """You said this face isn't them: never take a face like it for them again."""
        with self._lock:
            p = self._find("people", person_id)
            if not p:
                return False
            p.setdefault("not_faces", []).append([round(float(v), 5) for v in unit(feature)])
            del p["not_faces"][:-self.MAX_NOT_FACES]
            self.save()
            return True

    def add_car_sample(self, car_id, sig, crop_jpeg=None, pic=None):
        with self._lock:
            c = self._find("cars", car_id)
            if not c or not sig:
                return None
            s = {"id": new_id("s_"), "sig": sig, "pic": pic or self.put_pic(crop_jpeg, "c")}
            c["samples"].append(s)
            while len(c["samples"]) > self.MAX_SAMPLES:
                self._drop_pic(c["samples"].pop(0).get("pic"))
            self.save()
            return s

    # ---- matching ----

    def closest_face(self, feature):
        """(person, likeness) for whoever's face is most like this one, even if not very.
        People you said this face isn't (a face very like one you said no to) are skipped."""
        feature = unit(feature)
        best, best_sim = None, -1.0
        with self._lock:
            for p in self.known["people"]:
                if not p["faces"]:
                    continue
                feats = np.float32([f["feature"] for f in p["faces"]])
                sim = float(np.max(feats @ feature))
                nots = p.get("not_faces") or []
                if nots and float(np.max(np.float32(nots) @ feature)) >= max(sim, 0.5):
                    continue
                if sim > best_sim:
                    best, best_sim = p, sim
        return best, best_sim

    def match_face(self, feature, threshold):
        """(person, likeness) for the best match, person None if nobody is close enough."""
        best, best_sim = self.closest_face(feature)
        return (best if best_sim >= threshold else None), best_sim

    def match_car(self, sig, threshold):
        best, best_sim = None, 0.0
        with self._lock:
            for c in self.known["cars"]:
                for s in c["samples"]:
                    sim = vehicle_likeness(sig, s["sig"])
                    if sim > best_sim:
                        best, best_sim = c, sim
        return (best if best_sim >= threshold else None), best_sim

    # ---- bodies: knowing family from behind ----

    def add_body(self, person_id, obs, pic=None, when=None):
        """Keeps a picture of someone's build and clothes (obs from bodies.BodyEngine.describe,
        with "height" and "pace" added). Skips it when it's nearly the same as one already kept."""
        from . import bodies
        feat = obs.get("feature") if obs else None
        if feat is None:
            return None
        feat = unit(feat)
        with self._lock:
            p = self._find("people", person_id)
            if not p:
                return None
            have = [self._body_feature(b) for b in p["bodies"]]
            have = [h for h in have if h is not None]
            if have and float(np.max(np.float32(have) @ feat)) > 0.97:
                return None
            b = {"id": new_id("b_"), "feature": bodies.pack(feat), "clothes": obs.get("clothes"),
                 "night": bool(obs.get("night")), "height": obs.get("height"), "pace": obs.get("pace"),
                 "time": when or time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "pic": pic or self.put_pic(obs.get("crop"), "b")}
            p["bodies"].append(b)
            while len(p["bodies"]) > self.MAX_BODIES:
                old = p["bodies"].pop(0)
                self._body_cache.pop(old["id"], None)
                self._drop_pic(old.get("pic"))
            self.save()
            return b

    def _body_feature(self, b):
        f = self._body_cache.get(b["id"])
        if f is None:
            from . import bodies
            f = bodies.unpack(b.get("feature"))
            if f is None:
                return None
            self._body_cache[b["id"]] = f
        return f

    def body_samples(self, p):
        out = []
        for b in p.get("bodies", []):
            f = self._body_feature(b)
            if f is not None:
                out.append({"feature": f, "clothes": b.get("clothes"), "night": b.get("night")})
        return out

    @staticmethod
    def _median(p, key):
        vals = [b[key] for b in p.get("bodies", []) if b.get(key)]
        return round(float(np.median(vals)), 3) if vals else None

    def person_height(self, p):
        return self._median(p, "height")

    def match_body(self, obs, threshold):
        """(person, likeness, next best likeness) for the family member whose build and clothes
        are most like obs. person is None if nobody is close enough."""
        from . import bodies
        scores = []
        with self._lock:
            for p in self.known["people"]:
                samples = self.body_samples(p)
                if samples:
                    scores.append((bodies.likeness(obs, samples, self._median(p, "height"),
                                                   self._median(p, "pace")), p))
        if not scores:
            return None, 0.0, 0.0
        scores.sort(key=lambda s: -s[0])
        best, p = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        return (p if best >= threshold else None), best, second

    @property
    def has_bodies(self):
        return any(p.get("bodies") for p in self.known["people"])

    @property
    def has_people(self):
        return any(p["faces"] for p in self.known["people"])

    @property
    def has_cars(self):
        return any(c["samples"] for c in self.known["cars"])

    # ---- seen lately ----

    def add_seen(self, kind, camera, camera_name, crop_jpeg, feature=None, sig=None, match=None, when=None):
        """Remembers a face or vehicle seen, so you can say who or whose it is."""
        pic = self.put_pic(crop_jpeg, "s")
        if not pic:
            return None
        item = {"id": new_id("seen_"), "kind": kind, "camera": camera, "camera_name": camera_name,
                "time": when or time.strftime("%Y-%m-%dT%H:%M:%S"), "pic": pic, "match": match}
        if feature is not None:
            item["feature"] = [round(float(v), 5) for v in unit(feature)]
        if sig is not None:
            item["sig"] = sig
        with self._lock:
            self.seen.insert(0, item)
            while len(self.seen) > self.SEEN_KEEP:
                self._drop_pic(self.seen.pop().get("pic"))
            self.save()
        return item

    def take_seen(self, seen_id):
        with self._lock:
            for s in self.seen:
                if s["id"] == seen_id:
                    self.seen.remove(s)
                    self.save()
                    return s
        return None

    def forget_seen(self, seen_id):
        s = self.take_seen(seen_id)
        if s:
            self._drop_pic(s.get("pic"))
        return bool(s)

    def assign_seen(self, seen_id, target_id=None, new_name=None):
        """Says who a seen face is (or whose a seen vehicle is). Returns the person or car."""
        with self._lock:
            s = next((x for x in self.seen if x["id"] == seen_id), None)
            if not s:
                raise KeyError("That picture has gone. Refresh and try again.")
            kind = "people" if s["kind"] == "face" else "cars"
            if target_id:
                target = self._find(kind, target_id)
                if not target:
                    raise KeyError("Not found")
            else:
                target = self.add_person(new_name) if kind == "people" else self.add_car(new_name)
            self.seen.remove(s)
            if kind == "people":
                self.add_face(target["id"], s["feature"], pic=s["pic"])
            else:
                self.add_car_sample(target["id"], s["sig"], pic=s["pic"])
            # Other faces from the same visit are often the same person: leave them for the user.
            self.save()
            return target


def clean_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    if not name:
        raise ValueError("Please give a name")
    return name
