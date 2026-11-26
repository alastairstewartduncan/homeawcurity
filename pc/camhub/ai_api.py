"""Web routes for the AI tab: AI sources, skills, agents, AI drafting and the activity log."""
import asyncio

from fastapi import HTTPException, Request
from fastapi.responses import Response

from . import ai
from .agents import TRIGGERS, AgentRunner, clean_agent, clean_skill, clean_source
from .recordings_ai import clean_settings


def _public_source(s):
    out = {k: v for k, v in s.items() if k != "api_key"}
    key = s.get("api_key") or ""
    out["has_key"] = bool(key)
    out["key_hint"] = ("…" + key[-4:]) if len(key) >= 8 else ""
    return out


def add_ai_routes(app, hub):
    config = hub.config

    def runner() -> AgentRunner:
        r = getattr(hub, "agents", None)
        if r is None:
            raise HTTPException(503, "AI is not running")
        return r

    def section():
        return runner().section

    async def body_of(request):
        try:
            b = await request.json()
        except ValueError:
            b = None
        if not isinstance(b, dict):
            raise HTTPException(400, "Expected a JSON object")
        return b

    def ai_error(e):
        return HTTPException(400, str(e))

    @app.get("/api/ai")
    def get_ai():
        sec = section()
        return {
            "kinds": {k: {"label": v["label"], "base_url": v["base_url"], "model": v["model"],
                          "needs_key": v["needs_key"]} for k, v in ai.KINDS.items()},
            "triggers": TRIGGERS,
            "sources": [_public_source(s) for s in sec["sources"]],
            "default_source": sec.get("default_source", ""),
            "skills": sec["skills"],
            "agents": sec["agents"],
            "recordings": sec.get("recordings", {}),
            "cameras": [{"id": c["id"], "name": c.get("name") or c["id"]} for c in config.cameras],
        }

    # ---- AI sources ------------------------------------------------------------

    def _merge_source(b):
        sec = section()
        existing = next((s for s in sec["sources"] if s["id"] == b.get("id")), None) if b.get("id") else None
        src = clean_source(b, existing)
        if not src["id"]:
            src["id"] = runner().new_id("sources", src["name"])
        return src, existing

    @app.post("/api/ai/sources")
    async def save_source(request: Request):
        b = await body_of(request)
        src, existing = _merge_source(b)
        sec = section()
        if existing:
            sec["sources"] = [src if s["id"] == src["id"] else s for s in sec["sources"]]
        else:
            sec["sources"].append(src)
        if b.get("make_default") or not sec.get("default_source") or len(sec["sources"]) == 1:
            sec["default_source"] = src["id"]
        config.save()
        return _public_source(src)

    @app.delete("/api/ai/sources/{source_id}")
    def delete_source(source_id: str):
        sec = section()
        before = len(sec["sources"])
        sec["sources"] = [s for s in sec["sources"] if s["id"] != source_id]
        if len(sec["sources"]) == before:
            raise HTTPException(404, "No such AI source")
        if sec.get("default_source") == source_id:
            sec["default_source"] = sec["sources"][0]["id"] if sec["sources"] else ""
        for a in sec["agents"]:
            if a.get("source") == source_id:
                a["source"] = ""
        config.save()
        return {"ok": True}

    @app.post("/api/ai/sources/models")
    async def source_models(request: Request):
        """Lists models for a source as typed in the form (the saved key is used if none is typed)."""
        src, _ = _merge_source(await body_of(request))
        try:
            return {"models": await asyncio.to_thread(ai.list_models, src)}
        except ai.AIError as e:
            raise ai_error(e)

    @app.post("/api/ai/sources/test")
    async def source_test(request: Request):
        src, _ = _merge_source(await body_of(request))
        try:
            text = await asyncio.to_thread(
                ai.chat, src, "", "Reply with the single word: ready", (), None, 20)
        except ai.AIError as e:
            raise ai_error(e)
        return {"ok": True, "reply": text[:200]}

    # ---- skills ------------------------------------------------------------------

    @app.post("/api/ai/skills")
    async def save_skill(request: Request):
        b = await body_of(request)
        sec = section()
        sk = clean_skill(b)
        if not sk["instructions"]:
            raise HTTPException(400, "Write what the skill should do")
        if sk["id"] and any(s["id"] == sk["id"] for s in sec["skills"]):
            sec["skills"] = [sk if s["id"] == sk["id"] else s for s in sec["skills"]]
        else:
            sk["id"] = runner().new_id("skills", sk["name"])
            sec["skills"].append(sk)
        config.save()
        return sk

    @app.delete("/api/ai/skills/{skill_id}")
    def delete_skill(skill_id: str):
        sec = section()
        if not any(s["id"] == skill_id for s in sec["skills"]):
            raise HTTPException(404, "No such skill")
        sec["skills"] = [s for s in sec["skills"] if s["id"] != skill_id]
        for a in sec["agents"]:
            a["skills"] = [x for x in a.get("skills", []) if x != skill_id]
        config.save()
        return {"ok": True}

    # ---- agents ------------------------------------------------------------------

    @app.post("/api/ai/agents")
    async def save_agent(request: Request):
        b = await body_of(request)
        sec = section()
        ag = clean_agent(b, skill_ids={s["id"] for s in sec["skills"]},
                         camera_ids={c["id"] for c in config.cameras},
                         source_ids={s["id"] for s in sec["sources"]})
        if ag["id"] and any(a["id"] == ag["id"] for a in sec["agents"]):
            sec["agents"] = [ag if a["id"] == ag["id"] else a for a in sec["agents"]]
        else:
            ag["id"] = runner().new_id("agents", ag["name"])
            sec["agents"].append(ag)
        config.save()
        return ag

    @app.delete("/api/ai/agents/{agent_id}")
    def delete_agent(agent_id: str):
        sec = section()
        if not any(a["id"] == agent_id for a in sec["agents"]):
            raise HTTPException(404, "No such agent")
        sec["agents"] = [a for a in sec["agents"] if a["id"] != agent_id]
        config.save()
        return {"ok": True}

    @app.post("/api/ai/agents/{agent_id}/test")
    async def test_agent(agent_id: str, request: Request):
        """Runs the agent now on one camera and returns what it decided (no phone alert is sent)."""
        b = await body_of(request)
        agent = next((a for a in section()["agents"] if a["id"] == agent_id), None)
        if not agent:
            raise HTTPException(404, "No such agent")
        cams = [c for c in hub.cameras if not agent.get("cameras") or c in agent["cameras"]]
        cam = b.get("camera") or (cams[0] if cams else None)
        if not cam or cam not in hub.cameras:
            raise HTTPException(400, "Set up a camera first")
        rec = await asyncio.to_thread(runner().run, agent, cam, "manual", {}, True, bool(b.get("send_alert")))
        return rec

    # ---- AI drafting ---------------------------------------------------------------

    @app.post("/api/ai/draft")
    async def draft(request: Request):
        b = await body_of(request)
        kind = b.get("kind")
        desc = str(b.get("description") or "").strip()
        if kind not in ("agent", "skill"):
            raise HTTPException(400, "kind must be agent or skill")
        if len(desc) < 3:
            raise HTTPException(400, "Describe what you want in a few words")
        try:
            return await asyncio.to_thread(runner().draft, kind, desc[:3000], b.get("current"),
                                           str(b.get("source") or ""))
        except ai.AIError as e:
            raise ai_error(e)

    # ---- activity -------------------------------------------------------------------

    @app.get("/api/ai/activity")
    def activity(agent: str = None, limit: int = 100):
        items = [r for r in runner().activity if not agent or r.get("agent") == agent]
        pics = runner().pictures
        out = []
        for r in reversed(items[-max(1, min(300, limit)):]):
            out.append(dict(r, has_picture=r.get("id") in pics))
        return out

    @app.get("/api/ai/runs/{run_id}/picture.jpg")
    def run_picture(run_id: str):
        data = runner().pictures.get(run_id)
        if data is None:
            raise HTTPException(404, "Picture no longer kept")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ---- recordings: descriptions, morning digest, questions --------------------------

    def rec_ai():
        return runner().recordings

    @app.get("/api/ai/recordings")
    def get_recordings_ai():
        r = rec_ai()
        return {"settings": r.settings, "digests": list(reversed(r.digests)), "pending": r.pending()}

    @app.put("/api/ai/recordings")
    async def put_recordings_ai(request: Request):
        b = await body_of(request)
        sec = section()
        sec["recordings"] = clean_settings(b, {s["id"] for s in sec["sources"]})
        config.save()
        return sec["recordings"]

    @app.post("/api/ai/recordings/describe")
    async def describe_recordings(request: Request):
        """{"clip": id} describes one recording now; {"days": n} queues the undescribed ones."""
        b = await body_of(request)
        if b.get("clip"):
            try:
                return await asyncio.to_thread(rec_ai().describe, str(b["clip"]))
            except ai.AIError as e:
                raise ai_error(e)
        try:
            days = max(1, min(30, int(b.get("days") or 1)))
        except (TypeError, ValueError):
            days = 1
        if not section()["sources"]:
            raise HTTPException(400, "Add an AI source first (AI tab > AI sources).")
        return {"queued": await asyncio.to_thread(rec_ai().describe_missing, days, 200)}

    @app.post("/api/ai/digest")
    async def digest_now():
        try:
            return await asyncio.to_thread(rec_ai().make_digest, 24, True)
        except ai.AIError as e:
            raise ai_error(e)

    @app.post("/api/ai/ask")
    async def ask(request: Request):
        b = await body_of(request)
        q = str(b.get("question") or "").strip()
        if len(q) < 3:
            raise HTTPException(400, "Type a question first")
        try:
            days = max(1, min(30, int(b.get("days") or 7)))
        except (TypeError, ValueError):
            days = 7
        try:
            return await asyncio.to_thread(rec_ai().ask, q[:500], days)
        except ai.AIError as e:
            raise ai_error(e)
