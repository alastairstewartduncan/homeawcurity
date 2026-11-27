"""Knowing family without their face, and remembering visitors who keep coming back.
Run:  python -m pytest tests"""
import datetime
import json
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from camhub import bodies as bodymod, faces as facemod, smart  # noqa: E402
from test_smart import FRAME, drain, face, look, make, person  # noqa: E402

CROP = facemod.jpeg(np.zeros((40, 20, 3), np.uint8))


class FakeBodyEngine:
    available = True

    def __init__(self):
        self.next = []      # the bodies the next looks will see

    def describe(self, img, box):
        if not self.next:
            return None
        feat = self.next.pop(0)
        if feat is None:
            return None
        return {"feature": facemod.unit(feat), "clothes": None, "night": False, "crop": CROP}


def vec(i, n=768, noise=0.0, seed=0):
    v = np.zeros(n, np.float32)
    v[i] = 1
    if noise:
        v += np.random.default_rng(seed).normal(0, noise, n).astype(np.float32)
    return facemod.unit(v)


def setup(tmp_path, **settings):
    hub, w, q, engine, path = make(tmp_path)
    body = FakeBodyEngine()
    hub.smart.body_engine = body
    s = {"faces": {"enabled": True, "wait_seconds": 6, "no_face": "alert"}}
    s.update(settings)
    hub.smart.update_settings(s)
    return hub, w, q, engine, body, path


def visit(hub, w, engine, body, t, faces=(), bodies=(), x=0.5):
    """One visit: a look for each entry, then they leave and the visit ends."""
    n = max(len(faces), len(bodies), 1)
    faces, bodies = list(faces) + [[]] * (n - len(faces)), list(bodies) + [None] * (n - len(bodies))
    for i in range(n):
        engine.next = [faces[i]]
        body.next = [bodies[i]]
        w.on_frame(FRAME, t + i * 2, True, [person(x, 0.9)], False)
        look(hub, w)
    w.on_frame(FRAME, t + 2 * n + 7, False, [], False)    # past the wait: decided
    w.on_frame(FRAME, t + 2 * n + 60, False, [], False)   # long gone: the visit is over


# ---- family from behind ----------------------------------------------------------------

def test_family_learnt_from_their_face_are_then_known_from_behind(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path, bodies={"enabled": True})
    alice_face = np.zeros(128, np.float32)
    alice_face[0] = 1
    p = hub.smart.store.add_person("Alice")
    hub.smart.store.add_face(p["id"], alice_face, CROP)

    # She comes home facing the camera: her face is known, and her build and clothes are learnt.
    visit(hub, w, engine, body, 100, faces=[[face(alice_face)]], bodies=[vec(0, noise=0.01, seed=1)])
    assert drain(q, "known_person")[0]["recognised_by"] == "face"
    assert len(hub.smart.store.known["people"][0]["bodies"]) == 1
    assert hub.smart.store.public()["people"][0]["bodies"][0]["pic"]

    # Later she walks away from the camera: no face, but two looks at her back agree.
    visit(hub, w, engine, body, 1000, bodies=[vec(0, noise=0.012, seed=2), vec(0, noise=0.012, seed=3)])
    got = drain(q)
    known = [e for e in got if e["type"] == "known_person"]
    assert len(known) == 1 and known[0]["person_name"] == "Alice" and known[0]["recognised_by"] == "body"
    assert known[0]["alert"] is False and "build" in known[0]["message"]
    assert not [e for e in got if e["type"] == "stranger"]   # no "person seen"

    # Someone else's back: a "person seen" as before.
    visit(hub, w, engine, body, 2000, bodies=[vec(5), vec(5)])
    got = drain(q, "stranger")
    assert len(got) == 1 and got[0]["unconfirmed"] is True


def test_a_clear_unknown_face_beats_a_body_match(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path, bodies={"enabled": True})
    p = hub.smart.store.add_person("Alice")
    hub.smart.store.add_body(p["id"], {"feature": vec(0), "crop": CROP})
    other = np.zeros(128, np.float32)
    other[3] = 1
    # Dressed like Alice, but a face that isn't hers: a stranger alert.
    visit(hub, w, engine, body, 100, faces=[[face(other)], [face(other)]], bodies=[vec(0), vec(0)])
    got = drain(q, "stranger")
    assert len(got) == 1 and got[0]["unconfirmed"] is False


def test_bodies_off_means_no_learning_or_matching(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path)
    alice_face = np.zeros(128, np.float32)
    alice_face[0] = 1
    p = hub.smart.store.add_person("Alice")
    hub.smart.store.add_face(p["id"], alice_face, CROP)
    visit(hub, w, engine, body, 100, faces=[[face(alice_face)]], bodies=[vec(0)])
    assert hub.smart.store.known["people"][0]["bodies"] == []


