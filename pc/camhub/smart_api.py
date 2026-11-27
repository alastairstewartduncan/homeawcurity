"""Web routes for smart alerts: family faces and bodies, visitors, your cars, parcels, lines and loitering."""
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse


def add_smart_routes(app, hub):
    smart = hub.smart

    async def body_of(request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        return body

    def run(fn, *a):
        try:
            return fn(*a)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0]) if e.args else "Not found")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/smart")
    def get_smart():
        return smart.state()

    @app.put("/api/smart/settings")
    async def put_settings(request: Request):
        smart.update_settings(await body_of(request))
        return smart.state()

    @app.put("/api/smart/cameras/{cam_id}")
    async def put_camera(cam_id: str, request: Request):
        if cam_id not in hub.cameras:
            raise HTTPException(404, "No such camera")
        return smart.update_camera(cam_id, await body_of(request))

    @app.post("/api/smart/cameras/{cam_id}/parcel/clear")
    def clear_parcel(cam_id: str):
        w = smart.watchers.get(cam_id)
        if w is None:
            raise HTTPException(404, "No such camera")
        w.clear_parcel()
        return w.parcel_status()

    # ---- people ----

    @app.post("/api/smart/people")
    async def add_person(request: Request):
        body = await body_of(request)
        p = run(smart.store.add_person, body.get("name"))
        if body.get("photo"):
            try:
                smart.add_photo(p["id"], body["photo"])
            except (KeyError, ValueError) as e:
                smart.store.remove("people", p["id"])
                raise HTTPException(400, str(e))
        return smart.state()

    @app.put("/api/smart/people/{pid}")
    async def rename_person(pid: str, request: Request):
        if not run(smart.store.rename, "people", pid, (await body_of(request)).get("name")):
            raise HTTPException(404, "Not found")
        return smart.state()

    @app.delete("/api/smart/people/{pid}")
    def delete_person(pid: str):
        if not smart.store.remove("people", pid):
            raise HTTPException(404, "Not found")
        smart.questions.person_removed(pid)
        return smart.state()

    # ---- "Is this ...?" questions (learning.py) ----

    @app.get("/api/smart/questions")
    def get_questions():
        return {"questions": smart.questions.public()}

    @app.post("/api/smart/questions/{qid}")
    async def answer_question(qid: str, request: Request):
        """Body: {"answer": "yes" | "no" | "other", "id": person (for other), "name": new person}."""
        body = await body_of(request)
        said = run(smart.questions.answer, qid, str(body.get("answer") or ""), body.get("id"), body.get("name"))
        return {**smart.state(), "said": said}

    @app.delete("/api/smart/questions/{qid}")
    def forget_question(qid: str):
        if not smart.questions.forget(qid):
            raise HTTPException(404, "That question has already been answered")
        return smart.state()

    @app.post("/api/smart/people/{pid}/photos")
    async def add_photo(pid: str, request: Request):
        run(smart.add_photo, pid, (await body_of(request)).get("photo"))
        return smart.state()

    @app.delete("/api/smart/people/{pid}/photos/{fid}")
    def delete_photo(pid: str, fid: str):
        if not smart.store.remove_sample("people", pid, fid):
            raise HTTPException(404, "Not found")
        return smart.state()

    @app.delete("/api/smart/people/{pid}/bodies/{bid}")
    def delete_body(pid: str, bid: str):
        if not smart.store.remove_sample("people", pid, bid, key="bodies"):
            raise HTTPException(404, "Not found")
        return smart.state()

    # ---- visitors: people you haven't named who keep coming back ----

    @app.put("/api/smart/visitors/{vid}")
    async def update_visitor(vid: str, request: Request):
        body = await body_of(request)
        run(smart.visitors.update, vid, body.get("label"), body.get("regular"))
        return smart.state()

    @app.post("/api/smart/visitors/{vid}/family")
    async def visitor_is_family(vid: str, request: Request):
        body = await body_of(request)
        run(smart.visitors.make_family, vid, body.get("id"), body.get("name"))
        return smart.state()

    @app.delete("/api/smart/visitors/{vid}")
    def forget_visitor(vid: str):
        if not smart.visitors.forget(vid):
            raise HTTPException(404, "Not found")
        return smart.state()

    # ---- cars ----

    @app.post("/api/smart/cars")
    async def add_car(request: Request):
        body = await body_of(request)
        c = run(smart.store.add_car, body.get("name"))
        if body.get("camera"):
            try:
                smart.add_car_from_live(c["id"], body["camera"])
            except (KeyError, ValueError) as e:
                smart.store.remove("cars", c["id"])
                raise HTTPException(400, str(e))
        return smart.state()

    @app.put("/api/smart/cars/{cid}")
    async def rename_car(cid: str, request: Request):
        body = await body_of(request)
        if "name" in body and not run(smart.store.rename, "cars", cid, body.get("name")):
            raise HTTPException(404, "Not found")
        if "plate" in body and not run(smart.store.set_plate, cid, body.get("plate")):
            raise HTTPException(404, "Not found")
        return smart.state()

    @app.delete("/api/smart/cars/{cid}")
    def delete_car(cid: str):
        if not smart.store.remove("cars", cid):
            raise HTTPException(404, "Not found")
        return smart.state()

    @app.post("/api/smart/cars/{cid}/from_live")
    async def car_from_live(cid: str, request: Request):
        run(smart.add_car_from_live, cid, (await body_of(request)).get("camera"))
        return smart.state()

    @app.delete("/api/smart/cars/{cid}/samples/{sid}")
    def delete_sample(cid: str, sid: str):
        if not smart.store.remove_sample("cars", cid, sid):
            raise HTTPException(404, "Not found")
        return smart.state()

    # ---- seen lately ----

    @app.post("/api/smart/seen/{seen_id}/assign")
    async def assign_seen(seen_id: str, request: Request):
        body = await body_of(request)
        run(smart.store.assign_seen, seen_id, body.get("id"), body.get("name"))
        return smart.state()

    @app.delete("/api/smart/seen/{seen_id}")
    def forget_seen(seen_id: str):
        if not smart.store.forget_seen(seen_id):
            raise HTTPException(404, "Not found")
        return smart.state()

    @app.get("/api/smart/pics/{name}")
    def picture(name: str):
        p = smart.store.pic_path(name)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ---- the sightings log: people and number plates seen, and searching them ----

    add_log_routes(app, hub)


