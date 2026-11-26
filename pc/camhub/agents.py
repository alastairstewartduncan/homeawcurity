"""AI agents and skills.

A *skill* is a reusable piece of know-how written in plain words, for example
"Spot parcel deliveries" or "Describe people: clothing, what they carry".

An *agent* watches for camera events (a person seen, a recording starting or
finishing, or simply every N minutes), looks at the camera picture with the
chosen AI source and model, uses its skills, and decides whether to send a phone
alert. Agents can remember their last few observations so they notice changes.

Everything lives in config.json under "ai":
    {"sources": [...], "default_source": id, "skills": [...], "agents": [...]}
"""
import collections
import copy
import datetime
import itertools
import json
import logging
import os
import queue
import re
import threading
import time

from . import ai

log = logging.getLogger("camhub.agents")

TRIGGERS = {
    "person": "A person is seen",
    "motion": "A recording starts (any movement)",
    "clip_saved": "A recording finishes",
    "schedule": "Every few minutes",
}
ALERT_MODES = ("ai_decides", "always", "never")

DEFAULT_SKILL = {"id": "", "name": "", "description": "", "instructions": ""}
DEFAULT_AGENT = {
    "id": "", "name": "", "enabled": True,
    "goal": "",
    "skills": [],
    "cameras": [],            # empty means every camera
    "triggers": ["person"],
    "every_minutes": 30,      # for the "schedule" trigger
    "source": "",             # empty means the default AI source
    "model": "",              # empty means the source's model
    "alert": "ai_decides",
    "cooldown_minutes": 5,
    "memory": True,
}

STARTER_SKILLS = [
    {"id": "describe-people", "name": "Describe people",
     "description": "Says who is in view: how many, clothing, what they carry, what they are doing.",
     "instructions": "For each person you can see, note roughly their age group, clothing colours, anything they "
                     "carry (bag, parcel, tools) and what they appear to be doing. Don't guess names or identities."},
    {"id": "spot-deliveries", "name": "Spot deliveries",
     "description": "Recognises couriers and parcels left at the door.",
     "instructions": "Look for signs of a delivery: a courier uniform, a branded van, a parcel being carried or "
                     "left on the ground. Say which courier if a logo is clearly visible."},
    {"id": "suspicious-behaviour", "name": "Spot suspicious behaviour",
     "description": "Flags things like trying door handles, looking into windows or covering faces.",
     "instructions": "Treat these as suspicious: trying door or car handles, looking through windows, hanging "
                     "around without purpose, covering the face, climbing fences, carrying tools at night. "
                     "Ordinary visitors, postal workers and neighbours walking past are not suspicious."},
]

STARTER_AGENTS = [
    {**DEFAULT_AGENT, "id": "doorstep-watcher", "name": "Doorstep watcher", "enabled": False,
     "goal": "Tell me who came to the house and why. Only alert me for deliveries and anything suspicious, "
             "not for people just walking past.",
     "skills": ["describe-people", "spot-deliveries", "suspicious-behaviour"], "triggers": ["person"]},
]


def _slug(text, taken):
    base = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "item"
    s, n = base, 2
    while s in taken:
        s, n = f"{base}-{n}", n + 1
    return s


def clean_source(s, existing=None):
    kind = s.get("kind") if s.get("kind") in ai.KINDS else "anthropic"
    out = {
        "id": str(s.get("id") or ""),
        "name": str(s.get("name") or ai.KINDS[kind]["label"]).strip()[:60],
        "kind": kind,
        "base_url": str(s.get("base_url") or "").strip()[:300],
        "model": str(s.get("model") or "").strip()[:120],
        "api_key": str(s.get("api_key") or "").strip()[:500],
    }
    # An empty key from the browser means "keep the saved one"; keys are never sent back out.
    if not out["api_key"] and existing and not s.get("clear_key"):
        out["api_key"] = existing.get("api_key", "")
    return out


def clean_skill(s):
    return {
        "id": str(s.get("id") or ""),
        "name": str(s.get("name") or "Skill").strip()[:60],
        "description": str(s.get("description") or "").strip()[:300],
        "instructions": str(s.get("instructions") or "").strip()[:4000],
    }


