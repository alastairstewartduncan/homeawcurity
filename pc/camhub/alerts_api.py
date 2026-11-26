"""Web routes for home/away mode and health alerts."""
from fastapi import HTTPException, Request


def add_alert_routes(app, hub):

    async def body_of(request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        return body

    @app.get("/api/presence")
    def get_presence():
        return hub.presence.state()

    @app.put("/api/presence")
    async def put_presence(request: Request):
        try:
            return hub.presence.update(await body_of(request))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/presence/report")
    async def report_presence(request: Request):
        """Sent by the phone app when it arrives home or leaves, and every few hours."""
        body = await body_of(request)
        try:
            return hub.presence.report(body.get("phone"), body.get("name"), body.get("home"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/presence/phones/{phone_id}")
    def forget_phone(phone_id: str):
        if not hub.presence.forget_phone(phone_id):
            raise HTTPException(404, "No such phone")
        return hub.presence.state()

    @app.get("/api/health")
    def get_health():
        return {"problems": hub.health.status(), "settings": dict(hub.health.settings)}

    @app.put("/api/health")
    async def put_health(request: Request):
        try:
            settings = hub.health.update(await body_of(request))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"problems": hub.health.status(), "settings": settings}
