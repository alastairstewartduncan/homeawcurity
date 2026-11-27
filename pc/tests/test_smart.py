"""Smart alerts: faces, cars, parcels, lines and loitering.  Run:  python -m pytest tests"""
import json
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import auth, faces as facemod, smart  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.presence import Presence  # noqa: E402
from camhub.storage import Storage  # noqa: E402


def make_config(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"server": {"password_hash": auth.hash_password("pw123456")},
                             "storage": {"path": str(tmp_path / "rec")},
                             "cameras": [{"id": "front", "name": "Front"}]}))
    return Config(str(p)), p


class FakeDetector:
    available = True

    def __init__(self):
        self.objects = []

    def detect_objects(self, frame, a=0.5, b=0.5):
        return list(self.objects)


class FakeCam:
    def __init__(self, cfg, detector):
        self.cfg = cfg
        self.id = cfg["id"]
        self.clip = None
        self.detector = detector
        self.seen = []

    def add_seen(self, label):
        self.seen.append(label)

    def hires_frame(self, sharpest=False):
        return None


class FakeEngine:
    available = True

    def __init__(self):
        self.next = []

    def faces(self, img, max_side=960):
        return self.next.pop(0) if self.next else []

    def largest_face(self, img):
        found = self.faces(img)
        return found[0] if found else None


class FakeHub:
    def __init__(self, config):
        self.config = config
        self.storage = Storage(config)
        self.events = EventBus()
        self.detector = FakeDetector()
        self.cameras = {c["id"]: FakeCam(c, self.detector) for c in config.cameras}


def make(tmp_path, presence=False):
    config, path = make_config(tmp_path)
    hub = FakeHub(config)
    if presence:
        hub.presence = Presence(config, hub.events)
    engine = FakeEngine()
    hub.smart = smart.SmartHub(hub, engine=engine, start_worker=False)
    if presence:
        hub.events.enrich = lambda e: hub.presence.tag_event(hub.smart.tag_event(e))
    else:
        hub.events.enrich = hub.smart.tag_event
    cam = hub.cameras["front"]
    w = hub.smart.watcher(cam)
    q = hub.events.subscribe()
    return hub, w, q, engine, path


def drain(q, kind=None):
    out = []
    while not q.empty():
        e = q.get_nowait()
        if kind is None or e["type"] == kind:
            out.append(e)
    return out


def person(x, y, h=0.3, w=0.1, ignored=False):
    """A person standing with their feet at (x, y)."""
    return {"kind": "person", "conf": 0.9, "box": (x - w / 2, y - h, x + w / 2, y), "ignored": ignored}


def vehicle(box, ignored=False):
    return {"kind": "vehicle", "conf": 0.9, "box": box, "ignored": ignored}


FRAME = np.full((360, 640, 3), 90, np.uint8)


# ---- geometry and settings -------------------------------------------------------------

def test_crossing_and_direction():
    a, b = (0.5, 0.0), (0.5, 1.0)       # a vertical line; the arrow points left (x smaller)
    assert smart.crossing(a, b, (0.6, 0.5), (0.4, 0.5)) == 1
    assert smart.crossing(a, b, (0.4, 0.5), (0.6, 0.5)) == -1
    assert smart.crossing(a, b, (0.6, 0.5), (0.55, 0.5)) == 0
    # Passing beyond the end of the line doesn't count.
    assert smart.crossing((0.5, 0.0), (0.5, 0.4), (0.6, 0.6), (0.4, 0.6)) == 0


def test_clean_camera_rules():
    out = smart.clean_camera({
        "parcel": {"enabled": True, "area": [[0, 0], [0.2, 0]]},  # too few points: off
        "lines": [{"name": " Drive ", "a": [0.1, 0.5], "b": [0.9, 0.5], "direction": "weird", "what": "cats"},
                  {"a": [0.1, 0.1], "b": [0.1, 0.1]}],               # too short: dropped
        "areas": [{"name": "", "points": [[0, 0], [1, 0], [1, 2]], "seconds": 1}],
    })
    assert out["parcel"] == {"enabled": False, "area": []}
    assert len(out["lines"]) == 1
    ln = out["lines"][0]
    assert ln["name"] == "Drive" and ln["direction"] == "both" and ln["what"] == "person" and ln["enabled"]
    ar = out["areas"][0]
    assert ar["name"] == "Area 1" and ar["seconds"] == 5 and ar["points"][2] == [1.0, 1.0]


# ---- lines and loitering ---------------------------------------------------------------

