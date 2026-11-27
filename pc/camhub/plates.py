"""Reading number plates, on this PC.

Two small models from the OpenCV model zoo, kept in pc/models with the others:
- PP-OCRv3 (text_detection_en_ppocrv3_2023may.onnx, 2.4 MB) finds writing in the picture of a
  vehicle that has stopped.
- CRNN (text_recognition_CRNN_EN_2022oct_int8.onnx, 16 MB) reads each piece of writing it found.

What was read is then checked against the shapes UK plates come in (AB12 CDE, A123 BCD,
ABC 123D, and older or private ones), so badges, stickers and van signwriting don't count.
Characters the reader often mixes up on plates (0 and O, 1 and I, 5 and S, 8 and B, 2 and Z)
are put right using where letters and numbers go on a plate.

Nothing leaves the PC.
"""
import logging
import os
import re

import cv2
import numpy as np

from .detector import MODEL_DIR

log = logging.getLogger("camhub.plates")

TEXT_DETECT = os.path.join(MODEL_DIR, "text_detection_en_ppocrv3_2023may.onnx")
TEXT_READ = os.path.join(MODEL_DIR, "text_recognition_CRNN_EN_2022oct_int8.onnx")

CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz"
READ_SIZE = (100, 32)

# Letters (L) and numbers (N) of the UK plate shapes, newest first.
UK_SHAPES = [
    "LLNNLLL",   # AB12 CDE (2001 on)
    "LNNNLLL",   # A123 BCD (1983 to 2001)
    "LNNLLL", "LNLLL",
    "LLLNNNL",   # ABC 123D (1963 to 1983)
    "LLLNNL", "LLLNL",
    "LLLNNN", "LLLNN", "LLLN",   # dateless
    "NNNLLL", "NNLLL", "NLLL",
    "NNNNLL", "NNNNL", "NNNLL", "NNLL",
    "LLNNNN", "LNNNN", "LLNNN",  # Northern Ireland and older dateless
]
TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B"}
TO_NUMBER = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "T": "7"}
# How much each fix makes a reading less sure: O and 0 look the same on a plate, B and 8
# nearly, 1 and I less so.
FIX_COST = {"0": 0.08, "O": 0.08, "8": 0.1, "B": 0.1, "5": 0.15, "S": 0.15}
OTHER_FIX = 0.25


def clean(text):
    """Just the letters and numbers, in capitals: how plates are stored and searched."""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def pretty(plate):
    """AB12CDE -> AB12 CDE, the way plates are written."""
    p = clean(plate)
    if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z]{3}", p):
        return f"{p[:4]} {p[4:]}"
    if re.fullmatch(r"[A-Z]\d{1,3}[A-Z]{3}", p):
        return f"{p[:-3]} {p[-3:]}"
    if re.fullmatch(r"[A-Z]{3}\d{1,3}[A-Z]", p):
        return f"{p[:3]} {p[3:]}"
    m = re.fullmatch(r"([A-Z]+)(\d+)", p) or re.fullmatch(r"(\d+)([A-Z]+)", p)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return p


def _fit(text, shape):
    """The text made to fit a plate shape, or None if it can't."""
    if len(text) != len(shape):
        return None
    out = []
    fixes = 0
    cost = 0.0
    for ch, want in zip(text, shape):
        if want == "L":
            if ch.isalpha():
                out.append(ch)
                continue
            if ch not in TO_LETTER:
                return None
            out.append(TO_LETTER[ch])
        else:
            if ch.isdigit():
                out.append(ch)
                continue
            if ch not in TO_NUMBER:
                return None
            out.append(TO_NUMBER[ch])
        fixes += 1
        cost += FIX_COST.get(ch, OTHER_FIX)
    # A plate needs a letter as read. It needs a number as read too, except the newest shape
    # (AB12 CDE) with its two numbers misread as letters (LO05 read as LOOS).
    if not any(c.isalpha() for c in text):
        return None
    if not any(c.isdigit() for c in text) and not (shape == UK_SHAPES[0] and fixes <= 2):
        return None
    return "".join(out), fixes, cost


def as_plate(text):
    """(plate, how sure 0 to 1) if the text could be a UK plate, else (None, 0)."""
    t = clean(text)
    best = None
    for i, shape in enumerate(UK_SHAPES):
        r = _fit(t, shape)
        if r is None:
            continue
        plate, fixes, cost = r
        # The newest shape is by far the most common now (older plates are over 25 years old),
        # so a reading that fits it with a likely fix beats an old shape as read.
        score = max(0.1, (1.0 if i == 0 else 0.8 - 0.02 * i) - cost)
        if best is None or score > best[1]:
            best = (plate, score)
    return best if best else (None, 0.0)


