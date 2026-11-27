"""The sightings log: people and number plates seen on your property, and searching them.
Run:  python -m pytest tests"""
import base64
import datetime
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from camhub import faces as facemod, plates as platemod  # noqa: E402
from test_bodies import setup, vec, visit  # noqa: E402
from test_smart import FRAME, colour_frame, drain, face, vehicle  # noqa: E402

LEFT = [[0, 0.5], [0.45, 0.5], [0.45, 1], [0, 1]]   # the drive, on the left of the picture


class FakePlates:
    available = True

    def __init__(self):
        self.next = []

    def read(self, img):
        if not self.next:
            return None
        plate, sure = self.next.pop(0)
        if plate is None:
            return None
        return {"plate": plate, "sure": sure, "box": (2, 2, 30, 12)}


def onehot(i, n=128):
    v = np.zeros(n, np.float32)
    v[i] = 1
    return v


def make(tmp_path, **settings):
    s = {"log": {"people": True, "plates": True}}
    s.update(settings)
    hub, w, q, engine, body, path = setup(tmp_path, **s)
    hub.smart.plates = FakePlates()
    return hub, w, q, engine, body, path


# ---- number plates as text -------------------------------------------------------------

def test_plate_shapes_and_misreads():
    assert platemod.as_plate("AB12CDE")[0] == "AB12CDE"
    assert platemod.as_plate("ab12 cde")[0] == "AB12CDE"
    assert platemod.as_plate("A812CDE")[0] == "AB12CDE"      # 8 where a letter goes is a B
    assert platemod.as_plate("LOOSZZT")[0] == "LO05ZZT"      # O and S where numbers go
    assert platemod.as_plate("Y123BCD")[0] == "Y123BCD"
    assert platemod.as_plate("FORD")[0] is None
    assert platemod.as_plate("TAXI")[0] is None
    assert platemod.as_plate("0800123456")[0] is None         # a phone number on a van
    assert platemod.pretty("AB12CDE") == "AB12 CDE" and platemod.pretty("Y123BCD") == "Y123 BCD"
    assert platemod.similar("AB12CDE", "AB12CDE") == 1.0
    assert platemod.similar("AB12CDE", "AB12CDF") > 0.85
    assert platemod.similar("AB12CDE", "XY99ZZZ") < 0.3


# ---- people ----------------------------------------------------------------------------

def test_nothing_is_logged_until_the_property_is_drawn(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path)
    bob = onehot(2)
    visit(hub, w, engine, body, 100, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.2)
    assert hub.smart.log.stats()["entries"] == 0


def test_people_on_the_property_are_logged_and_grouped(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path)
    hub.smart.update_camera("front", {"property": LEFT})
    log = hub.smart.log
    bob, carol = onehot(2), onehot(9)

    # Out on the pavement: not logged.
    visit(hub, w, engine, body, 100, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.8)
    assert log.stats()["entries"] == 0
    # On the drive: logged, with their face as the picture.
    visit(hub, w, engine, body, 1000, faces=[[face(bob)], [face(bob, size=120)]], bodies=[vec(1), vec(1)], x=0.2)
    entries = log.search("person")
    assert len(entries) == 1 and entries[0]["face"] and entries[0]["pic"] and entries[0]["name"].startswith("Person")
    # He comes back: the same person, seen twice.
    visit(hub, w, engine, body, 2000, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.2)
    who = log.who("person")
    assert len(who) == 1 and who[0]["count"] == 2
    # Someone else: a new person.
    visit(hub, w, engine, body, 3000, faces=[[face(carol)], [face(carol)]], bodies=[vec(8), vec(8)], x=0.2)
    who = log.who("person")
    assert len(who) == 2 and who[0]["count"] == 1
    # Seen from behind only, same build and clothes as Bob: put with Bob.
    visit(hub, w, engine, body, 4000, bodies=[vec(1, noise=0.01, seed=3), vec(1, noise=0.01, seed=4)], x=0.2)
    bob_group = next(g for g in log.who("person") if g["count"] > 1)
    assert bob_group["count"] == 3
    # Every recording he's in, and naming him so he can be searched.
    assert len(log.search(group=bob_group["group"])) == 3
    log.rename_group(bob_group["group"], "Postman")
    assert [g["name"] for g in log.who(q="postman")] == ["Postman"]
    assert len(log.search(q="postman")) == 3
    # In a privacy area: never logged.
    hub.cameras["front"].cfg["privacy"] = {"masks": [{"points": [[0, 0], [0.3, 0], [0.3, 1], [0, 1]]}]}
    visit(hub, w, engine, body, 5000, faces=[[face(onehot(20))], [face(onehot(20))]], x=0.2)
    assert log.stats()["people"] == 4