def test_line_crossing_alerts_once_in_the_arrow_direction(tmp_path):
    hub, w, q, _, path = make(tmp_path)
    hub.smart.update_camera("front", {"lines": [
        {"id": "drive", "name": "Into the drive", "a": [0.5, 0.0], "b": [0.5, 1.0], "direction": "arrow"}]})
    assert json.loads(path.read_text())["smart"]["cameras"]["front"]["lines"][0]["id"] == "drive"
    assert w.detect_interval() == 0.5
    # Walking right (against the arrow): nothing.
    for i, x in enumerate([0.3, 0.45, 0.6, 0.7]):
        w.on_frame(FRAME, 100 + i, True, [person(x, 0.8)], False)
    assert not drain(q, "line_crossed")
    # Walking back left (with the arrow): one alert, even if they wander back and forth.
    for i, x in enumerate([0.6, 0.45, 0.55, 0.4, 0.3]):
        w.on_frame(FRAME, 110 + i, True, [person(x, 0.8)], False)
    got = drain(q, "line_crossed")
    assert len(got) == 1
    e = got[0]
    assert e["smart"] and e["rule"] == "Into the drive" and e["person"] and e["picture"].startswith("/api/smart/pics/")
    assert "Crossed Into the drive" in hub.cameras["front"].seen
    # Vehicles don't count for a people line.
    for i, x in enumerate([0.7, 0.3]):
        w.on_frame(FRAME, 130 + i, True, [vehicle((x - 0.1, 0.6, x + 0.1, 0.8))], False)
    assert not drain(q, "line_crossed")


def test_loitering_needs_the_time_and_tolerates_gaps(tmp_path):
    hub, w, q, _, _ = make(tmp_path)
    hub.smart.update_camera("front", {"areas": [
        {"name": "By the car", "points": [[0.2, 0.5], [0.6, 0.5], [0.6, 0.9], [0.2, 0.9]], "seconds": 20}]})
    t = 1000.0
    w.on_frame(FRAME, t, True, [person(0.4, 0.8)], False)
    assert w.wants_detection(t + 30)      # keeps looking while someone's there, even standing still
    w.on_frame(FRAME, t + 8, False, [], False)            # missed for a moment
    w.on_frame(FRAME, t + 12, False, [person(0.41, 0.8)], False)
    assert not drain(q, "loitering")
    w.on_frame(FRAME, t + 21, False, [person(0.42, 0.8)], False)
    got = drain(q, "loitering")
    assert len(got) == 1 and "21 seconds" in got[0]["message"]
    w.on_frame(FRAME, t + 40, False, [person(0.42, 0.8)], False)
    assert not drain(q, "loitering")       # once per stay
    # They leave for a while and come back: a new stay.
    w.on_frame(FRAME, t + 60, False, [], False)
    w.on_frame(FRAME, t + 61, False, [person(0.4, 0.8)], False)
    w.on_frame(FRAME, t + 70, False, [person(0.4, 0.8)], False)
    assert not drain(q, "loitering")
    w.on_frame(FRAME, t + 82, False, [person(0.4, 0.8)], False)
    assert len(drain(q, "loitering")) == 1
    # Someone just walking past outside the area: nothing.
    w.on_frame(FRAME, t + 200, False, [person(0.9, 0.8)], False)
    w.on_frame(FRAME, t + 260, False, [person(0.9, 0.8)], False)
    assert not drain(q, "loitering")


# ---- faces -----------------------------------------------------------------------------

def face(feature, size=80, score=0.95):
    return {"box": (10, 10, size, size), "score": score, "size": size, "feature": facemod.unit(feature),
            "crop": facemod.jpeg(np.zeros((20, 20, 3), np.uint8))}


def look(hub, w):
    """Runs the queued face checks straight away (there's no background worker in tests)."""
    while not hub.smart._jobs.empty():
        hub.smart.run_face_job(*hub.smart._jobs.get_nowait())