def clean_agent(a, skill_ids=None, camera_ids=None, source_ids=None):
    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default
    out = {
        "id": str(a.get("id") or ""),
        "name": str(a.get("name") or "Agent").strip()[:60],
        "enabled": bool(a.get("enabled", True)),
        "goal": str(a.get("goal") or "").strip()[:4000],
        "skills": [str(x) for x in (a.get("skills") or [])][:20],
        "cameras": [str(x) for x in (a.get("cameras") or [])][:20],
        "triggers": [t for t in (a.get("triggers") or []) if t in TRIGGERS] or ["person"],
        "every_minutes": num(a.get("every_minutes"), 1, 1440, 30),
        "source": str(a.get("source") or ""),
        "model": str(a.get("model") or "").strip()[:120],
        "alert": a.get("alert") if a.get("alert") in ALERT_MODES else "ai_decides",
        "cooldown_minutes": num(a.get("cooldown_minutes"), 0, 1440, 5),
        "memory": bool(a.get("memory", True)),
    }
    if skill_ids is not None:
        out["skills"] = [s for s in out["skills"] if s in skill_ids]
    if camera_ids is not None:
        out["cameras"] = [c for c in out["cameras"] if c in camera_ids]
    if source_ids is not None and out["source"] not in source_ids:
        out["source"] = ""
    return out


