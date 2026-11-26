"""Talking to AI services: Anthropic (Claude), OpenAI, Google Gemini, Ollama, or anything
that speaks the OpenAI chat format (LM Studio, OpenRouter, Groq, Azure and others).

Each "AI source" in Settings is a dict:
    {"id", "name", "kind", "base_url", "api_key", "model"}
`kind` is one of KINDS below. Only the Python standard library is used, so nothing extra
needs installing on the PC.
"""
import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("camhub.ai")

# kind -> what the settings page shows, the usual address and a starting model.
KINDS = {
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com",
                  "model": "claude-haiku-5-5", "needs_key": True},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1",
               "model": "gpt-4o-mini", "needs_key": True},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "model": "gemini-2.0-flash", "needs_key": True},
    "ollama": {"label": "Ollama (runs on your own PC)", "base_url": "http://localhost:11434",
               "model": "llava", "needs_key": False},
    "openai_compatible": {"label": "Other (OpenAI-compatible: LM Studio, OpenRouter, Groq...)",
                          "base_url": "http://localhost:1234/v1", "model": "", "needs_key": False},
}

TIMEOUT = 90


class AIError(Exception):
    """A problem worth showing to the person, in plain words."""


def _post(url, body, headers, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    return _send(req, timeout)


def _get(url, headers, timeout=20):
    return _send(urllib.request.Request(url, headers=headers), timeout)


def _send(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(text)
            err = j.get("error")
            msg = (err.get("message") if isinstance(err, dict) else err) or j.get("message") or text
        except ValueError:
            msg = text
        if e.code in (401, 403):
            raise AIError(f"The AI service didn't accept the API key ({e.code}): {str(msg)[:200]}")
        if e.code == 404:
            raise AIError(f"Not found (404). Check the model name and address: {str(msg)[:200]}")
        if e.code == 429:
            raise AIError("The AI service says too many requests or out of credit (429). Try again later.")
        raise AIError(f"The AI service returned an error ({e.code}): {str(msg)[:300]}")
    except urllib.error.URLError as e:
        raise AIError(f"Couldn't reach the AI service at {req.full_url.split('?')[0]}: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise AIError(f"The AI service didn't answer in time: {e}")
    except ValueError:
        raise AIError("The AI service sent back something that wasn't JSON. Check the address.")


def _base(src):
    kind = src.get("kind")
    if kind not in KINDS:
        raise AIError(f"Unknown kind of AI source: {kind}")
    url = (src.get("base_url") or KINDS[kind]["base_url"]).strip().rstrip("/")
    if not re.match(r"^https?://", url):
        raise AIError("The address must start with http:// or https://")
    return kind, url


def _key(src):
    key = (src.get("api_key") or "").strip()
    if not key and KINDS.get(src.get("kind"), {}).get("needs_key"):
        raise AIError(f"Add an API key for {src.get('name') or src.get('kind')} in the AI tab first.")
    return key


def list_models(src):
    """Returns the model names the source offers, newest services first where possible."""
    kind, url = _base(src)
    key = _key(src)
    if kind == "anthropic":
        j = _get(url + "/v1/models?limit=100", {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return [m["id"] for m in j.get("data", []) if m.get("id")]
    if kind == "gemini":
        j = _get(url + "/models?pageSize=200&key=" + urllib.parse.quote(key), {})
        return [m["name"].split("/", 1)[-1] for m in j.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])]
    if kind == "ollama":
        j = _get(url + "/api/tags", {})
        return [m["name"] for m in j.get("models", [])]
    j = _get(url + "/models", {"Authorization": f"Bearer {key}"} if key else {})
    return sorted(m["id"] for m in j.get("data", []) if m.get("id"))


def chat(src, system, prompt, images=(), model=None, max_tokens=800):
    """Sends one question, with optional JPEG pictures (bytes), and returns the reply text."""
    kind, url = _base(src)
    key = _key(src)
    model = (model or src.get("model") or KINDS[kind]["model"]).strip()
    if not model:
        raise AIError("Choose a model for this AI source first.")
    pics = [base64.b64encode(i).decode() for i in images if i]

    if kind == "anthropic":
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": p}}
                   for p in pics] + [{"type": "text", "text": prompt}]
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
        if system:
            body["system"] = system
        j = _post(url + "/v1/messages", body, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()

    if kind == "gemini":
        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": p}} for p in pics] + [{"text": prompt}]
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        j = _post(f"{url}/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(key)}",
                  body, {})
        cands = j.get("candidates") or []
        if not cands:
            reason = (j.get("promptFeedback") or {}).get("blockReason")
            raise AIError(f"Gemini gave no answer{f' ({reason})' if reason else ''}.")
        return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])).strip()

    if kind == "ollama":
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msg = {"role": "user", "content": prompt}
        if pics:
            msg["images"] = pics
        msgs.append(msg)
        j = _post(url + "/api/chat", {"model": model, "messages": msgs, "stream": False,
                                      "options": {"num_predict": max_tokens}}, {}, timeout=300)
        return (j.get("message") or {}).get("content", "").strip()

    # OpenAI and everything compatible with it.
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + p}} for p in pics]
    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": content if pics else prompt}]
    j = _post(url + "/chat/completions", {"model": model, "messages": msgs, "max_tokens": max_tokens},
              {"Authorization": f"Bearer {key}"} if key else {})
    choices = j.get("choices") or []
    if not choices:
        raise AIError("The AI service gave no answer.")
    c = choices[0].get("message", {}).get("content")
    if isinstance(c, list):
        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return (c or "").strip()


def parse_json(text):
    """Pulls the first JSON object out of a reply, even if it's wrapped in ``` fences or prose."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None