def test_height_tells_a_child_from_an_adult():
    m = bodymod.HeightModel()
    for i in range(30):
        m.add((0.45, 0.9 - 0.4 - (i % 3) * 0.01, 0.55, 0.9))   # adults, about 0.4 tall at this spot
    assert m.ratio((0.45, 0.9 - 0.4, 0.55, 0.9)) == pytest.approx(1.0, abs=0.03)
    child = m.ratio((0.47, 0.9 - 0.24, 0.53, 0.9))
    assert child == pytest.approx(0.6, abs=0.03)
    assert m.ratio((0.45, 0.2, 0.55, 0.4)) is None        # nobody seen standing over there yet
    assert m.ratio((0.45, 0.0, 0.55, 0.9)) is None         # cut off at the top
    # Same clothes and build, but much shorter: not the same person.
    samples = [{"feature": vec(0), "clothes": None, "night": False}]
    assert bodymod.likeness({"feature": vec(0), "height": 1.0}, samples, height=1.0) > 0.95
    assert bodymod.likeness({"feature": vec(0), "height": child}, samples, height=1.0) == 0.0
    assert bodymod.likeness({"feature": vec(0), "height": 1.05}, samples, height=1.0) < \
        bodymod.likeness({"feature": vec(0), "height": 1.0}, samples, height=1.0)


def test_clothes_colours():
    red = np.zeros((200, 80, 3), np.uint8)
    red[:, :] = (30, 30, 200)
    blue = red.copy()
    blue[:, :] = (200, 60, 20)
    a, b = bodymod.clothes(red), bodymod.clothes(blue)
    assert bodymod.clothes_likeness(a, bodymod.clothes(red.copy()), False) > 0.95
    assert bodymod.clothes_likeness(a, b, False) < 0.5
    assert bodymod.unpack(bodymod.pack(vec(7)))[7] == pytest.approx(1.0, abs=1e-3)


# ---- returning strangers ---------------------------------------------------------------

def test_a_stranger_seen_three_times_is_a_returning_stranger(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path, visitors={"enabled": True, "visits": 3, "days": 7})
    bob = np.zeros(128, np.float32)
    bob[2] = 1
    for i, t in enumerate((100, 1000)):
        visit(hub, w, engine, body, t, faces=[[face(bob)], [face(bob)]])
        got = drain(q, "stranger")
        assert len(got) == 1 and not got[0].get("returning")
    vis = hub.smart.state()["visitors"]
    assert len(vis) == 1 and vis[0]["recent"] == 2 and vis[0]["face"]

    # Third time: a returning stranger alert instead of the plain stranger one.
    visit(hub, w, engine, body, 2000, faces=[[face(bob)], [face(bob)]])
    got = drain(q)
    assert not [e for e in got if e["type"] == "stranger"]
    ret = [e for e in got if e["type"] == "returning_stranger"]
    assert len(ret) == 1 and ret[0]["stranger"] is True and ret[0]["visits"] == 3
    assert "3 times in the last 7 days" in ret[0]["message"]
    vis = hub.smart.state()["visitors"]
    assert len(vis) == 1 and vis[0]["total"] == 3 and len(vis[0]["visits"]) == 3
    assert all(x["pic"] for x in vis[0]["visits"])

    # Someone else entirely is a new visitor.
    carol = np.zeros(128, np.float32)
    carol[9] = 1
    visit(hub, w, engine, body, 3000, faces=[[face(carol)], [face(carol)]])
    assert drain(q, "stranger") and len(hub.smart.state()["visitors"]) == 2

    # The postman: marked as a regular, so no more returning stranger alerts.
    hub.smart.visitors.update(vis[0]["id"], label="Postman", regular=True)
    visit(hub, w, engine, body, 4000, faces=[[face(bob)], [face(bob)]])
    got = drain(q)
    assert not [e for e in got if e["type"] == "returning_stranger"] and [e for e in got if e["type"] == "stranger"]


def test_returning_stranger_without_a_face_goes_by_build_and_clothes(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path, bodies={"enabled": True},
                                       visitors={"enabled": True, "visits": 2, "days": 7})
    hub.smart.update_settings({"faces": {"no_face": "quiet"}})
    visit(hub, w, engine, body, 100, bodies=[vec(4, noise=0.012, seed=1)])
    assert not drain(q, "stranger")              # quiet when no face, as set
    visit(hub, w, engine, body, 1000, bodies=[vec(4, noise=0.012, seed=2)])
    got = drain(q, "returning_stranger")         # but a returning stranger still alerts
    assert len(got) == 1 and got[0]["unconfirmed"] is True and "build" in got[0]["message"]


def test_only_people_on_your_property_are_remembered(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path, bodies={"enabled": True}, visitors={"enabled": True})
    hub.smart.update_camera("front", {"property": [[0, 0.5], [0.4, 0.5], [0.4, 1], [0, 1]]})
    bob = np.zeros(128, np.float32)
    bob[2] = 1
    # Out on the pavement (right of the picture): still a stranger alert, but not remembered.
    visit(hub, w, engine, body, 100, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.8)
    assert drain(q, "stranger") and hub.smart.state()["visitors"] == []
    # On the drive: remembered.
    visit(hub, w, engine, body, 1000, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.2)
    assert len(hub.smart.state()["visitors"]) == 1
    # In a privacy area: never used, even on the property.
    hub.cameras["front"].cfg["privacy"] = {"masks": [{"points": [[0, 0], [0.3, 0], [0.3, 1], [0, 1]]}]}
    carol = np.zeros(128, np.float32)
    carol[9] = 1
    visit(hub, w, engine, body, 2000, faces=[[face(carol)], [face(carol)]], bodies=[vec(8), vec(8)], x=0.2)
    assert len(hub.smart.state()["visitors"]) == 1