def test_family_are_logged_by_name(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path)
    hub.smart.update_camera("front", {"property": LEFT})
    alice = onehot(0)
    p = hub.smart.store.add_person("Alice")
    hub.smart.store.add_face(p["id"], alice, facemod.jpeg(FRAME[:20, :20]))
    visit(hub, w, engine, body, 100, faces=[[face(alice)]], bodies=[vec(0)], x=0.2)
    e = hub.smart.log.search("person")
    assert len(e) == 1 and e[0]["name"] == "Alice" and e[0]["known"] and e[0]["by"] == "face"
    assert hub.smart.log.who(q="alice")[0]["family"]


def test_logging_off_or_without_faces_logs_nothing(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path, log={"people": False, "plates": False})
    hub.smart.update_camera("front", {"property": LEFT})
    visit(hub, w, engine, body, 100, faces=[[face(onehot(3))], [face(onehot(3))]], x=0.2)
    assert hub.smart.log.stats()["entries"] == 0
    hub.smart.update_settings({"log": {"people": True}, "faces": {"enabled": False}})
    visit(hub, w, engine, body, 1000, faces=[[face(onehot(3))], [face(onehot(3))]], x=0.2)
    assert hub.smart.log.stats()["entries"] == 0


def test_old_entries_are_deleted_with_their_pictures(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path)
    hub.smart.update_camera("front", {"property": LEFT})
    log = hub.smart.log
    visit(hub, w, engine, body, 100, faces=[[face(onehot(3))], [face(onehot(3))]], x=0.2)
    visit(hub, w, engine, body, 1000, faces=[[face(onehot(4))], [face(onehot(4))]], x=0.2)
    old = log.search()[1]
    old_time = (datetime.datetime.now() - datetime.timedelta(days=40)).isoformat(timespec="seconds")
    log.db.execute("UPDATE sightings SET time = ? WHERE id = ?", (old_time, old["id"]))
    log.db.commit()
    assert hub.smart.store.pic_path(old["pic"])
    assert log.tidy(30) == 1
    assert hub.smart.store.pic_path(old["pic"]) is None
    assert len(log.search()) == 1 and len(log.who()) == 1
    # Choosing fewer days tidies straight away.
    log.db.execute("UPDATE sightings SET time = ?", ((datetime.datetime.now() - datetime.timedelta(days=10)).isoformat(),))
    log.db.commit()
    hub.smart.update_settings({"log": {"days": 7}})
    assert log.stats()["entries"] == 0 and hub.smart.settings["log"]["days"] == 7


# ---- vehicles and number plates --------------------------------------------------------

def stop(w, t, box, colour=(30, 30, 200), looks=4):
    f = colour_frame(colour, box)
    for i in range(looks):
        w.on_frame(f, t + i, i == 0, [vehicle(box)], False)


def leave(w, t):
    for i in range(0, 14, 2):
        w.on_frame(FRAME, t + i, True, [], False)


def test_number_plates_are_read_logged_and_searchable(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path)
    hub.smart.update_camera("front", {"property": LEFT})
    log = hub.smart.log
    plates = hub.smart.plates
    drive = (0.05, 0.6, 0.35, 0.95)

    # Logging plates alone doesn't send vehicle alerts.
    w.on_frame(FRAME, 1, False, None, False)
    for t in (2, 3, 4):
        w.on_frame(FRAME, t, False, [], False)      # nothing parked to start with
    plates.next = [(None, 0), ("AB12CDE", 0.6), ("AB12CDE", 0.93)]
    stop(w, 10, drive, looks=4)
    # Still standing there: looked at again until the plate is clear.
    f = colour_frame((30, 30, 200), drive)
    for t in range(14, 30, 2):
        w.on_frame(f, t, False, [vehicle(drive)], False)
    assert not drain(q, "vehicle")
    e = log.search("vehicle")
    assert len(e) == 1 and e[0]["plate"] == "AB12 CDE" and e[0]["plate_sure"] == 0.93 and e[0]["plate_pic"]
    assert plates.next == []

    # It leaves and comes back later, read with one character wrong: still the same car.
    leave(w, 40)
    plates.next = [("AB12CDF", 0.9)]
    stop(w, 1000, drive)
    who = log.who("vehicle")
    assert len(who) == 1 and who[0]["count"] == 2 and who[0]["plate"] == "AB12 CDE"
    # Search a plate, even with a character wrong or only part of it.
    assert len(log.search(q="AB12 CDE")) == 2
    assert len(log.search(q="ab12cdx")) == 2
    assert len(log.search(q="12CD")) == 2
    assert log.search(q="XY99ZZZ") == []

    # A vehicle stopping out in the road isn't logged.
    leave(w, 1100)
    plates.next = [("XY99ZZZ", 0.95)]
    stop(w, 2200, (0.6, 0.6, 0.9, 0.95))
    assert len(log.search("vehicle")) == 2


