"""Where the camera is pointing now, compared with where it was when it started to move.

When the PC turns or zooms the camera by itself (following someone, or zooming in on a face),
everything drawn on the picture (privacy areas, your property, ignore zones) is drawn for the
camera's usual view. To keep those in the right place, each live picture is compared with the
one before: matching corners in both (ORB features) give how far the picture slid and grew.
Added up, that maps a point in the usual view to the same spot in today's view, and back.

If the pictures can't be matched for a moment (a blank wall, deep night noise), the view is
"lost": the caller then covers the whole picture where privacy areas exist and sends the
camera back to where it started.
"""
import threading

import cv2
import numpy as np

WIDTH = 320            # pictures are compared at this width
MIN_MATCHES = 12       # fewer matching corners than this and the step can't be trusted
LOST_AFTER = 4         # steps in a row that can't be matched
MAX_STEP_SCALE = 0.25  # one step can't zoom more than this (25%): anything bigger is a bad match


def _identity():
    return np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], np.float64)


class ViewTracker:
    """Follows how the picture moves while the camera turns or zooms."""

    def __init__(self):
        self._lock = threading.Lock()
        self._orb = cv2.ORB_create(nfeatures=600, fastThreshold=12)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.active = False
        self.lost = False
        self._m = _identity()     # usual view (pixels of the small picture) -> view now
        self._prev = None
        self._size = None
        self._fails = 0
        self.version = 0          # goes up each time the view changes, for caching

    # ---- control ----

    def start(self, frame=None):
        """The camera is about to move: from here on, follow the picture."""
        with self._lock:
            if self.active:
                return
            self.active, self.lost, self._fails = True, False, 0
            self._m = _identity()
            self._prev = None
            self.version += 1
        if frame is not None:
            self.update(frame)

    def stop(self):
        """The camera is back where it started (or we stop caring): the usual view again."""
        with self._lock:
            self.active, self.lost, self._fails = False, False, 0
            self._m = _identity()
            self._prev = None
            self.version += 1

    # ---- each picture ----

    def _features(self, frame):
        h, w = frame.shape[:2]
        size = (WIDTH, max(1, int(round(h * WIDTH / w))))
        gray = cv2.cvtColor(cv2.resize(frame, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        # Even out the brightness, so night pictures still have corners to match.
        gray = cv2.equalizeHist(gray)
        kp, des = self._orb.detectAndCompute(gray, None)
        return size, kp, des

    def update(self, frame):
        """Compares this picture with the last one. Returns False when the view is lost."""
        if not self.active or frame is None or frame.size == 0:
            return not self.lost
        size, kp, des = self._features(frame)
        with self._lock:
            if not self.active:
                return True
            prev, self._prev = self._prev, (size, kp, des)
            if prev is None or prev[0] != size:
                self._size = size
                return not self.lost
            step = self._step(prev[1], prev[2], kp, des)
            if step is None:
                self._fails += 1
                if self._fails >= LOST_AFTER and not self.lost:
                    self.lost = True
                    self.version += 1
                # Keep the old picture to compare with, so a single bad one doesn't break the chain.
                self._prev = prev
                return not self.lost
            self._fails = 0
            if np.abs(step - _identity()).max() > 1e-3:
                self._m = _compose(step, self._m)
                self.version += 1
            return not self.lost

    def _step(self, kp1, des1, kp2, des2):
        if des1 is None or des2 is None or len(kp1) < MIN_MATCHES or len(kp2) < MIN_MATCHES:
            return None
        matches = self._matcher.match(des1, des2)
        if len(matches) < MIN_MATCHES:
            return None
        a = np.float32([kp1[m.queryIdx].pt for m in matches])
        b = np.float32([kp2[m.trainIdx].pt for m in matches])
        m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0,
                                             maxIters=500, confidence=0.98)
        if m is None or inl is None or int(inl.sum()) < MIN_MATCHES:
            return None
        scale = float(np.hypot(m[0, 0], m[1, 0]))
        if abs(scale - 1.0) > MAX_STEP_SCALE:
            return None
        # A dome camera pans and tilts: it shouldn't seem to roll. Take the roll out.
        m = np.array([[scale, 0.0, m[0, 2]], [0.0, scale, m[1, 2]]], np.float64)
        return m

    # ---- mapping points ----

    def matrix(self):
        """(2x3 matrix from the usual view to the view now, in 0-1 picture units) or None
        when the camera hasn't moved."""
        with self._lock:
            if not self.active or self._size is None:
                return None
            m = self._m.copy()
            w, h = self._size
        # Pixels of the small picture -> 0 to 1 units, both ways.
        s = np.array([[w, 0, 0], [0, h, 0], [0, 0, 1]], np.float64)
        s_inv = np.array([[1 / w, 0, 0], [0, 1 / h, 0], [0, 0, 1]], np.float64)
        full = s_inv @ np.vstack([m, [0, 0, 1]]) @ s
        return full[:2]

    def to_view(self, pts):
        """Points of the usual view -> the same spots in the view now."""
        return apply(self.matrix(), pts)

    def to_home(self, pts):
        """Points of the view now -> the same spots in the usual view."""
        m = self.matrix()
        if m is None:
            return [list(map(float, p)) for p in pts]
        inv = cv2.invertAffineTransform(m)
        return apply(inv, pts)

    def box_to_home(self, box):
        (x1, y1), (x2, y2) = self.to_home([(box[0], box[1]), (box[2], box[3])])
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    def centre_in_home(self):
        """Where the middle of today's picture is in the usual view (0.5, 0.5 when unmoved)."""
        return self.to_home([(0.5, 0.5)])[0]

    def zoom(self):
        """How much bigger things look than in the usual view (1 = not zoomed)."""
        m = self.matrix()
        return 1.0 if m is None else float(np.hypot(m[0, 0], m[1, 0]))


def _compose(a, b):
    """a after b, for 2x3 matrices."""
    return (np.vstack([a, [0, 0, 1]]) @ np.vstack([b, [0, 0, 1]]))[:2]


def apply(m, pts):
    if m is None:
        return [list(map(float, p)) for p in pts]
    p = np.float64([[float(x), float(y)] for x, y in pts])
    out = p @ m[:, :2].T + m[:, 2]
    return [[float(x), float(y)] for x, y in out]


def project_masks(masks, m):
    """Privacy areas of the usual view, moved to where they are in the view now."""
    if m is None:
        return [dict(z) for z in masks]
    return [dict(z, points=apply(m, z.get("points") or [])) for z in masks]


FULL_FRAME = [{"name": "Whole picture", "points": [[-0.01, -0.01], [1.01, -0.01], [1.01, 1.01], [-0.01, 1.01]]}]