def add_log_routes(app, hub):
    import os

    from . import bodies as bodymod
    from . import faces as facemod

    smart = hub.smart
    slog = smart.log

    def clip_exists(clip_id):
        base = hub.storage.clip_base(clip_id)
        return bool(base) and os.path.isfile(base + ".mp4")

    @app.get("/api/smart/log/who")
    def log_who(kind: str = "", q: str = ""):
        return {"who": slog.who(kind or None, q or None), "stats": slog.stats()}

    @app.get("/api/smart/log/entries")
    def log_entries(kind: str = "", q: str = "", group: str = "", day_from: str = "", day_to: str = "",
                    camera: str = "", limit: int = 200):
        return {"entries": slog.search(kind or None, q or None, group or None, day_from or None, day_to or None,
                                       camera or None, max(1, min(1000, limit)), clip_exists)}

    @app.post("/api/smart/log/photo")
    async def log_photo(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        img = facemod.decode_image((body or {}).get("photo") if isinstance(body, dict) else None)
        if img is None:
            raise HTTPException(400, "That file isn't a picture this PC can read")
        face = smart.engine.largest_face(img) if smart.engine.available else None
        if face is not None and face["size"] >= facemod.MIN_FACE_PX * 0.8:
            found, matches = "face", slog.by_photo(face=face["feature"])
        else:
            found, matches = None, []
            if smart.body_engine.available and getattr(hub.detector, "available", False):
                people = [o for o in hub.detector.detect_objects(img, 0.5, 0.35) if o[0] == "person"]
                if people:
                    _, _, box = max(people, key=lambda o: (o[2][2] - o[2][0]) * (o[2][3] - o[2][1]))
                    obs = smart.body_engine.describe(img, box)
                    if obs is not None:
                        found, matches = "body", slog.by_photo(body=obs["feature"])
            if found is None:
                h, w = img.shape[:2]
                obs = smart.body_engine.describe(img, (0, 0, 1, 1)) if smart.body_engine.available and \
                    h >= bodymod.MIN_BODY_PX and h > w else None
                if obs is not None:
                    found, matches = "body", slog.by_photo(body=obs["feature"])
        if found is None:
            raise HTTPException(400, "No face or person could be found in that picture. Try one where they're larger.")
        who = {g["group"]: g for g in slog.who("person")}
        out = []
        for m in matches:
            if m["group"] in who:
                out.append(dict(who[m["group"]], likeness=m["likeness"], by=m["by"]))
        return {"found": found, "matches": out}

    @app.put("/api/smart/log/groups/{grp}")
    async def log_rename(grp: str, request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        try:
            return {"name": slog.rename_group(grp, (body or {}).get("name"))}
        except KeyError:
            raise HTTPException(404, "Not found")

    @app.delete("/api/smart/log/groups/{grp}")
    def log_forget_group(grp: str):
        return {"deleted": slog.delete_group(grp)}

    @app.delete("/api/smart/log/entries/{sid}")
    def log_delete(sid: str):
        if not slog.delete(sid):
            raise HTTPException(404, "Not found")
        return {"deleted": 1}

    @app.delete("/api/smart/log")
    def log_clear():
        return {"deleted": slog.clear()}
