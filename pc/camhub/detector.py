"""Person detection with OpenCV's DNN module and the MobileNet-SSD model.

The model files are downloaded by the install script into pc/models/.
Runs on the CPU and only when motion has been seen, so the load stays small.
"""
import logging
import os
import threading

import cv2
import numpy as np

log = logging.getLogger("camhub.detector")

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
PROTO = os.path.join(MODEL_DIR, "deploy.prototxt")
WEIGHTS = os.path.join(MODEL_DIR, "mobilenet_iter_73000.caffemodel")
PERSON_CLASS = 15
# Other things the same model knows that matter here: bus, car and motorbike.
VEHICLE_CLASSES = (6, 7, 14)


class PersonDetector:
    def __init__(self):
        self._net = None
        self._lock = threading.Lock()
        if os.path.exists(PROTO) and os.path.exists(WEIGHTS):
            try:
                self._net = cv2.dnn.readNetFromCaffe(PROTO, WEIGHTS)
                log.info("Person detection model loaded")
            except cv2.error as e:
                log.error("Could not load the person detection model: %s", e)
        else:
            log.warning("Person detection model not found in %s, so every motion will record", MODEL_DIR)

    @property
    def available(self):
        return self._net is not None

    def detect(self, frame, min_confidence=0.5):
        """Returns a list of (confidence, (x1, y1, x2, y2)) in 0-1 coordinates."""
        return [(c, box) for kind, c, box in self.detect_objects(frame, min_confidence, min_confidence)
                if kind == "person"]

    def detect_objects(self, frame, person_confidence=0.5, vehicle_confidence=0.5):
        """People and vehicles in one go: a list of (kind, confidence, (x1, y1, x2, y2)),
        kind "person" or "vehicle", coordinates from 0 to 1."""
        if self._net is None:
            return []
        blob = cv2.dnn.blobFromImage(cv2.resize(frame, (300, 300)), 0.007843, (300, 300), 127.5)
        with self._lock:
            self._net.setInput(blob)
            out = self._net.forward()
        found = []
        for i in range(out.shape[2]):
            cls = int(out[0, 0, i, 1])
            conf = float(out[0, 0, i, 2])
            if cls == PERSON_CLASS:
                kind, need = "person", person_confidence
            elif cls in VEHICLE_CLASSES:
                kind, need = "vehicle", vehicle_confidence
            else:
                continue
            if conf < need:
                continue
            x1, y1, x2, y2 = (float(v) for v in np.clip(out[0, 0, i, 3:7], 0, 1))
            if x2 - x1 <= 0 or y2 - y1 <= 0:
                continue
            found.append((kind, conf, (x1, y1, x2, y2)))
        return found