class AgentRunner:
    """Runs agents in the background when camera events happen."""

    LOG_KEEP = 300
    PICTURES_KEEP = 60

    def __init__(self, hub):
        self.hub = hub
        self.config = hub.config
        self._q = queue.Queue(maxsize=30)
        self._stop = threading.Event()
        self._last = {}               # (agent id, camera id) -> time of last run
        self._last_alert = {}         # (agent id, camera id) -> time of last alert
        self._notes = collections.defaultdict(lambda: collections.deque(maxlen=5))
        self._runs = itertools.count(int(time.time()))
        self.activity = collections.deque(maxlen=self.LOG_KEEP)
        self.pictures = collections.OrderedDict()   # run id -> jpeg bytes
        self._lock = threading.Lock()
        self._log_path = os.path.join(os.path.dirname(self.config.path), "ai_activity.jsonl")
        self._load_activity()
        self._ensure_section()

    # ---- settings -----------------------------------------------------------

    def _ensure_section(self):
        data = self.config.data
        sec = data.get("ai")
        if not isinstance(sec, dict):
            sec = {}
        first_time = "skills" not in sec
        sec.setdefault("sources", [])
        sec.setdefault("default_source", "")
        sec.setdefault("skills", copy.deepcopy(STARTER_SKILLS) if first_time else [])
        sec.setdefault("agents", copy.deepcopy(STARTER_AGENTS) if first_time else [])
        data["ai"] = sec
        if first_time:
            self.config.save()

    @property
    def section(self):
        return self.config.data["ai"]

    def source(self, source_id=""):
        sources = self.section["sources"]
        want = source_id or self.section.get("default_source") or ""
        for s in sources:
            if s["id"] == want:
                return s
        if sources:
            return sources[0]
        raise ai.AIError("Add an AI source first (AI tab > AI sources).")

    def skill(self, skill_id):
        return next((s for s in self.section["skills"] if s["id"] == skill_id), None)

    def new_id(self, kind, name):
        return _slug(name, {x["id"] for x in self.section[kind]})

    # ---- background work ------------------------------------------------------

    def start(self):
        threading.Thread(target=self._listen, name="ai-events", daemon=True).start()
        threading.Thread(target=self._work, name="ai-worker", daemon=True).start()
        threading.Thread(target=self._schedule, name="ai-schedule", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _listen(self):
        q = self.hub.events.subscribe()
        try:
            while not self._stop.is_set():
                try:
                    e = q.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    self.on_event(e)
                except Exception:
                    log.exception("AI agent event handling failed")
        finally:
            self.hub.events.unsubscribe(q)

    def on_event(self, e):
        kinds = {"person": "person", "clip_started": "motion", "clip_saved": "clip_saved"}
        trig = kinds.get(e.get("type"))
        if not trig:
            return
        cam = e.get("camera")
        for a in list(self.section["agents"]):
            if not a.get("enabled") or (a.get("cameras") and cam not in a["cameras"]):
                continue
            hit = trig in a["triggers"]
            # A recording started by a person also counts as "person seen".
            if trig == "motion" and e.get("person") and "person" in a["triggers"]:
                hit = True
            if hit:
                self.queue(a, cam, trig, e)

    def _schedule(self):
        while not self._stop.wait(20):
            now = time.time()
            for a in list(self.section["agents"]):
                if not a.get("enabled") or "schedule" not in a.get("triggers", []):
                    continue
                for cam in self._cams_for(a):
                    if now - self._last.get((a["id"], cam), 0) >= a.get("every_minutes", 30) * 60:
                        self.queue(a, cam, "schedule", {})

    def _cams_for(self, a):
        ids = list(self.hub.cameras.keys())
        return [c for c in ids if not a.get("cameras") or c in a["cameras"]]

    def queue(self, agent, cam_id, trigger, event):
        key = (agent["id"], cam_id)
        now = time.time()
        # Don't run the same agent on the same camera more than every 20 seconds.
        if trigger != "manual" and now - self._last.get(key, 0) < 20:
            return False
        self._last[key] = now
        try:
            self._q.put_nowait((agent["id"], cam_id, trigger, event))
            return True
        except queue.Full:
            log.warning("AI agents are busy; skipped %s on %s", agent["name"], cam_id)
            return False

    def _work(self):
        while not self._stop.is_set():
            try:
                agent_id, cam_id, trigger, event = self._q.get(timeout=1)
            except queue.Empty:
                continue
            agent = next((a for a in self.section["agents"] if a["id"] == agent_id), None)
            if agent:
                try:
                    self.run(agent, cam_id, trigger, event)
                except Exception:
                    log.exception("AI agent %s failed", agent_id)

    # ---- one run ---------------------------------------------------------------

    def _picture(self, cam_id, event):
        if event.get("type") == "clip_saved" and event.get("clip"):
            base = self.hub.storage.clip_base(event["clip"])
            if base and os.path.isfile(base + ".jpg"):
                with open(base + ".jpg", "rb") as f:
                    return f.read()
        cam = self.hub.cameras.get(cam_id)
        if cam is None:
            return None
        data, _ = cam.latest_jpeg(85)
        return data

    def build_prompt(self, agent, cam_name, trigger, event, has_picture):
        skills = [s for s in (self.skill(i) for i in agent.get("skills", [])) if s]
        system = [
            f'You are "{agent["name"]}", an AI agent inside Home Camera, a home security camera system '
            "on the owner's own PC. You are given a picture from a security camera and what just happened.",
            "Your goal, in the owner's words:\n" + (agent.get("goal") or "Describe what you see."),
        ]
        if skills:
            system.append("Your skills (use each one that applies):\n" + "\n\n".join(
                f"### {s['name']}\n{s['instructions']}" for s in skills))
        alert_rule = {
            "always": 'Always set "alert" to true.',
            "never": 'Always set "alert" to false; you only keep notes.',
            "ai_decides": 'Set "alert" to true only when your goal says the owner should be told now.',
        }[agent.get("alert", "ai_decides")]
        system.append(
            "Reply with only a JSON object, no other text:\n"
            '{"alert": true or false, "message": "phone notification text, under 120 characters", '
            '"summary": "one to three sentences on what you saw and why you decided"}\n' + alert_rule +
            " Be factual; if the picture is dark or unclear, say so rather than guessing.")
        what = {
            "person": "A person was detected", "motion": "Movement started a recording",
            "clip_saved": "A recording just finished; this is a picture from it",
            "schedule": "Routine check", "manual": "The owner pressed Test",
        }.get(trigger, trigger)
        when = datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
        user = [f"Camera: {cam_name}", f"What happened: {what}", f"Time: {when}"]
        notes = list(self._notes[(agent["id"], event.get("camera") or "")]) if agent.get("memory") else []
        if notes:
            user.append("Your notes from earlier on this camera (oldest first):\n" + "\n".join(f"- {n}" for n in notes))
        if not has_picture:
            user.append("No camera picture is available right now.")
        return "\n\n".join(system), "\n".join(user)

    def run(self, agent, cam_id, trigger, event, wait_for_picture=True, publish=True):
        """Runs one agent on one camera. Returns the activity record."""
        cam = self.hub.cameras.get(cam_id)
        cam_name = (cam.cfg.get("name") if cam else None) or cam_id
        picture = self._picture(cam_id, event)
        if picture is None and wait_for_picture and cam is not None:
            time.sleep(2)
            picture = self._picture(cam_id, event)
        run_id = str(next(self._runs))
        rec = {"id": run_id, "time": datetime.datetime.now().isoformat(timespec="seconds"),
               "agent": agent["id"], "agent_name": agent["name"], "camera": cam_id, "camera_name": cam_name,
               "trigger": trigger, "clip": event.get("clip"), "picture": picture is not None}
        try:
            src = self.source(agent.get("source"))
            model = agent.get("model") or src.get("model") or ai.KINDS[src["kind"]]["model"]
            rec.update(source=src["name"], model=model)
            system, user = self.build_prompt(agent, cam_name, trigger, dict(event, camera=cam_id), picture is not None)
            started = time.time()
            text = ai.chat(src, system, user, [picture] if picture else [], model=model)
            rec["seconds"] = round(time.time() - started, 1)
            reply = ai.parse_json(text) or {"alert": False, "summary": text[:500], "message": ""}
            alert = bool(reply.get("alert"))
            mode = agent.get("alert", "ai_decides")
            if mode == "always":
                alert = True
            elif mode == "never":
                alert = False
            rec.update(summary=str(reply.get("summary") or "")[:1000],
                       message=str(reply.get("message") or "")[:200], alert=alert)
            if agent.get("memory") and rec["summary"]:
                self._notes[(agent["id"], cam_id)].append(f"{rec['time'][11:16]} {rec['summary'][:200]}")
        except ai.AIError as e:
            rec.update(error=str(e), alert=False)
        except Exception as e:
            log.exception("AI agent run failed")
            rec.update(error=f"Unexpected problem: {e}", alert=False)

        if rec.get("alert") and trigger != "manual":
            key = (agent["id"], cam_id)
            cool = agent.get("cooldown_minutes", 5) * 60
            if time.time() - self._last_alert.get(key, 0) < cool:
                rec["alert_skipped"] = "A similar alert was sent a moment ago"
                rec["alert"] = False
            else:
                self._last_alert[key] = time.time()

        with self._lock:
            if picture:
                self.pictures[run_id] = picture
                while len(self.pictures) > self.PICTURES_KEEP:
                    self.pictures.popitem(last=False)
            self.activity.append(rec)
        self._append_log(rec)
        log.info("Agent %s on %s: %s", agent["name"], cam_name,
                 rec.get("error") or (("ALERT " if rec.get("alert") else "") + rec.get("summary", "")[:150]))
        if rec.get("alert") and publish:
            self.hub.events.publish({
                "type": "ai_alert", "camera": cam_id, "camera_name": cam_name,
                "agent": agent["id"], "agent_name": agent["name"],
                "message": rec.get("message") or rec.get("summary", "")[:120],
                "summary": rec.get("summary", ""), "run": run_id, "clip": event.get("clip"),
                "time": rec["time"]})
        return rec

    def _append_log(self, rec):
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            if os.path.getsize(self._log_path) > 2_000_000:
                with open(self._log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-self.LOG_KEEP:]
                with open(self._log_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
        except OSError as e:
            log.warning("Couldn't write the AI activity log: %s", e)

    def _load_activity(self):
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                for line in f.readlines()[-self.LOG_KEEP:]:
                    try:
                        self.activity.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass

    # ---- AI help writing agents and skills ----------------------------------------

    def draft(self, kind, description, current=None, source_id=""):
        """Asks the AI to write (or change) an agent or skill from a plain description."""
        src = self.source(source_id)
        if kind == "skill":
            shape = ('{"name": "short name", "description": "one sentence on what it does", '
                     '"instructions": "clear instructions for an AI looking at a security camera picture"}')
            extra = ""
        else:
            shape = ('{"name": "short name", "goal": "what the agent should watch for and when to alert, in plain words", '
                     f'"triggers": [one or more of {json.dumps(list(TRIGGERS))}], "every_minutes": number, '
                     '"alert": "ai_decides" | "always" | "never", "cooldown_minutes": number, "memory": true or false, '
                     '"skills": ["ids of existing skills to use"], '
                     '"new_skills": [{"name": ..., "description": ..., "instructions": ...}]}')
            skills = [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in self.section["skills"]]
            cams = [{"id": c["id"], "name": c.get("name")} for c in self.config.cameras]
            extra = ("\nExisting skills you can reuse (prefer these; add new_skills only for what's missing):\n"
                     + json.dumps(skills) + "\nCameras: " + json.dumps(cams)
                     + '\nTriggers: "person" = a person is detected, "motion" = any recording starts, '
                       '"clip_saved" = a recording finishes, "schedule" = every N minutes.')
        system = ("You help a home owner set up AI agents for their home security cameras (Home Camera). "
                  "An agent is run on camera events, gets a camera picture, and decides whether to alert the "
                  "owner's phone. A skill is reusable know-how an agent can use. Write in plain, friendly English. "
                  "Never include anything about identifying people by face or name. "
                  "Reply with only one JSON object of this shape:\n" + shape + extra)
        prompt = f"What the owner wants:\n{description.strip()}"
        if current:
            prompt = (f"Here is the current {kind}:\n{json.dumps(current)}\n\n"
                      f"Change it as the owner asks:\n{description.strip()}")
        text = ai.chat(src, system, prompt, max_tokens=1500)
        out = ai.parse_json(text)
        if not out:
            raise ai.AIError("The AI didn't return a usable draft. Try describing it a little differently.")
        if kind == "skill":
            return {"skill": clean_skill(out)}
        new_skills = [clean_skill(s) for s in (out.get("new_skills") or [])[:5] if isinstance(s, dict)]
        agent = clean_agent(out, skill_ids={s["id"] for s in self.section["skills"]},
                            camera_ids={c["id"] for c in self.config.cameras})
        if current:
            agent["id"] = current.get("id", "")
            agent["source"], agent["model"] = current.get("source", ""), current.get("model", "")
            agent["cameras"] = current.get("cameras", [])
            agent["enabled"] = bool(current.get("enabled", True))
        return {"agent": agent, "new_skills": new_skills}
