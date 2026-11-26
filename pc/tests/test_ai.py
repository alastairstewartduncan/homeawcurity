"""Checks for AI sources, skills and agents, against a pretend AI service on this machine."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from camhub import ai, auth  # noqa: E402
from camhub.config import Config  # noqa: E402
from camhub.events import EventBus  # noqa: E402
from camhub.storage import Storage  # noqa: E402


class FakeAI:
    """Answers like Anthropic, OpenAI, Gemini and Ollama would. `reply` is the text it returns."""

    def __init__(self):
        self.reply = '{"alert": true, "message": "Parcel at the door", "summary": "A courier left a box."}'
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path.startswith("/v1/models"):
                    return self._json({"data": [{"id": "claude-test"}]})
                if self.path.startswith("/models?"):
                    return self._json({"models": [{"name": "models/gemini-test",
                                                   "supportedGenerationMethods": ["generateContent"]}]})
                if self.path == "/api/tags":
                    return self._json({"models": [{"name": "llava:latest"}]})
                if self.path == "/models":
                    return self._json({"data": [{"id": "gpt-test"}]})
                self._json({"error": "nope"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, dict(self.headers), body))
                if self.headers.get("x-api-key") == "bad":
                    return self._json({"error": {"message": "invalid x-api-key"}}, 401)
                if self.path == "/v1/messages":
                    return self._json({"content": [{"type": "text", "text": fake.reply}]})
                if ":generateContent" in self.path:
                    return self._json({"candidates": [{"content": {"parts": [{"text": fake.reply}]}}]})
                if self.path == "/api/chat":
                    return self._json({"message": {"content": fake.reply}})
                if self.path == "/chat/completions":
                    return self._json({"choices": [{"message": {"content": fake.reply}}]})
                self._json({"error": "nope"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_ai():
    f = FakeAI()
    yield f
    f.close()


def test_parse_json_finds_object_in_prose():
    assert ai.parse_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    assert ai.parse_json('Here: {"a": "x}y", "b": {"c": 2}} thanks') == {"a": "x}y", "b": {"c": 2}}
    assert ai.parse_json("no json here") is None


@pytest.mark.parametrize("kind,model", [("anthropic", "claude-test"), ("openai_compatible", "gpt-test"),
                                        ("gemini", "gemini-test"), ("ollama", "llava:latest")])
def test_every_kind_of_source_chats_and_lists_models(fake_ai, kind, model):
    src = {"kind": kind, "base_url": fake_ai.url, "api_key": "k123", "model": model}
    assert ai.list_models(src) == [model]
    assert "Parcel" in ai.chat(src, "system words", "what do you see", [b"\xff\xd8jpeg"])
    method, path, headers, body = fake_ai.requests[-1]
    text = json.dumps(body)
    assert "system words" in text and "what do you see" in text
    assert "/9hqcGVn" in text  # the picture, base64-encoded


def test_bad_key_explained(fake_ai):
    with pytest.raises(ai.AIError, match="API key"):
        ai.chat({"kind": "anthropic", "base_url": fake_ai.url, "api_key": "bad"}, "", "hi")
    with pytest.raises(ai.AIError, match="API key"):
        ai.chat({"kind": "openai", "name": "OpenAI", "base_url": fake_ai.url}, "", "hi")


def make_hub(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "server": {"password_hash": auth.hash_password("pw123456")},
        "storage": {"path": str(tmp_path / "rec")},
        "cameras": [{"id": "front", "name": "Front"}]}))
    config = Config(str(cfg_path))

    class FakeCam:
        def __init__(self, cfg):
            self.cfg = cfg

        def latest_jpeg(self, quality=75):
            return b"\xff\xd8picture", 1

    class Hub:
        pass

    from camhub.agents import AgentRunner
    hub = Hub()
    hub.config = config
    hub.storage = Storage(config)
    hub.events = EventBus()
    hub.cameras = {"front": FakeCam(config.cameras[0])}
    hub.agents = AgentRunner(hub)
    return hub, cfg_path


def test_ai_tab_end_to_end(tmp_path, fake_ai):
    from fastapi.testclient import TestClient

    from camhub.server import create_app

    hub, cfg_path = make_hub(tmp_path)
    client = TestClient(create_app(hub))
    assert client.get("/api/ai").status_code == 401
    assert client.post("/api/login", json={"password": "pw123456"}).status_code == 200

    start = client.get("/api/ai").json()
    assert start["skills"] and start["agents"]  # starter examples on first run
    assert "anthropic" in start["kinds"]

    # Add a source; the key is stored on the PC but never sent back to the browser.
    r = client.post("/api/ai/sources", json={"name": "Claude", "kind": "anthropic", "base_url": fake_ai.url,
                                             "api_key": "sk-secret-1234", "model": "claude-test"})
    assert r.status_code == 200
    src = r.json()
    assert "api_key" not in src and src["has_key"] and src["key_hint"] == "…1234"
    assert "sk-secret" not in client.get("/api/ai").text
    assert json.loads(cfg_path.read_text())["ai"]["default_source"] == src["id"]
    # Saving again without a key keeps the saved one.
    client.post("/api/ai/sources", json={"id": src["id"], "name": "Claude", "kind": "anthropic",
                                         "base_url": fake_ai.url, "model": "claude-test"})
    assert json.loads(cfg_path.read_text())["ai"]["sources"][0]["api_key"] == "sk-secret-1234"
    assert client.post("/api/ai/sources/models", json={"id": src["id"], "kind": "anthropic",
                                                       "base_url": fake_ai.url}).json()["models"] == ["claude-test"]

    # AI writes a skill from a description.
    fake_ai.reply = '{"name": "Spot foxes", "description": "Finds foxes", "instructions": "Look for foxes."}'
    d = client.post("/api/ai/draft", json={"kind": "skill", "description": "tell me about foxes"}).json()
    assert d["skill"]["name"] == "Spot foxes"
    sk = client.post("/api/ai/skills", json=d["skill"]).json()
    assert sk["id"] == "spot-foxes"

    # AI writes an agent, reusing that skill and suggesting a new one.
    fake_ai.reply = json.dumps({"name": "Night fox watch", "goal": "Alert me about foxes at night",
                                "triggers": ["motion", "bogus"], "alert": "ai_decides", "cooldown_minutes": 10,
                                "skills": ["spot-foxes", "missing"],
                                "new_skills": [{"name": "Night vision", "instructions": "IR pictures are grey"}]})
    d = client.post("/api/ai/draft", json={"kind": "agent", "description": "foxes in the garden at night"}).json()
    assert d["agent"]["triggers"] == ["motion"] and d["agent"]["skills"] == ["spot-foxes"]
    assert d["new_skills"][0]["name"] == "Night vision"
    ag = client.post("/api/ai/agents", json=d["agent"]).json()
    assert ag["id"] == "night-fox-watch"

    # Test the agent: it looks at the picture and decides, without alerting the phone.
    q = hub.events.subscribe()
    fake_ai.reply = '{"alert": true, "message": "Fox on the lawn", "summary": "A fox is crossing the lawn."}'
    rec = client.post(f"/api/ai/agents/{ag['id']}/test", json={}).json()
    assert rec["alert"] is True and rec["message"] == "Fox on the lawn" and not rec.get("error")
    assert q.empty()
    body = fake_ai.requests[-1][3]
    assert "Look for foxes." in body["system"] and "Alert me about foxes" in body["system"]
    assert body["messages"][0]["content"][0]["type"] == "image"
    act = client.get("/api/ai/activity").json()
    assert act[0]["id"] == rec["id"] and act[0]["has_picture"]
    assert client.get(f"/api/ai/runs/{rec['id']}/picture.jpg").content == b"\xff\xd8picture"

    # A real camera event runs the agent and sends an alert, once per cool-down.
    hub.agents.on_event({"type": "clip_started", "camera": "front", "person": False})
    agent_id, cam, trig, ev = hub.agents._q.get_nowait()
    agent = next(a for a in hub.agents.section["agents"] if a["id"] == agent_id)
    hub.agents.run(agent, cam, trig, ev)
    e = q.get_nowait()
    assert e["type"] == "ai_alert" and e["message"] == "Fox on the lawn" and e["camera_name"] == "Front"
    rec2 = hub.agents.run(agent, cam, trig, ev)
    assert rec2["alert"] is False and rec2["alert_skipped"]

    # Deleting a skill removes it from agents.
    assert client.delete("/api/ai/skills/spot-foxes").status_code == 200
    assert client.get("/api/ai").json()["agents"][-1]["skills"] == []


def test_agent_reports_missing_source(tmp_path):
    hub, _ = make_hub(tmp_path)
    agent = hub.agents.section["agents"][0]
    rec = hub.agents.run(agent, "front", "manual", {})
    assert "Add an AI source" in rec["error"] and rec["alert"] is False


def _make_clip(hub, when, person=True):
    import datetime

    import cv2
    import numpy as np
    clip_id, base = hub.storage.new_clip_paths("front", when)
    os.makedirs(os.path.dirname(base), exist_ok=True)
    w = cv2.VideoWriter(base + ".mp4", cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
    for i in range(30):
        img = np.full((240, 320, 3), i * 8, np.uint8)
        w.write(img)
    w.release()
    with open(base + ".json", "w") as f:
        json.dump({"start": when.isoformat(timespec="seconds"), "duration": 3, "person": person}, f)
    return clip_id


def test_recording_descriptions_digest_and_questions(tmp_path, fake_ai):
    import datetime

    from fastapi.testclient import TestClient

    from camhub.recordings_ai import clip_frames, wait_until_idle
    from camhub.server import create_app

    hub, cfg_path = make_hub(tmp_path)
    client = TestClient(create_app(hub))
    client.post("/api/login", json={"password": "pw123456"})
    client.post("/api/ai/sources", json={"name": "Claude", "kind": "anthropic", "base_url": fake_ai.url,
                                         "api_key": "sk-secret-1234", "model": "claude-test"})
    now = datetime.datetime.now().replace(microsecond=0)
    c1 = _make_clip(hub, now - datetime.timedelta(hours=2))
    c2 = _make_clip(hub, now - datetime.timedelta(hours=1), person=False)
    assert len(clip_frames(hub.storage.clip_base(c1) + ".mp4")) == 3

    # Turned off by default: nothing is described.
    assert client.get("/api/ai/recordings").json()["settings"]["summaries"] is False
    r = client.put("/api/ai/recordings", json={"summaries": True, "digest": True, "digest_time": "25:99"})
    assert r.json()["summaries"] and r.json()["digest_time"] == "08:00"

    rec = hub.agents.recordings
    rec.start()
    try:
        fake_ai.reply = '{"summary": "A courier in a yellow jacket leaves a parcel.", "tags": ["delivery", "bogus"]}'
        hub.agents.on_event({"type": "clip_saved", "camera": "front", "clip": c1})
        wait_until_idle(rec)
    finally:
        rec.stop()
    clips = client.get("/api/clips", params={"date": c1.split("_")[1][:4] + "-" + c1.split("_")[1][4:6] + "-"
                                              + c1.split("_")[1][6:]}).json()
    by_id = {c["id"]: c for c in clips}
    assert by_id[c1]["summary"].startswith("A courier") and by_id[c1]["tags"] == ["delivery"]
    assert by_id[c2]["summary"] == ""
    body = fake_ai.requests[-1][3]
    assert sum(1 for p in body["messages"][0]["content"] if p["type"] == "image") == 3

    # Describe one recording on request.
    fake_ai.reply = '{"summary": "A cat crosses the lawn.", "tags": ["animal"]}'
    assert client.post("/api/ai/recordings/describe", json={"clip": c2}).json()["tags"] == ["animal"]

    # Ask your recordings: the AI only sees descriptions and may only point at real clips.
    fake_ai.reply = json.dumps({"answer": "The courier came about two hours ago.", "clips": [c1, "made-up"]})
    a = client.post("/api/ai/ask", json={"question": "when did the postman come?"}).json()
    assert a["answer"].startswith("The courier") and [c["id"] for c in a["clips"]] == [c1]
    sent = fake_ai.requests[-1][3]["messages"][0]["content"][0]["text"]
    assert "yellow jacket" in sent and "postman" in sent

    # Morning digest goes to the phone as an event.
    q = hub.events.subscribe()
    fake_ai.reply = json.dumps({"headline": "1 delivery, 1 cat", "details": "A parcel came at 07:00.",
                                "highlights": [c1]})
    d = client.post("/api/ai/digest").json()
    assert d["count"] == 2 and d["text"].startswith("1 delivery, 1 cat") and d["highlights"] == [c1]
    e = q.get_nowait()
    while e["type"] != "ai_digest":
        e = q.get_nowait()
    assert e["clip"] == c1
    assert client.get("/api/ai/recordings").json()["digests"][0]["text"].startswith("1 delivery")