def test_visitors_are_forgotten_and_can_be_named(tmp_path):
    hub, w, q, engine, body, _ = setup(tmp_path, bodies={"enabled": True}, visitors={"enabled": True})
    bob = np.zeros(128, np.float32)
    bob[2] = 1
    visit(hub, w, engine, body, 100, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)])
    visit(hub, w, engine, body, 1000, faces=[[face(np.eye(128)[9])], [face(np.eye(128)[9])]])
    store = hub.smart.visitors
    assert len(store.visitors) == 2
    # Saying who one is moves their face and build to that person, and they're family from then on.
    store.make_family(store.visitors[-1]["id"], name="Bob")
    bob_p = hub.smart.store.known["people"][0]
    assert bob_p["name"] == "Bob" and bob_p["faces"] and bob_p["bodies"]
    assert hub.smart.store.match_face(bob, 0.4)[0]["name"] == "Bob"
    # Visitors not seen for a long time are forgotten.
    store.visitors[0]["last"] = (datetime.datetime.now() - datetime.timedelta(days=40)).isoformat()
    assert store.forget_old(30) == 1 and store.visitors == []


def test_settings_and_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    hub, w, q, engine, body, path = setup(tmp_path)
    client = TestClient(create_app(hub))
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200
    st = client.put("/api/smart/settings", json={
        "bodies": {"enabled": True, "strictness": "strict"},
        "visitors": {"enabled": True, "visits": 99, "days": 14, "forget_days": 3}}).json()
    assert st["settings"]["bodies"] == {"enabled": True, "strictness": "strict"}
    assert st["settings"]["visitors"]["visits"] == 20 and st["settings"]["visitors"]["forget_days"] == 14
    assert json.loads(path.read_text())["smart"]["visitors"]["days"] == 14
    assert st["bodies_available"] is True and st["visitors"] == []
    r = client.put("/api/smart/cameras/front", json={"property": [[0, 0], [1, 0], [1, 1]]}).json()
    assert r["property"] == [[0, 0], [1, 0], [1, 1]]
    hub.smart.update_camera("front", {})

    p = hub.smart.store.add_person("Alice")
    b = hub.smart.store.add_body(p["id"], {"feature": vec(0), "crop": CROP})
    assert hub.smart.store.add_body(p["id"], {"feature": vec(0), "crop": CROP}) is None  # the same again
    st = client.get("/api/smart").json()
    assert st["people"][0]["bodies"][0]["id"] == b["id"]
    assert client.delete(f"/api/smart/people/{p['id']}/bodies/{b['id']}").json()["people"][0]["bodies"] == []

    hub.smart.update_settings({"visitors": {"enabled": True}})
    bob = np.zeros(128, np.float32)
    bob[2] = 1
    visit(hub, w, engine, body, 100, faces=[[face(bob)], [face(bob)]])
    vid = client.get("/api/smart").json()["visitors"][0]["id"]
    st = client.put(f"/api/smart/visitors/{vid}", json={"label": "  Window  cleaner ", "regular": True}).json()
    assert st["visitors"][0]["label"] == "Window cleaner" and st["visitors"][0]["regular"]
    st = client.post(f"/api/smart/visitors/{vid}/family", json={"id": p["id"]}).json()
    assert st["visitors"] == [] and len(st["people"][0]["faces"]) == 2
    assert client.delete(f"/api/smart/visitors/{vid}").status_code == 404


@pytest.mark.skipif(not os.path.exists(bodymod.BODY_MODEL), reason="body model not downloaded")
def test_real_body_model():
    eng = bodymod.BodyEngine()
    assert eng.available
    rng = np.random.default_rng(0)
    # A made-up person: dark legs, a red top, a head, on a plain background.
    img = np.full((720, 1280, 3), 140, np.uint8)
    cv2.rectangle(img, (600, 300), (680, 470), (40, 40, 200), -1)
    cv2.rectangle(img, (610, 470), (670, 640), (40, 30, 30), -1)
    cv2.circle(img, (640, 270), 28, (150, 170, 200), -1)
    box = (560 / 1280, 230 / 720, 720 / 1280, 650 / 720)
    a = eng.describe(img, box)
    assert a and a["feature"].shape == (768,) and a["crop"]
    noisy = np.clip(img.astype(np.int16) + rng.integers(-12, 12, img.shape), 0, 255).astype(np.uint8)
    b = eng.describe(noisy, box)
    other = img.copy()
    cv2.rectangle(other, (600, 300), (680, 470), (200, 160, 40), -1)  # a blue top instead
    c = eng.describe(other, box)
    same = bodymod.likeness(b, [a])
    assert same > bodymod.BODY_MATCH["normal"]
    assert bodymod.likeness(c, [a]) < same
    assert eng.describe(img, (0.5, 0.5, 0.52, 0.55)) is None   # too small to tell