def test_family_is_quiet_and_strangers_alert(tmp_path):
    hub, w, q, engine, _ = make(tmp_path)
    alice = np.zeros(128, np.float32)
    alice[0] = 1
    stranger = np.zeros(128, np.float32)
    stranger[1] = 1
    p = hub.smart.store.add_person("Alice")
    hub.smart.store.add_face(p["id"], alice, facemod.jpeg(np.zeros((8, 8, 3), np.uint8)))
    hub.smart.update_settings({"faces": {"enabled": True, "wait_seconds": 10}})

    # The ordinary person alert waits for the face check.
    hub.events.publish({"type": "clip_started", "person": True, "camera": "front"})
    held = drain(q, "clip_started")[0]
    assert held["alert"] is False and held["held"] == "face check"
    hub.events.publish({"type": "clip_started", "person": False, "camera": "front"})
    assert "alert" not in drain(q, "clip_started")[0]  # plain movement is untouched

    # Alice comes home: recognised, no alert.
    engine.next = [[face(alice + 0.05)]]
    w.on_frame(FRAME, 100, True, [person(0.5, 0.9)], False)
    look(hub, w)
    known = drain(q, "known_person")
    assert len(known) == 1 and known[0]["person_name"] == "Alice" and known[0]["alert"] is False
    w.on_frame(FRAME, 111, False, [], False)
    assert not drain(q, "stranger")
    assert "Alice" in hub.cameras["front"].seen

    # Much later a stranger: two clear looks give a stranger alert, saved in "seen lately".
    engine.next = [[face(stranger)], [face(stranger)]]
    w.on_frame(FRAME, 500, True, [person(0.5, 0.9)], False)
    look(hub, w)
    w.on_frame(FRAME, 502, True, [person(0.5, 0.9)], False)
    look(hub, w)
    got = drain(q, "stranger")
    assert len(got) == 1 and got[0]["stranger"] is True and got[0]["unconfirmed"] is False
    assert "alert" not in got[0]  # no presence here, so the phone alerts as usual
    seen = hub.smart.store.public()["seen"]
    assert seen and seen[0]["kind"] == "face"
    # Naming them makes them family next time.
    hub.smart.store.assign_seen(seen[0]["id"], new_name="Bob")
    match, _ = hub.smart.store.match_face(stranger, facemod.FACE_MATCH["normal"])
    assert match and match["name"] == "Bob"

    # Nobody's face could be seen: "person seen" after the wait, as set.
    w.on_frame(FRAME, 900, True, [person(0.5, 0.9)], False)
    look(hub, w)
    w.on_frame(FRAME, 905, False, [], False)
    assert not drain(q, "stranger")
    w.on_frame(FRAME, 911, False, [], False)
    got = drain(q, "stranger")
    assert len(got) == 1 and got[0]["unconfirmed"] is True and "person seen" in got[0]["title"]


def test_faces_off_or_missing_models_leave_alerts_alone(tmp_path):
    hub, w, q, engine, _ = make(tmp_path)
    hub.events.publish({"type": "person", "camera": "front"})
    assert "held" not in drain(q)[0]
    hub.smart.update_settings({"faces": {"enabled": True}})
    engine.available = False
    hub.events.publish({"type": "person", "camera": "front"})
    assert "held" not in drain(q)[0]


def test_stranger_alerts_follow_home_and_away(tmp_path):
    hub, w, q, engine, _ = make(tmp_path, presence=True)
    hub.smart.update_settings({"faces": {"enabled": True, "no_face": "alert", "wait_seconds": 5}})
    hub.presence.report("phone", "Phone", True)   # someone's home, alerts off ...
    hub.presence.update({"home_alerts": "people"})  # ... except about people
    w.on_frame(FRAME, 10, True, [person(0.5, 0.9)], False)
    look(hub, w)
    w.on_frame(FRAME, 16, False, [], False)
    assert drain(q, "stranger")[0]["alert"] is True
    hub.presence.update({"home_alerts": "none"})
    w.on_frame(FRAME, 100, True, [person(0.5, 0.9)], False)
    look(hub, w)
    w.on_frame(FRAME, 106, False, [], False)
    assert drain(q, "stranger")[0]["alert"] is False


# ---- cars ------------------------------------------------------------------------------

def colour_frame(colour, box):
    img = np.full((360, 640, 3), (120, 120, 120), np.uint8)
    x1, y1, x2, y2 = box
    img[int(y1 * 360):int(y2 * 360), int(x1 * 640):int(x2 * 640)] = colour
    return img