def test_your_cars_are_known_by_their_plate(tmp_path):
    hub, w, q, engine, body, _ = make(tmp_path)
    hub.smart.update_camera("front", {"property": LEFT})
    car = hub.smart.store.add_car("Golf")
    hub.smart.store.set_plate(car["id"], "kx69 opl")
    assert hub.smart.store.public()["cars"][0]["plate"] == "KX69OPL"
    for t in (1, 2, 3, 4):
        w.on_frame(FRAME, t, False, [] if t > 1 else None, False)
    hub.smart.plates.next = [("KX69OPL", 0.95)]
    stop(w, 10, (0.05, 0.6, 0.35, 0.95))
    e = hub.smart.log.search("vehicle")[0]
    assert e["name"] == "Golf" and e["known"]


# ---- the viewer ------------------------------------------------------------------------

def test_log_api(tmp_path):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    hub, w, q, engine, body, path = make(tmp_path, log={"people": False, "plates": False})
    client = TestClient(create_app(hub))
    assert client.get("/api/smart/log/who").status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200
    st = client.put("/api/smart/settings", json={"log": {"people": True, "plates": True, "days": 999}}).json()
    assert st["settings"]["log"] == {"people": True, "plates": True, "days": 365}
    assert "log_stats" in st and "plates_available" in st
    client.put("/api/smart/cameras/front", json={"property": LEFT})

    bob = onehot(5)
    visit(hub, w, engine, body, 100, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.2)
    visit(hub, w, engine, body, 1000, faces=[[face(bob)], [face(bob)]], bodies=[vec(1), vec(1)], x=0.2)
    who = client.get("/api/smart/log/who?kind=person").json()["who"]
    assert len(who) == 1 and who[0]["count"] == 2
    grp = who[0]["group"]
    entries = client.get("/api/smart/log/entries", params={"group": grp}).json()["entries"]
    assert len(entries) == 2
    today = datetime.date.today().isoformat()
    assert len(client.get("/api/smart/log/entries", params={"day_from": today, "day_to": today}).json()["entries"]) == 2
    assert client.get("/api/smart/log/entries", params={"day_to": "2020-01-01"}).json()["entries"] == []

    # Search with a photo of him.
    photo = "data:image/jpeg;base64," + base64.b64encode(facemod.jpeg(FRAME)).decode()
    engine.next = [[face(bob)]]
    r = client.post("/api/smart/log/photo", json={"photo": photo}).json()
    assert r["found"] == "face" and r["matches"][0]["group"] == grp and r["matches"][0]["count"] == 2
    engine.next = [[face(onehot(77))]]
    assert client.post("/api/smart/log/photo", json={"photo": photo}).json()["matches"] == []
    assert client.post("/api/smart/log/photo", json={"photo": "nope"}).status_code == 400

    # Naming, deleting one entry, deleting everything about him, and clearing the log.
    assert client.put(f"/api/smart/log/groups/{grp}", json={"name": "Window cleaner"}).json()["name"] == "Window cleaner"
    assert client.get("/api/smart/log/who", params={"q": "window"}).json()["who"][0]["group"] == grp
    assert client.delete(f"/api/smart/log/entries/{entries[0]['id']}").json()["deleted"] == 1
    assert client.delete("/api/smart/log/entries/nope").status_code == 404
    assert client.delete(f"/api/smart/log/groups/{grp}").json()["deleted"] == 1
    assert client.get("/api/smart/log/who").json()["who"] == []
    visit(hub, w, engine, body, 2000, faces=[[face(bob)], [face(bob)]], x=0.2)
    assert client.delete("/api/smart/log").json()["deleted"] == 1

    # Your car's plate.
    car = hub.smart.store.add_car("Golf")
    st = client.put(f"/api/smart/cars/{car['id']}", json={"plate": "ab12 cde"}).json()
    assert st["cars"][0]["plate"] == "AB12CDE" and st["cars"][0]["name"] == "Golf"


# ---- the real plate models -------------------------------------------------------------

@pytest.mark.skipif(not (os.path.exists(platemod.TEXT_DETECT) and os.path.exists(platemod.TEXT_READ)),
                    reason="number plate models not downloaded")
def test_real_plate_reader_reads_a_plate():
    import cv2
    car = np.full((600, 900, 3), (60, 60, 140), np.uint8)
    cv2.rectangle(car, (100, 80), (800, 300), (40, 40, 40), -1)
    cv2.rectangle(car, (260, 420), (640, 500), (0, 210, 255), -1)       # a yellow rear plate
    cv2.putText(car, "AB12 CDE", (275, 482), cv2.FONT_HERSHEY_DUPLEX, 2.2, (0, 0, 0), 5)
    reader = platemod.PlateReader()
    assert reader.available
    found = reader.read(car)
    assert found and found["plate"] == "AB12CDE"
    assert reader.read(np.full((300, 400, 3), 128, np.uint8)) is None