def similar(a, b):
    """How alike two plates are, 0 to 1, allowing for a misread character or two."""
    a, b = clean(a), clean(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Edit distance.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return max(0.0, 1.0 - prev[-1] / max(len(a), len(b)))


class PlateReader:
    def __init__(self, detect_path=TEXT_DETECT, read_path=TEXT_READ):
        self.det = self.rec = None
        if not (os.path.isfile(detect_path) and os.path.isfile(read_path)):
            log.info("Number plate models not found, so plates won't be read")
            return
        try:
            self.det = cv2.dnn_TextDetectionModel_DB(detect_path)
            self.det.setBinaryThreshold(0.3)
            self.det.setPolygonThreshold(0.5)
            self.det.setMaxCandidates(40)
            self.det.setUnclipRatio(2.0)
            self.rec = cv2.dnn.readNet(read_path)
        except cv2.error:
            log.exception("Couldn't load the number plate models")
            self.det = self.rec = None

    @property
    def available(self):
        return self.det is not None and self.rec is not None

    def _texts(self, img):
        """[(quad, text, confidence)] for each bit of writing found."""
        h, w = img.shape[:2]
        scale = min(1.0, 960 / max(h, w))
        if max(h, w) * scale < 320:
            scale = 320 / max(h, w)   # small vehicle crops are made bigger so the writing shows
        W = max(32, int(round(w * scale / 32)) * 32)
        H = max(32, int(round(h * scale / 32)) * 32)
        self.det.setInputParams(1.0 / 255.0, (W, H), (122.67891434, 116.66876762, 104.00698793))
        try:
            quads, _ = self.det.detect(img)
        except cv2.error:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        target = np.float32([[0, READ_SIZE[1] - 1], [0, 0], [READ_SIZE[0] - 1, 0],
                             [READ_SIZE[0] - 1, READ_SIZE[1] - 1]])
        out = []
        for q in quads:
            q = np.float32(q).reshape(4, 2)
            m = cv2.getPerspectiveTransform(q, target)
            crop = cv2.warpPerspective(gray, m, READ_SIZE)
            text, conf = self._read(crop)
            if text:
                out.append((q, text, conf))
        return out

    def _read(self, crop):
        blob = cv2.dnn.blobFromImage(crop, 1 / 127.5, READ_SIZE, 127.5)
        self.rec.setInput(blob)
        out = self.rec.forward()            # [steps, 1, 37]
        probs = out[:, 0, :]
        e = np.exp(probs - probs.max(axis=1, keepdims=True))
        probs = e / e.sum(axis=1, keepdims=True)
        idx = probs.argmax(axis=1)
        text, confs, last = [], [], 0
        for i, c in enumerate(idx):
            if c != 0 and c != last:
                text.append(CHARSET[c - 1])
                confs.append(float(probs[i, c]))
            last = c
        if not text:
            return "", 0.0
        return "".join(text).upper(), float(np.mean(confs))

    def read(self, img):
        """The number plate in a picture of a vehicle: {"plate", "sure", "box"} or None.
        box is (x1, y1, x2, y2) in pixels of img."""
        if not self.available or img is None or img.size == 0:
            return None
        texts = self._texts(img)
        if not texts:
            return None
        cands = []
        for q, t, c in texts:
            cands.append((t, c, [q]))
        # Plates are often found as two pieces ("AB12" and "CDE"): try neighbours together.
        for i, (qa, ta, ca) in enumerate(texts):
            for qb, tb, cb in texts[i + 1:]:
                (qa2, ta2), (qb2, tb2) = sorted([(qa, ta), (qb, tb)], key=lambda x: x[0][:, 0].min())
                ha = qa2[:, 1].max() - qa2[:, 1].min()
                hb = qb2[:, 1].max() - qb2[:, 1].min()
                if ha <= 0 or hb <= 0 or max(ha, hb) / min(ha, hb) > 1.5:
                    continue
                if abs(qa2[:, 1].mean() - qb2[:, 1].mean()) > 0.5 * max(ha, hb):
                    continue
                gap = qb2[:, 0].min() - qa2[:, 0].max()
                if -0.5 * ha <= gap <= 2.0 * max(ha, hb):
                    cands.append((ta2 + tb2, min(ca, cb), [qa2, qb2]))
        best = None
        for text, conf, quads in cands:
            plate, shape_score = as_plate(text)
            if not plate:
                continue
            score = conf * shape_score
            if conf < 0.4 or score < 0.3:
                continue
            if best is None or score > best[1]:
                pts = np.concatenate(quads)
                box = (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
                best = (plate, score, box)
        if best is None:
            return None
        return {"plate": best[0], "sure": round(best[1], 3), "box": best[2]}