def test_unknown_vehicle_stopping_alerts_but_ours_and_passing_cars_dont(tmp_path):
    hub, w, q, _, _ = make(tmp_path)
    red, blue = (30, 30, 200), (200, 60, 20)
    parked_box = (0.05, 0.5, 0.25, 0.8)
    spot = (0.5, 0.5, 0.8, 0.85)
    car = hub.smart.store.add_car("Red car")
    hub.smart.store.add_car_sample(car["id"], facemod.vehicle_signature(colour_frame(red, spot), spot))
    hub.smart.update_settings({"vehicles": {"enabled": True}})

    # When watching starts, cars already parked are just noted.
    f = colour_frame(blue, parked_box)
    assert w.wants_detection(0) is False
    w.on_frame(f, 1, False, None, False)
    assert w.wants_detection(1)
    for t in (2, 3, 4):
        w.on_frame(f, t, False, [vehicle(parked_box)], False)
    assert len(w.parked) == 1 and not drain(q, "vehicle")

    # A car driving past: never still, so nothing.
    for i, x in enumerate([0.3, 0.45, 0.6, 0.75]):
        w.on_frame(f, 10 + i, True, [vehicle(parked_box), vehicle((x, 0.5, x + 0.2, 0.8))], False)
    w.on_frame(f, 30, False, [vehicle(parked_box)], False)
    assert not drain(q, "vehicle")

    # Our red car stops: known, quiet.
    f = colour_frame(red, spot)
    for t in (40, 41, 42, 43):
        w.on_frame(f, t, t == 40, [vehicle(parked_box), vehicle(spot)], False)
    got = drain(q, "vehicle")
    assert len(got) == 1 and got[0]["known"] is True and got[0]["vehicle"] == "Red car" and got[0]["alert"] is False

    # It leaves; a blue car stops in the same place: unknown vehicle alert.
    for t in range(50, 62, 2):
        w.on_frame(colour_frame(blue, parked_box), t, True, [vehicle(parked_box)], False)
    f = colour_frame(blue, spot)
    for t in (70, 71, 72, 73):
        w.on_frame(f, t, t == 70, [vehicle(parked_box), vehicle(spot)], False)
    got = drain(q, "vehicle")
    assert len(got) == 1 and got[0]["known"] is False and "alert" not in got[0]
    assert hub.smart.store.public()["seen"][0]["kind"] == "vehicle"


def test_vehicle_likeness():
    box = (0.2, 0.2, 0.8, 0.8)
    red = facemod.vehicle_signature(colour_frame((30, 30, 200), box), box)
    red2 = facemod.vehicle_signature(colour_frame((40, 35, 190), box), box)
    blue = facemod.vehicle_signature(colour_frame((200, 60, 20), box), box)
    assert facemod.vehicle_likeness(red, red2) > 0.8
    assert facemod.vehicle_likeness(red, blue) < 0.5
    night = facemod.vehicle_signature(colour_frame((90, 90, 90), box), box)
    assert night["night"] and facemod.vehicle_likeness(night, red) == 0.0


# ---- parcels ---------------------------------------------------------------------------

def doorstep(parcel=False, light=0):
    rng = np.random.default_rng(7)
    img = (rng.integers(60, 120, (360, 640, 3))).astype(np.uint8)
    img = cv2.GaussianBlur(img, (9, 9), 0)
    if parcel:
        cv2.rectangle(img, (300, 250), (360, 300), (40, 110, 170), -1)
        cv2.rectangle(img, (300, 250), (360, 300), (20, 60, 90), 2)
    return cv2.convertScaleAbs(img, alpha=1.0, beta=light)


AREA = [[0.4, 0.6], [0.65, 0.6], [0.65, 0.95], [0.4, 0.95]]


def run_parcel(w, frame, start, seconds, people=None):
    for i in range(seconds):
        w.on_frame(frame, start + i, bool(people), [people] if people else [], False)


def test_parcel_left_and_taken(tmp_path):
    hub, w, q, _, _ = make(tmp_path)
    hub.smart.update_camera("front", {"parcel": {"enabled": True, "area": AREA}})
    empty, box = doorstep(), doorstep(parcel=True)
    run_parcel(w, empty, 0, 8)
    assert w.parcel_status()["state"] == "empty"
    # Slow light changes and people walking past leave nothing behind: no alerts.
    run_parcel(w, doorstep(light=6), 10, 15)
    run_parcel(w, empty, 30, 3, people=person(0.52, 0.9))
    run_parcel(w, empty, 33, 15)
    assert not drain(q, "parcel_arrived")
    # A courier comes and leaves a box.
    run_parcel(w, empty, 60, 3, people=person(0.52, 0.9))
    run_parcel(w, box, 63, 3, people=person(0.52, 0.9))
    run_parcel(w, box, 66, 12)
    got = drain(q, "parcel_arrived")
    assert len(got) == 1 and "parcel" in got[0]["title"]
    st = w.parcel_status()
    assert st["state"] == "parcel" and st["picture"] and st["since"]
    run_parcel(w, box, 80, 20)
    assert not drain(q, "parcel_arrived")
    # Someone takes it while nobody's home: an alert.
    run_parcel(w, box, 120, 3, people=person(0.52, 0.9))
    run_parcel(w, empty, 123, 2, people=person(0.52, 0.9))
    run_parcel(w, empty, 125, 12)
    got = drain(q, "parcel_taken")
    assert len(got) == 1 and got[0]["alert"] is True and got[0]["away"] is True
    assert w.parcel_status()["state"] == "empty"
    assert "Parcel" in hub.cameras["front"].seen and "Parcel taken" in hub.cameras["front"].seen


