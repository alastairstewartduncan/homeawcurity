"""The web server behind the PC viewer and the Android app."""
import asyncio
import json
import logging
import os
import queue
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import BUILT, COMMIT, __version__, auth

log = logging.getLogger("camhub.server")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "camhub_token"

EDITABLE_MOTION = {
    "sensitivity": (int, 1, 10),
    "min_area_percent": (float, 0.05, 50),
    "person_only": (bool, None, None),
    "person_confidence": (float, 0.2, 0.95),
    "pre_seconds": (int, 0, 30),
    "post_seconds": (int, 3, 120),
    "max_clip_minutes": (int, 1, 60),
}


def create_app(hub):
    app = FastAPI(title="Home Camera", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = auth.LoginLimiter()
    config = hub.config

    def token_of(request):
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return request.cookies.get(COOKIE)

    def authed(request):
        return auth.check_token(config.server["secret"], token_of(request) or "")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        public = path in ("/login", "/api/login", "/favicon.svg", "/manifest.json") or path.startswith("/static/")
        if not public and not authed(request):
            if path.startswith("/api/"):
                return JSONResponse({"error": "login required"}, status_code=401)
            return RedirectResponse("/login")
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # ---- pages ---------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/login")
    def login_page():
        return FileResponse(os.path.join(STATIC, "login.html"), headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.svg")
    def favicon():
        return FileResponse(os.path.join(STATIC, "favicon.svg"))

    @app.get("/static/{name}")
    def static(name: str):
        p = os.path.join(STATIC, os.path.basename(name))
        if not os.path.isfile(p):
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    # ---- login ---------------------------------------------------------------

    @app.post("/api/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            return JSONResponse({"error": "Too many attempts. Try again in 15 minutes."}, status_code=429)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        password = str(body.get("password", ""))
        stored = config.server.get("password_hash")
        if not stored or not auth.check_password(password, stored):
            limiter.failed(ip)
            time.sleep(0.5)
            return JSONResponse({"error": "Wrong password"}, status_code=401)
        limiter.succeeded(ip)
        token = auth.make_token(config.server["secret"])
        resp = JSONResponse({"token": token})
        resp.set_cookie(COOKIE, token, max_age=365 * 86400, httponly=True, samesite="strict",
                        secure=bool(config.server.get("https_cert")))
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    # ---- status and cameras --------------------------------------------------

    @app.get("/api/status")
    def status():
        return {
            "person_detection": hub.detector.available,
            "cameras": [c.status() for c in hub.cameras.values()],
            "version": {"version": __version__, "commit": COMMIT, "built": BUILT},
            "storage": hub.storage.usage(),
            "retention_days": config.storage.get("retention_days"),
            "min_free_gb": config.storage.get("min_free_gb"),
        }

    def cam_or_404(cam_id):
        cam = hub.cameras.get(cam_id)
        if not cam:
            raise HTTPException(404, "No such camera")
        return cam

    @app.get("/api/cameras/{cam_id}/snapshot.jpg")
    def snapshot(cam_id: str):
        data, _ = cam_or_404(cam_id).latest_jpeg(90)
        if data is None:
            raise HTTPException(503, "No picture yet")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/cameras/{cam_id}/live.mjpg")
    async def live(cam_id: str, request: Request):
        cam = cam_or_404(cam_id)

        async def frames():
            last = 0
            while not await request.is_disconnected():
                data, fid = await asyncio.to_thread(cam.latest_jpeg)
                if data is not None and fid != last:
                    last = fid
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                await asyncio.sleep(1.0 / cam.LIVE_FPS)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.post("/api/cameras/{cam_id}/ptz")
    async def ptz(cam_id: str, request: Request):
        cam = cam_or_404(cam_id)
        if not cam.ptz or not cam.ptz.available:
            # Try once more, in case the camera was offline at start-up.
            if not cam.ptz or not await asyncio.to_thread(cam.ptz.connect):
                raise HTTPException(409, cam.ptz.error if cam.ptz else "Pan and tilt not set up")
        body = await request.json()
        cam.ptz_moved(manual=True)
        try:
            if body.get("action") == "stop":
                await asyncio.to_thread(cam.ptz.stop)
            elif body.get("action") == "home":
                await asyncio.to_thread(cam.ptz.home)
            elif body.get("action") == "set_home":
                await asyncio.to_thread(cam.ptz.set_home)
            else:
                await asyncio.to_thread(cam.ptz.move, body.get("pan", 0), body.get("tilt", 0), body.get("zoom", 0))
        except Exception as e:
            raise HTTPException(502, f"Camera refused: {e}")
        return {"ok": True}

    @app.get("/api/cameras/{cam_id}/listen")
    async def listen(cam_id: str, request: Request):
        """The camera's sound as raw 16-bit mono samples (rate in X-Sample-Rate)."""
        from .audio import RATE

        cam = cam_or_404(cam_id)
        q = cam.audio.subscribe()
        try:
            first = await asyncio.to_thread(q.get, True, 10)
        except queue.Empty:
            first = None
        if first is None:
            cam.audio.unsubscribe(q)
            raise HTTPException(409, cam.audio.problem or "No sound came from the camera.")

        async def sound():
            try:
                yield first
                while not await request.is_disconnected():
                    try:
                        data = await asyncio.to_thread(q.get, True, 2)
                    except queue.Empty:
                        continue
                    if data is None:
                        break
                    yield data
            finally:
                cam.audio.unsubscribe(q)

        return StreamingResponse(sound(), media_type="application/octet-stream",
                                 headers={"Cache-Control": "no-store", "X-Sample-Rate": str(RATE),
                                          "X-Accel-Buffering": "no"})

    def ptz_ready(cam):
        if not cam.ptz or not cam.ptz.available:
            raise HTTPException(409, (cam.ptz.error if cam.ptz else None) or "Turning isn't set up for this camera")

    @app.post("/api/cameras/{cam_id}/presets")
    async def save_preset(cam_id: str, request: Request):
        """Saves where the camera points now. Body: {"name"}."""
        cam = cam_or_404(cam_id)
        ptz_ready(cam)
        name = str((await request.json()).get("name") or "").strip()[:30]
        if not name:
            raise HTTPException(400, "Give the position a name, for example Gate")
        presets = cam.ptz_cfg.setdefault("presets", [])
        same = next((p for p in presets if p["name"].lower() == name.lower()), None)
        taken = {p["token"] for p in presets if p is not same}
        try:
            token = await asyncio.to_thread(cam.ptz.save_preset, name, taken)
        except Exception as e:
            raise HTTPException(502, f"The camera didn't save it: {e}")
        if same:
            same["token"] = token
        else:
            if len(presets) >= 20:
                raise HTTPException(400, "Up to 20 positions can be saved")
            presets.append({"name": name, "token": token})
        config.save()
        return {"presets": presets}

    @app.post("/api/cameras/{cam_id}/presets/{token}/go")
    async def goto_preset(cam_id: str, token: str):
        cam = cam_or_404(cam_id)
        ptz_ready(cam)
        if not any(p["token"] == token for p in cam.ptz_cfg.get("presets", [])):
            raise HTTPException(404, "No such position")
        cam.ptz_moved(manual=True)
        try:
            await asyncio.to_thread(cam.ptz.goto_preset, token)
        except Exception as e:
            raise HTTPException(502, f"Camera refused: {e}")
        return {"ok": True}

    @app.delete("/api/cameras/{cam_id}/presets/{token}")
    async def delete_preset(cam_id: str, token: str):
        cam = cam_or_404(cam_id)
        presets = cam.ptz_cfg.get("presets", [])
        if not any(p["token"] == token for p in presets):
            raise HTTPException(404, "No such position")
        cam.ptz_cfg["presets"] = [p for p in presets if p["token"] != token]
        config.save()
        if cam.ptz and cam.ptz.available:
            try:
                await asyncio.to_thread(cam.ptz.remove_preset, token)
            except Exception:
                pass   # forgetting it here is what matters; some cameras won't clear fixed slots
        return {"presets": cam.ptz_cfg["presets"]}

    @app.put("/api/cameras/{cam_id}/patrol")
    async def put_patrol(cam_id: str, request: Request):
        """Body: {"enabled", "dwell_seconds", "follow_motion"}, all optional."""
        cam = cam_or_404(cam_id)
        body = await request.json()
        p = cam.ptz_cfg
        patrol = p.setdefault("patrol", {"enabled": False, "dwell_seconds": 30})
        if "enabled" in body:
            patrol["enabled"] = bool(body["enabled"])
            cam._patrol_pause_until = 0.0
        if "dwell_seconds" in body:
            try:
                patrol["dwell_seconds"] = max(10, min(3600, int(body["dwell_seconds"])))
            except (TypeError, ValueError):
                raise HTTPException(400, "Use a whole number of seconds")
        if "follow_motion" in body:
            p["follow_motion"] = bool(body["follow_motion"])
        config.save()
        return {"patrol": patrol, "follow_motion": p.get("follow_motion", False)}

    @app.post("/api/cameras/{cam_id}/talk")
    async def talk(cam_id: str, request: Request):
        """A short piece of 8 kHz 16-bit mono sound to play through the camera's speaker."""
        cam = cam_or_404(cam_id)
        data = await request.body()
        if len(data) > 64000:
            raise HTTPException(413, "Too much sound in one go")
        if cam.talker.supported is False:
            raise HTTPException(409, cam.talker.problem or "This camera can't play sound from Home Camera.")
        try:
            await asyncio.to_thread(cam.talker.put, data)
        except Exception as e:
            raise HTTPException(409, f"Couldn't talk through the camera: {e}")
        return {"ok": True}

    @app.get("/api/cameras/{cam_id}/settings")
    def get_settings(cam_id: str):
        cam = cam_or_404(cam_id)
        return {"name": cam.cfg.get("name"), "motion": cam.motion}

    @app.put("/api/cameras/{cam_id}/settings")
    async def put_settings(cam_id: str, request: Request):
        cam = cam_or_404(cam_id)
        body = await request.json()
        motion = body.get("motion", {})
        new = dict(cam.motion)
        for k, (typ, lo, hi) in EDITABLE_MOTION.items():
            if k in motion:
                try:
                    v = typ(motion[k])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"Bad value for {k}")
                if lo is not None:
                    v = max(lo, min(hi, v))
                new[k] = v
        if "ignore_zones" in motion:
            zones = []
            for z in motion["ignore_zones"][:20]:
                pts = [[max(0.0, min(1.0, float(x))), max(0.0, min(1.0, float(y)))]
                       for x, y in z.get("points", [])[:50]]
                if len(pts) >= 3:
                    zones.append({"name": str(z.get("name", "Zone"))[:40], "points": pts})
            new["ignore_zones"] = zones
        cam.cfg["motion"] = new
        config.save()
        return {"motion": new}

    # ---- adding and changing cameras -------------------------------------------

    def camera_summary(c):
        on = c.get("onvif") or {}
        return {
            "id": c["id"],
            "name": c.get("name") or c["id"],
            "ip": on.get("host", ""),
            "user": on.get("username", ""),
            "has_password": bool(on.get("password")),
            "onvif_port": on.get("port", 8888),
            "configured": bool(c.get("main_url")),
        }

    @app.get("/api/cameras")
    def list_cameras():
        return [camera_summary(c) for c in config.cameras]

    @app.post("/api/cameras/setup")
    async def setup_camera(request: Request):
        """Finds a camera's video from its IP and login, then saves and starts it.

        Body: {"id": optional existing id, "name", "ip", "user", "password" (empty keeps the
        saved one), "onvif_port"}. Returns {"ok", "log": [...], "camera"}.
        """
        import copy
        import re as _re

        from .config import DEFAULT_CAMERA
        from .discovery import find_streams

        body = await request.json()
        ip = str(body.get("ip", "")).strip()
        if not _re.match(r"^[A-Za-z0-9.\-]{1,253}$", ip):
            raise HTTPException(400, "Enter the camera's IP address, for example 192.168.1.50")
        name = str(body.get("name") or "Camera").strip()[:40] or "Camera"
        user = str(body.get("user") or "admin").strip()[:64]
        existing = config.camera(body.get("id")) if body.get("id") else None
        if body.get("id") and not existing:
            raise HTTPException(404, "No such camera")
        password = str(body.get("password") or "")
        if not password and existing:
            password = (existing.get("onvif") or {}).get("password", "")
        try:
            onvif_port = int(body.get("onvif_port") or 8888)
        except (TypeError, ValueError):
            raise HTTPException(400, "The ONVIF port must be a number")

        log = []
        main_url, sub_url = await asyncio.to_thread(find_streams, ip, user, password, 554, log.append)
        if not main_url:
            log.append("The camera's video wasn't found. Check the IP address, user name and password, "
                       "and that the camera is switched on.")
            return {"ok": False, "log": log}

        if existing:
            cfg = copy.deepcopy(existing)
        else:
            cfg = copy.deepcopy(DEFAULT_CAMERA)
            base = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "camera"
            cam_id, n = base, 2
            while config.camera(cam_id):
                cam_id, n = f"{base}-{n}", n + 1
            cfg["id"] = cam_id
        cfg["name"] = name
        cfg["main_url"], cfg["sub_url"] = main_url, sub_url
        cfg["onvif"] = {"host": ip, "port": onvif_port, "username": user, "password": password}
        await asyncio.to_thread(hub.put_camera, cfg)
        log.append("Camera found and saved. It's starting now.")
        return {"ok": True, "log": log, "camera": camera_summary(cfg)}

    @app.post("/api/cameras/{cam_id}/diagnose")
    async def diagnose_camera(cam_id: str):
        """Checks how the camera's video can be read, for when it won't come online.

        The camera is paused while this runs, as many cameras allow only a few connections.
        """
        import copy

        from .camera import redact
        from .discovery import diagnose, list_paths

        c = config.camera(cam_id)
        if not c:
            raise HTTPException(404, "No such camera")
        cfg = copy.deepcopy(c)
        log = []
        cam = hub.cameras.get(cam_id)
        if cam:
            await asyncio.to_thread(cam.stop)
        try:
            await asyncio.to_thread(diagnose, cfg.get("main_url"), cfg.get("sub_url"), log.append)
            on = cfg.get("onvif") or {}
            if on.get("host"):
                await asyncio.to_thread(list_paths, on["host"], on.get("username", ""), on.get("password", ""),
                                        554, log.append)
        finally:
            await asyncio.to_thread(hub.put_camera, cfg)
        log = [redact(line) for line in log]
        for line in log:
            logging.getLogger("camhub.diagnose").info("[%s] %s", cam_id, line)
        return {"log": log}

    @app.delete("/api/cameras/{cam_id}")
    async def delete_camera(cam_id: str):
        if not config.camera(cam_id):
            raise HTTPException(404, "No such camera")
        await asyncio.to_thread(hub.remove_camera, cam_id)
        return {"ok": True}

    @app.put("/api/storage")
    async def put_storage(request: Request):
        body = await request.json()
        try:
            days = max(0, min(3650, int(body.get("retention_days", config.storage["retention_days"]))))
            free = max(0.0, min(10000.0, float(body.get("min_free_gb", config.storage["min_free_gb"]))))
        except (TypeError, ValueError):
            raise HTTPException(400, "Use whole numbers")
        config.storage["retention_days"] = days
        config.storage["min_free_gb"] = free
        config.save()
        return {"retention_days": days, "min_free_gb": free}

    from .privacy import add_privacy_routes
    add_privacy_routes(app, hub)

    # ---- recordings -----------------------------------------------------------

    @app.get("/api/days")
    def days(camera: str = None):
        return hub.storage.days(camera)

    @app.get("/api/clips")
    def clips(date: str, camera: str = None):
        return hub.storage.clips(date, camera)

    def clip_file(clip_id, ext):
        base = hub.storage.clip_base(clip_id)
        if not base or not os.path.isfile(base + ext):
            raise HTTPException(404, "Clip not found")
        return base + ext

    @app.get("/api/clips/{clip_id}/video")
    def clip_video(clip_id: str, download: int = 0):
        path = clip_file(clip_id, ".mp4")
        headers = {"Cache-Control": "private, max-age=86400"}
        if download:
            return FileResponse(path, media_type="video/mp4", filename=f"{clip_id}.mp4", headers=headers)
        return FileResponse(path, media_type="video/mp4", headers=headers)

    @app.get("/api/clips/{clip_id}/thumb")
    def clip_thumb(clip_id: str):
        return FileResponse(clip_file(clip_id, ".jpg"), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/clips/{clip_id}")
    def delete_clip(clip_id: str):
        if not hub.storage.delete_clip(clip_id):
            raise HTTPException(404, "Clip not found")
        return {"ok": True}

    from .ai_api import add_ai_routes
    add_ai_routes(app, hub)

    # ---- live events (used for phone notifications) ---------------------------

    @app.get("/api/events")
    async def events(request: Request, since: int = 0):
        q = hub.events.subscribe()

        async def stream():
            try:
                yield "retry: 5000\n\n"
                for e in hub.events.since(since) if since else []:
                    yield f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n"
                quiet = 0.0
                while not await request.is_disconnected():
                    try:
                        e = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.5)
                        quiet += 0.5
                        if quiet >= 20:
                            quiet = 0.0
                            yield ": keep-alive\n\n"
                        continue
                    quiet = 0.0
                    yield f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n"
            finally:
                hub.events.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
