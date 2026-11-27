"""Learning as you go, following people with the camera, sharper faces, and privacy areas that
move with the picture.  Run:  python -m pytest tests"""
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import faces as facemod, follow, learning, privacy, viewmotion  # noqa: E402
from tests.test_smart import FRAME, drain, face, look, make, person  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def axis(i):
    v = np.zeros(128, np.float32)
    v[i] = 1
    return v


def like(base, other, sim):
    """A face exactly `sim` alike to base."""
    return facemod.unit(sim * base + np.sqrt(1 - sim ** 2) * other)


def with_alice(tmp_path, **learn):
    hub, w, q, engine, path = make(tmp_path)
    p = hub.smart.store.add_person("Alice")
    hub.smart.store.add_face(p["id"], axis(0), facemod.jpeg(np.zeros((8, 8, 3), np.uint8)))
    hub.smart.update_settings({"faces": {"enabled": True, "wait_seconds": 10}, "learn": learn})
    return hub, w, q, engine, p


def kinds(q):
    """The events published since last asked, by type."""
    out = {}
    for e in drain(q):
        out.setdefault(e["type"], []).append(e)
    return out


def visit(hub, w, engine, feats, t0):
    """Someone comes into view and is looked at once per face given, then the wait passes."""
    for i, f in enumerate(feats):
        engine.next = [[face(f)]]
        w.on_frame(FRAME, t0 + i * 2, True, [person(0.5, 0.9)], False)
        look(hub, w)
    w.on_frame(FRAME, t0 + 11 + len(feats) * 2, False, [], False)


# ---- learning as you go ------------------------------------------------------------------