def test_parcel_taken_while_home_is_quiet(tmp_path):
    hub, w, q, _, _ = make(tmp_path, presence=True)
    hub.presence.report("phone", "Phone", True)
    hub.smart.update_camera("front", {"parcel": {"enabled": True, "area": AREA}})
    empty, box = doorstep(), doorstep(parcel=True)
    run_parcel(w, empty, 0, 8)
    run_parcel(w, box, 10, 3, people=person(0.52, 0.9))
    run_parcel(w, box, 13, 12)
    assert len(drain(q, "parcel_arrived")) == 1
    run_parcel(w, empty, 30, 3, people=person(0.52, 0.9))
    run_parcel(w, empty, 33, 12)
    got = drain(q, "parcel_taken")
    assert len(got) == 1 and got[0]["alert"] is False and "picked up" in got[0]["title"]


# ---- the web routes --------------------------------------------------------------------

def test_smart_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    hub, w, q, engine, path = make(tmp_path)
    client = TestClient(create_app(hub))
    assert client.get("/api/smart").status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200

    st = client.get("/api/smart").json()
    assert st["faces_available"] and st["people"] == [] and "front" in st["cameras"]
    st = client.put("/api/smart/settings", json={"faces": {"enabled": True, "strictness": "strict"},
                                                  "parcels": {"taken_alert": "always"}}).json()
    assert st["settings"]["faces"]["enabled"] and st["settings"]["faces"]["strictness"] == "strict"
    saved = json.loads(path.read_text())["smart"]
    assert saved["parcels"]["taken_alert"] == "always"

    # A person with a photo: the face in it is learned.
    photo = "data:image/jpeg;base64," + __import__("base64").b64encode(facemod.jpeg(FRAME)).decode()
    engine.next = [[face(np.ones(128))]]
    st = client.post("/api/smart/people", json={"name": "Alastair", "photo": photo}).json()
    assert st["people"][0]["name"] == "Alastair" and len(st["people"][0]["faces"]) == 1
    pic = st["people"][0]["faces"][0]["pic"]
    assert client.get(f"/api/smart/pics/{pic}").headers["content-type"] == "image/jpeg"
    assert client.get("/api/smart/pics/..%2Fknown.json").status_code == 404
    # No face in the photo: a clear message, and no empty person left behind.
    r = client.post("/api/smart/people", json={"name": "Nobody", "photo": photo})
    assert r.status_code == 400 and "No clear face" in r.json()["detail"]
    assert len(client.get("/api/smart").json()["people"]) == 1
    pid = st["people"][0]["id"]
    assert client.put(f"/api/smart/people/{pid}", json={"name": "Al"}).json()["people"][0]["name"] == "Al"

    # Cars from the live picture.
    r = client.post("/api/smart/cars", json={"name": "Golf", "camera": "front"})
    assert r.status_code == 400  # no picture yet
    w.frame = colour_frame((30, 30, 200), (0.2, 0.2, 0.6, 0.6))
    hub.detector.objects = [("vehicle", 0.8, (0.2, 0.2, 0.6, 0.6))]
    st = client.post("/api/smart/cars", json={"name": "Golf", "camera": "front"}).json()
    assert st["cars"][0]["name"] == "Golf" and len(st["cars"][0]["samples"]) == 1

    # Rules for a camera.
    r = client.put("/api/smart/cameras/front", json={"parcel": {"enabled": True, "area": AREA},
                                                    "lines": [{"a": [0, 0.5], "b": [1, 0.5]}]})
    assert r.status_code == 200 and r.json()["lines"][0]["name"] == "Line 1"
    assert client.put("/api/smart/cameras/nope", json={}).status_code == 404
    assert client.post("/api/smart/cameras/front/parcel/clear").json()["state"] == "learning"
    st = client.get("/api/smart").json()
    assert st["cameras"]["front"]["parcel"]["enabled"] and st["cameras"]["front"]["parcel_status"]["state"] == "learning"

    assert client.delete(f"/api/smart/people/{pid}").json()["people"] == []
    assert client.delete("/api/smart/seen/nope").status_code == 404


@pytest.mark.skipif(not os.path.exists(facemod.FACE_RECOGNISE), reason="face models not downloaded")
def test_real_face_models_load():
    eng = facemod.FaceEngine()
    assert eng.available
    assert eng.faces(FRAME) == []
