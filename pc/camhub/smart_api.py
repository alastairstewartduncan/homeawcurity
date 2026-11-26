"""Web routes for smart alerts: family faces, your cars, parcels, lines and loitering."""
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
        if not run(smart.store.rename, "cars", cid, (await body_of(request)).get("name")):
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