def test_a_face_close_to_alice_asks_instead_of_a_stranger_alert(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    unsure = like(axis(0), axis(5), 0.36)         # normal matching needs 0.40
    visit(hub, w, engine, [unsure], 100)
    got = kinds(q)
    assert "stranger" not in got
    asked = got["who_is_this"]
    assert len(asked) == 1 and asked[0]["person_name"] == "Alice" and "is this Alice" in asked[0]["title"]
    assert asked[0]["question_pic"].startswith("/api/smart/pics/q_")
    qs = hub.smart.questions.public()
    assert len(qs) == 1 and qs[0]["id"] == asked[0]["question"] and "feature" not in qs[0]

    # Yes: Alice is known by that face from now on.
    said = hub.smart.questions.answer(qs[0]["id"], "yes")
    assert "Alice" in said and hub.smart.questions.public() == []
    who, _ = hub.smart.store.match_face(unsure, facemod.FACE_MATCH["normal"])
    assert who and who["name"] == "Alice"
    visit(hub, w, engine, [unsure], 500)
    assert set(kinds(q)) == {"known_person"}


def test_no_means_never_taken_for_her_and_the_face_goes_to_seen_lately(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    unsure = like(axis(0), axis(5), 0.37)
    visit(hub, w, engine, [unsure], 100)
    qid = drain(q, "who_is_this")[0]["question"]
    said = hub.smart.questions.answer(qid, "no")
    assert "won't be taken for Alice" in said
    assert [s["kind"] for s in hub.smart.store.public()["seen"]] == ["face"]
    who, _ = hub.smart.store.closest_face(like(axis(0), axis(5), 0.39))
    assert who is None
    # Next time someone with that face comes, it's a stranger alert, not a question.
    visit(hub, w, engine, [unsure, unsure], 500)
    got = kinds(q)
    assert "stranger" in got and "who_is_this" not in got
    # Alice herself is still known.
    who, _ = hub.smart.store.match_face(axis(0), facemod.FACE_MATCH["normal"])
    assert who["name"] == "Alice"


def test_someone_else_and_one_question_at_a_time(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    unsure = like(axis(0), axis(5), 0.36)
    visit(hub, w, engine, [unsure], 100)
    visit(hub, w, engine, [like(axis(0), axis(5), 0.35)], 300)   # the same face again: no new question
    got = kinds(q)
    assert len(got["who_is_this"]) == 1 and "stranger" not in got
    qid = hub.smart.questions.public()[0]["id"]
    with pytest.raises(ValueError):
        hub.smart.questions.answer(qid, "maybe")
    said = hub.smart.questions.answer(qid, "other", name="Bea")
    assert "Bea" in said
    who, _ = hub.smart.store.match_face(unsure, facemod.FACE_MATCH["normal"])
    assert who["name"] == "Bea"
    with pytest.raises(KeyError):
        hub.smart.questions.answer(qid, "yes")


def test_asking_can_be_switched_off(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path, ask=False)
    visit(hub, w, engine, [like(axis(0), axis(5), 0.36)], 100)
    got = kinds(q)
    assert "stranger" in got and "who_is_this" not in got


def test_very_sure_new_looking_faces_are_learnt_by_themselves(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    new_light = like(axis(0), axis(7), 0.70)       # sure (0.40 + 0.14) but not like her one photo
    visit(hub, w, engine, [new_light], 100)
    faces = hub.smart.store.public()["people"][0]["faces"]
    assert len(faces) == 2 and faces[1]["auto"] and not faces[0]["auto"]
    visit(hub, w, engine, [like(axis(0), axis(7), 0.98)], 300)   # nothing new about this one
    assert len(hub.smart.store.public()["people"][0]["faces"]) == 2

    # Learnt faces never push out the photos you chose, and only a few are kept.
    (tmp_path / "b").mkdir()
    hub2, *_ = with_alice(tmp_path / "b")
    pid = hub2.smart.store.known["people"][0]["id"]
    for i in range(learning.AUTO_KEEP + 5):
        hub2.smart.store.add_face(pid, axis(10 + i), auto=True)
    kept = hub2.smart.store.known["people"][0]["faces"]
    assert len(kept) == learning.AUTO_KEEP + 1 and not kept[0].get("auto")


def test_learning_by_itself_can_be_switched_off(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path, auto=False)
    visit(hub, w, engine, [like(axis(0), axis(7), 0.70)], 100)
    assert len(hub.smart.store.public()["people"][0]["faces"]) == 1


def test_several_unclear_looks_together_recognise_her(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    # Each look alone is too unlike Alice, but they agree with each other about her.
    visit(hub, w, engine, [like(axis(0), axis(5), 0.33), like(axis(0), axis(6), 0.33)], 100)
    got = kinds(q)
    assert set(got) == {"known_person"} and got["known_person"][0]["person_name"] == "Alice"


def test_questions_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    hub, w, q, engine, alice = with_alice(tmp_path)
    visit(hub, w, engine, [like(axis(0), axis(5), 0.36)], 100)
    client = TestClient(create_app(hub))
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200
    st = client.get("/api/smart").json()
    assert st["settings"]["learn"] == {"ask": True, "auto": True}
    assert st["settings"]["tracking"]["follow"] is False
    qid = st["questions"][0]["id"]
    assert client.get(f"/api/smart/pics/{st['questions'][0]['pic']}").status_code == 200
    assert client.post(f"/api/smart/questions/{qid}", json={"answer": "eh"}).status_code == 400
    r = client.post(f"/api/smart/questions/{qid}", json={"answer": "yes"}).json()
    assert "Alice" in r["said"] and r["questions"] == []
    assert client.post(f"/api/smart/questions/{qid}", json={"answer": "yes"}).status_code == 404
    st = client.put("/api/smart/settings", json={"learn": {"ask": False}, "tracking": {"follow": True, "max_seconds": 5}}).json()
    assert st["settings"]["learn"]["ask"] is False and st["settings"]["tracking"] == {"follow": True, "zoom": False, "max_seconds": 20}
    # Removing a person removes questions about them.
    visit(hub, w, engine, [like(axis(0), axis(9), 0.30)], 900)
    client.put("/api/smart/settings", json={"learn": {"ask": True}})
    visit(hub, w, engine, [like(axis(0), axis(9), 0.36)], 1500)
    assert client.get("/api/smart/questions").json()["questions"]
    client.delete(f"/api/smart/people/{alice['id']}")
    assert client.get("/api/smart/questions").json()["questions"] == []


# ---- sharper faces ----------------------------------------------------------------------

def test_enhance_enlarges_and_brightens_a_small_dark_picture():
    rng = np.random.default_rng(3)
    small = (rng.integers(0, 40, (60, 50, 3))).astype(np.uint8)
    small[:] = small[:, :, :1]           # black and white, as at night
    out, scale = facemod.enhance(small)
    assert scale > 4 - 1e-6 and out.shape[0] == 240 and out.shape[1] == 200
    assert out.mean() > small.mean() + 10
    big = np.full((500, 400, 3), 128, np.uint8)
    out, scale = facemod.enhance(big)
    assert scale == 1.0 and out.shape == big.shape


@pytest.mark.skipif(not os.path.exists(facemod.FACE_RECOGNISE), reason="face models not downloaded")
def test_sharpened_faces_report_their_real_size():
    eng = facemod.FaceEngine()
    assert eng.faces_sharpened(np.full((40, 40, 3), 30, np.uint8)) == []


# ---- where the camera points ----------------------------------------------------------------

def scenery(seed=4):
    rng = np.random.default_rng(seed)
    img = np.zeros((1200, 2400, 3), np.uint8)
    for _ in range(900):
        c = tuple(int(x) for x in rng.integers(0, 255, 3))
        p = (int(rng.integers(0, 2400)), int(rng.integers(0, 1200)))
        if rng.random() < 0.5:
            cv2.circle(img, p, int(rng.integers(6, 40)), c, -1)
        else:
            cv2.rectangle(img, p, (p[0] + int(rng.integers(8, 60)), p[1] + int(rng.integers(8, 60))), c, -1)
    return cv2.GaussianBlur(img, (3, 3), 0)


def test_view_tracker_follows_a_pan_and_a_zoom():
    world = scenery()
    vt = viewmotion.ViewTracker()
    view = lambda x, y, w=1280, h=720: cv2.resize(world[y:y + h, x:x + w], (640, 360))  # noqa: E731
    assert vt.matrix() is None and vt.to_home([(0.3, 0.4)]) == [[0.3, 0.4]]
    vt.start(view(400, 200))
    for dx in range(0, 321, 16):                  # pan right a quarter of the picture
        assert vt.update(view(400 + dx, 200 + dx // 4))
    hx, hy = vt.centre_in_home()
    assert abs(hx - (0.5 + 320 / 1280)) < 0.02 and abs(hy - (0.5 + 80 / 720)) < 0.02
    # A privacy area at the right of the usual view is now in the middle-left of the picture.
    m = viewmotion.project_masks([{"points": [[0.75, 0.4], [0.8, 0.4], [0.8, 0.5]]}], vt.matrix())
    assert abs(m[0]["points"][0][0] - 0.5) < 0.02
    # A person's box maps back to the usual view.
    b = vt.box_to_home((0.45, 0.3, 0.55, 0.7))
    assert abs(b[0] - 0.70) < 0.02
    # Zoom in on the middle: things look bigger.
    for z in np.linspace(1.0, 1.4, 9)[1:]:
        w, h = int(1280 / z), int(720 / z)
        cx, cy = 400 + 320 + 640, 200 + 80 + 360
        assert vt.update(view(cx - w // 2, cy - h // 2, w, h))
    assert abs(vt.zoom() - 1.4) < 0.06
    hx, hy = vt.centre_in_home()
    assert abs(hx - 0.75) < 0.03
    vt.stop()
    assert vt.matrix() is None


def test_view_is_lost_on_a_blank_picture():
    vt = viewmotion.ViewTracker()
    vt.start(cv2.resize(scenery()[0:720, 0:1280], (640, 360)))
    blank = np.full((360, 640, 3), 40, np.uint8)
    results = [vt.update(blank) for _ in range(viewmotion.LOST_AFTER)]
    assert results[-1] is False and vt.lost


@needs_ffmpeg
def test_recording_masks_move_with_the_picture(tmp_path):
    src, dst = tmp_path / "clip.mp4.part", tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
                    "-t", "3", "-c:v", "libx264", "-g", "15", "-f", "mp4", str(src)], check=True)
    tl_ = [{"points": [[0, 0], [0.4, 0], [0.4, 0.4], [0, 0.4]]}]
    br = [{"points": [[0.6, 0.6], [1, 0.6], [1, 1], [0.6, 1]]}]
    timeline = [(0.0, tl_), (1.0, tl_), (1.2, br), (1.4, br)]
    assert privacy.mask_clip(str(src), str(dst), {"style": "black", "masks": tl_}, timeline)

    def frame(sec):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(sec), "-i", str(dst), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, np.uint8).reshape(360, 640, 3)
    early, late = frame(0.5), frame(2.5)
    assert early[10:130, 10:240].mean() < 20 and early[230:350, 400:630].mean() > 60
    assert late[230:350, 400:630].mean() < 20 and late[10:130, 10:240].mean() > 60


def test_camera_keeps_track_of_masks_during_a_clip(tmp_path):
    from camhub.camera import Camera

    cam = Camera.__new__(Camera)
    cam.cfg = {"privacy": {"style": "black", "masks": [{"points": [[0, 0], [0.2, 0], [0.2, 0.2]]}]}}
    cam.view = viewmotion.ViewTracker()
    cam._view_logged = cam.view.version
    import collections
    cam.view_log = collections.deque()
    assert cam.views_between(0, 100) is None            # never moved: masks stay put
    assert cam.privacy_now()["masks"] == cam.cfg["privacy"]["masks"]
    cam.view.start()
    cam.view.lost = True
    assert cam.privacy_now()["masks"] == viewmotion.FULL_FRAME   # can't tell: cover everything
    cam._log_view(50)
    tl = cam.views_between(40, 100)
    assert tl[0] == (0.0, cam.cfg["privacy"]["masks"]) and tl[1] == (10.0, viewmotion.FULL_FRAME)
    assert len(cam.privacy_recent(10 ** 9)["masks"]) == 2


# ---- following people ----------------------------------------------------------------------

PROPERTY = [[0, 0.5], [1, 0.5], [1, 1], [0, 1]]     # the bottom half of the picture


class FakePtz:
    available = True
    zoom = False

    def __init__(self):
        self.calls = []
        self.done = threading.Event()

    def _log(self, *c):
        self.calls.append(c)
        self.done.set()

    def move(self, pan, tilt, zoom=0.0):
        self._log("move", pan, tilt, zoom)

    def stop(self):
        self._log("stop")

    def save_preset(self, name, taken=()):
        self._log("save", name)
        return "P9"

    def update_preset(self, token, name):
        self._log("update", token)
        return token

    def goto_preset(self, token):
        self._log("goto", token)

    def home(self):
        self._log("home")

    def wait_for(self, kind, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if any(c[0] == kind for c in self.calls):
                return True
            time.sleep(0.02)
        return False


def follower(tracking=None, prop=PROPERTY, masks=()):
    settings = {"tracking": {**follow.DEFAULT_TRACKING, "follow": True, **(tracking or {})}}
    smart = SimpleNamespace(hub=SimpleNamespace(settings=settings), rules={"property": prop},
                            wants_face_zoom=lambda box, zoomed=False: False, look_now=lambda: None)
    cam = SimpleNamespace(id="front", smart=smart, ptz=FakePtz(), view=viewmotion.ViewTracker(),
                          cfg={"privacy": {"masks": list(masks)}}, ptz_cfg={"presets": [{"token": "P1"}]},
                          kept=[], moved=[])
    cam.raw_frame = lambda: None
    cam.keep_recording = cam.kept.append
    cam.smart_look_now = lambda: None
    cam.ptz_moved = lambda manual=True, seconds=6: cam.moved.append((manual, seconds))
    f = follow.Follower(cam)
    return f, cam


def at(x, y, h=0.3):
    p = person(x, y, h)
    p["home"] = p["box"]
    return p


def test_follows_someone_on_the_property_and_goes_back():
    f, cam = follower()
    f.step(100, [at(0.9, 0.8)])
    assert f.state == "follow"
    f.step(100.2, [at(0.9, 0.8)])            # to the right of the middle: turn right
    assert cam.ptz.wait_for("move")
    names = [c[0] for c in cam.ptz.calls]
    assert names.index("save") < names.index("move")      # where it was is saved first
    move = next(c for c in cam.ptz.calls if c[0] == "move")
    assert move[1] > 0 and move[3] == 0
    assert cam.ptz_cfg["return_token"] == "P9" and cam.view.active and f.steering
    assert cam.kept and f.status()["state"] == "follow"
    f.step(100.4, [at(0.72, 0.8)])
    f.step(100.6, [at(0.52, 0.8)])           # in the middle now: stop turning
    assert cam.ptz.wait_for("stop")
    f.step(100.8, [at(0.5, 0.6)])
    f.step(101, [at(0.5, 0.42)])             # walked off the property
    assert f.state == "back" and f.reason == "left the property"
    assert cam.ptz.wait_for("goto") and ("goto", "P9") in cam.ptz.calls
    assert cam.moved[-1] == (False, follow.RETURN_SETTLE)
    f.step(101 + follow.RETURN_SETTLE + 0.1, None)
    assert f.state == "idle" and not cam.view.active
    # Next time it reuses the same slot.
    f.step(200, [at(0.9, 0.8)])
    f.step(200.2, [at(0.9, 0.8)])
    assert cam.ptz.wait_for("update")


def test_only_people_on_the_property_and_not_in_privacy_areas():
    f, cam = follower()
    f.step(100, [at(0.9, 0.3)])                                       # the street
    f.step(100, [dict(at(0.9, 0.8), ignored=True)])                   # an ignore zone
    assert f.state == "idle"
    f, cam = follower(masks=[{"points": [[0.8, 0.6], [1, 0.6], [1, 1], [0.8, 1]]}])
    f.step(100, [at(0.9, 0.8)])
    assert f.state == "idle"
    f, cam = follower(prop=[])                                        # property not drawn
    f.step(100, [at(0.9, 0.8)])
    assert f.state == "idle" and not f.enabled()
    f, cam = follower(tracking={"follow": False})
    f.step(100, [at(0.9, 0.8)])
    assert f.state == "idle"


def test_out_of_sight_time_limit_and_steering_by_hand():
    f, cam = follower()
    f.step(100, [at(0.9, 0.8)])
    f.step(100.2, [at(0.9, 0.8)])
    f.step(100.3, [])
    assert f.state == "follow"
    f.step(100.2 + follow.LOSE_AFTER + follow.WAIT_FOR_RETURN + 0.1, [])
    assert f.state == "back" and f.reason == "out of sight"

    f, cam = follower(tracking={"max_seconds": 20})
    f.step(100, [at(0.9, 0.8)])
    f.step(121, [at(0.9, 0.8)])
    assert f.state == "idle" and f.reason == "time limit"   # never moved, so nothing to go back to

    f, cam = follower()
    f.step(100, [at(0.9, 0.8)])
    f.step(100.2, [at(0.9, 0.8)])
    assert cam.ptz.wait_for("move")
    f.cancel()
    time.sleep(0.2)
    assert f.state == "idle" and not cam.view.active and not any(c[0] == "goto" for c in cam.ptz.calls)


def test_lost_view_sends_it_straight_back():
    f, cam = follower()
    f.step(100, [at(0.9, 0.8)])
    f.step(100.2, [at(0.9, 0.8)])
    f.step(100.4, [at(0.9, 0.8)], view_ok=False)
    assert f.state == "back" and "lost track" in f.reason


def test_zooms_in_on_a_face_then_back_out():
    f, cam = follower(tracking={"zoom": True})
    cam.ptz.zoom = True
    want = {"zoom": True}
    cam.smart.wants_face_zoom = lambda box, zoomed=False: want["zoom"]
    f.step(100, [at(0.5, 0.95, h=0.4)])
    f.step(100.2, [at(0.5, 0.95, h=0.4)])
    assert f.state == "zoom" and f.phase == "centre"
    t = 100.4
    while f.phase == "centre" and t < 105:
        f.step(t, [at(0.5, 1.0, h=0.4)])      # head is up at the top: tilt up first
        t += 0.2
    assert f.phase == "in"
    f.step(t, [at(0.5, 0.95, h=0.4)])
    f.step(t + 0.2, [at(0.5, 0.95, h=0.4)])
    assert cam.ptz.wait_for("move")
    assert any(c[0] == "move" and c[3] > 0 for c in cam.ptz.calls)       # zooming in
    f.step(t + 0.4, [at(0.5, 0.95, h=0.7)])                              # big enough
    assert f.phase == "hold"
    want["zoom"] = False                                                 # face recognised
    f.step(t + 0.6, [at(0.5, 0.95, h=0.7)])
    assert f.phase == "out"
    f.step(t + 0.7, [at(0.5, 0.95, h=0.7)])
    time.sleep(0.15)
    assert any(c[0] == "move" and c[3] < 0 for c in cam.ptz.calls)       # zooming out
    f.step(t + 5, [at(0.5, 0.95, h=0.4)])
    assert f.state == "follow" and f.target["zooms"] == 1


def test_wants_face_zoom_from_the_smart_rules(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    assert not w.wants_face_zoom((0, 0, 1, 1))              # nobody here
    engine.next = [[], []]
    w.on_frame(FRAME, 100, True, [person(0.5, 0.9)], False)
    look(hub, w)
    w.on_frame(FRAME, 102, True, [person(0.5, 0.9)], False)
    look(hub, w)
    assert w.wants_face_zoom((0, 0, 1, 1))                 # looked twice, no face seen
    engine.next = [[face(axis(0))]]
    w.on_frame(FRAME, 104, True, [person(0.5, 0.9)], False)
    look(hub, w)
    assert not w.wants_face_zoom((0, 0, 1, 1), zoomed=True)   # Alice recognised


def test_rules_use_the_usual_view_while_following(tmp_path):
    hub, w, q, engine, alice = with_alice(tmp_path)
    hub.smart.update_camera("front", {"property": PROPERTY})
    p = person(0.5, 0.3)              # in the moved picture it looks off the property...
    p["home"] = (0.45, 0.6, 0.55, 0.9)  # ...but in the usual view they stand on it
    engine.next = [[face(axis(0))]]
    w.on_frame(FRAME, 100, True, [p], False, follow=True)
    look(hub, w)
    assert drain(q, "known_person")
    v = w.visit
    assert v["on_property"]


@needs_ffmpeg
def test_live_picture_keeps_privacy_areas_covered_while_the_camera_turns(tmp_path):
    """The camera's analyser follows the picture as it pans, and covers the private window where
    it now is, not where it was."""
    import json

    from camhub import camera as cam_mod
    from camhub.config import DEFAULT_MOTION, Config
    from camhub.events import EventBus
    from camhub.storage import Storage

    world = scenery(7)
    video = tmp_path / "pan.mp4"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    frames = 60
    for i in range(frames):
        x = 300 + i * 4                     # pans right 4 pixels (of 1280) a frame
        out.write(cv2.resize(world[200:920, x:x + 1280], (640, 360), interpolation=cv2.INTER_AREA))
    out.release()
    (tmp_path / "config.json").write_text(json.dumps({"storage": {"path": str(tmp_path / "rec")}, "cameras": []}))
    cfg = Config(str(tmp_path / "config.json"))
    window = [[0.6, 0.3], [0.75, 0.3], [0.75, 0.6], [0.6, 0.6]]
    c = {"id": "garden", "name": "Garden", "main_url": "", "sub_url": str(video), "motion": dict(DEFAULT_MOTION),
         "privacy": {"style": "black", "masks": [{"name": "Window", "points": window}]}}

    class NoPeople:
        available = False

    cam = cam_mod.Camera(c, Storage(cfg), EventBus(), NoPeople())
    cam.view.start()                        # as if following someone had just begun
    t = threading.Thread(target=cam._analyze_loop, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and cam._frame_id < frames - 8:
            time.sleep(0.05)
        hx, _ = cam.view.centre_in_home()
        shifted = hx - 0.5
        assert 0.1 < shifted < 0.25, shifted
        frame = cam._frame
        x1 = int((0.6 - shifted) * 640) + 6
        x2 = int((0.75 - shifted) * 640) - 6
        assert frame[115:210, x1:x2].max() == 0             # covered where the window is now
        assert frame[115:210, 400:470].mean() > 30          # not where it used to be
        assert len(cam.view_log) > 10                       # remembered for the recording
    finally:
        cam.stop()
