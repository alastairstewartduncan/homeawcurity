"""AI for recordings: a one-line description of each clip, a morning digest, and
"Ask your recordings" (questions in plain words answered from those descriptions).

Settings live in config.json under ai.recordings:
    {"summaries": bool, "source": id, "model": str, "digest": bool, "digest_time": "08:00"}
"""
import collections
import datetime
import json
import logging
import os
import queue
import re
import threading
import time

from . import ai

log = logging.getLogger("camhub.recordings_ai")

DEFAULTS = {"summaries": False, "source": "", "model": "", "digest": False, "digest_time": "08:00"}
TAGS = ["person", "delivery", "visitor", "unknown person", "vehicle", "animal", "suspicious", "nothing"]

SUMMARY_SYSTEM = (
    "You describe short home security camera recordings for the owner. You get a few pictures taken "
    "across one recording, in time order. Reply with only a JSON object:\n"
    '{"summary": "one plain sentence, under 140 characters, saying who or what was there and what they did", '
    f'"tags": [zero or more of {json.dumps(TAGS)}]}}\n'
    "Be factual and specific (clothing colour, parcel, van, cat). Don't guess names or identities. If nothing "
    'of interest is visible (wind, light change), say so and use the tag "nothing". Use "unknown person" '
    "for anyone who isn't obviously a courier or postal worker, and \"suspicious\" only for clearly odd behaviour."
)


def clean_settings(b, source_ids=None):
    out = dict(DEFAULTS)
    out["summaries"] = bool(b.get("summaries"))
    out["digest"] = bool(b.get("digest"))
    out["source"] = str(b.get("source") or "")
    if source_ids is not None and out["source"] not in source_ids:
        out["source"] = ""
    out["model"] = str(b.get("model") or "").strip()[:120]
    t = str(b.get("digest_time") or "08:00")
    out["digest_time"] = t if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t) else "08:00"
    return out


def clip_frames(path, count=3, width=640):
    """A few JPEG pictures spread through a clip, read with OpenCV."""
    try:
        import cv2
    except ImportError:
        return []
    cap = cv2.VideoCapture(path)
    frames = []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        spots = [int(total * f) for f in ((0.15, 0.5, 0.85) if count == 3 else
                                          [(i + 1) / (count + 1) for i in range(count)])] if total > 0 else []
        for pos in spots:
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ok, frame = cap.read()
            if not ok or frame is None or frame.size == 0:
                continue
            h, w = frame.shape[:2]
            if w > width:
                frame = cv2.resize(frame, (width, int(h * width / w)))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                frames.append(buf.tobytes())
    finally:
        cap.release()
    return frames


class RecordingAI:
    def __init__(self, runner):
        self.runner = runner
        self.hub = runner.hub
        self.config = runner.config
        self._q = queue.Queue(maxsize=200)
        self._queued = set()
        self._stop = threading.Event()
        self.digests = collections.deque(maxlen=30)
        self._digest_path = os.path.join(os.path.dirname(self.config.path), "ai_digests.jsonl")
        self._load_digests()
        sec = runner.section
        sec["recordings"] = clean_settings(sec.get("recordings") or {})

    @property
    def settings(self):
        return self.runner.section["recordings"]

    def _source(self):
        src = self.runner.source(self.settings.get("source"))
        return src, (self.settings.get("model") or None)

    # ---- background -------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._work, name="ai-recordings", daemon=True).start()
        threading.Thread(target=self._digest_clock, name="ai-digest", daemon=True).start()

    def stop(self):
        self._stop.set()

    def on_event(self, e):
        if e.get("type") == "clip_saved" and e.get("clip") and self.settings.get("summaries"):
            self.queue_summary(e["clip"])

    def queue_summary(self, clip_id):
        if clip_id in self._queued:
            return False
        try:
            self._q.put_nowait(clip_id)
            self._queued.add(clip_id)
            return True
        except queue.Full:
            return False

    def pending(self):
        return self._q.qsize()

    def _work(self):
        while not self._stop.is_set():
            try:
                clip_id = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.describe(clip_id)
            except ai.AIError as e:
                log.warning("Couldn't describe %s: %s", clip_id, e)
            except Exception:
                log.exception("Couldn't describe %s", clip_id)
            finally:
                self._queued.discard(clip_id)

    # ---- one recording ------------------------------------------------------------

    def describe(self, clip_id):
        """Writes a one-line AI description and tags into the clip's details. Returns them."""
        storage = self.hub.storage
        base = storage.clip_base(clip_id)
        if not base or not os.path.isfile(base + ".mp4"):
            raise ai.AIError("Recording not found")
        pics = clip_frames(base + ".mp4")
        if not pics and os.path.isfile(base + ".jpg"):
            with open(base + ".jpg", "rb") as f:
                pics = [f.read()]
        if not pics:
            raise ai.AIError("Couldn't read any pictures from this recording")
        meta = storage.clip_meta(clip_id)
        cam = clip_id.split("_")[0]
        cam_cfg = self.config.camera(cam) or {}
        start = meta.get("start") or ""
        prompt = (f"Camera: {cam_cfg.get('name') or cam}\nStarted: {start}\n"
                  f"Length: {round(meta.get('duration') or 0)} seconds\n"
                  f"Person detector said: {'a person' if meta.get('person') else 'movement only'}")
        src, model = self._source()
        text = ai.chat(src, SUMMARY_SYSTEM, prompt, pics, model=model, max_tokens=300)
        reply = ai.parse_json(text) or {"summary": text.strip().splitlines()[0] if text.strip() else ""}
        summary = str(reply.get("summary") or "").strip()[:200]
        tags = [t for t in (reply.get("tags") or []) if t in TAGS]
        storage.update_meta(clip_id, {"ai_summary": summary, "ai_tags": tags,
                                      "ai_model": model or src.get("model") or ""})
        self.hub.events.publish({"type": "clip_described", "clip": clip_id, "camera": cam,
                                 "summary": summary, "tags": tags})
        return {"summary": summary, "tags": tags}

    # ---- clips in a time range ---------------------------------------------------

    def clips_between(self, start, end):
        storage = self.hub.storage
        out = []
        day = start.date()
        while day <= end.date():
            for c in storage.clips(day.isoformat()):
                try:
                    t = datetime.datetime.fromisoformat(c["start"])
                except ValueError:
                    continue
                if start <= t <= end:
                    out.append(c)
            day += datetime.timedelta(days=1)
        out.sort(key=lambda c: c["start"])
        return out

    # ---- morning digest ------------------------------------------------------------

    def _digest_clock(self):
        last_day = None
        while not self._stop.wait(30):
            s = self.settings
            if not s.get("digest"):
                continue
            now = datetime.datetime.now()
            if now.strftime("%H:%M") < s.get("digest_time", "08:00") or last_day == now.date():
                continue
            if any(d.get("date") == now.date().isoformat() and not d.get("manual") for d in self.digests):
                last_day = now.date()
                continue
            last_day = now.date()
            try:
                self.make_digest()
            except Exception as e:
                log.warning("Morning digest failed: %s", e)

    def make_digest(self, hours=24, manual=False):
        end = datetime.datetime.now()
        start = end - datetime.timedelta(hours=hours)
        clips = self.clips_between(start, end)
        counts = collections.Counter(t for c in clips for t in (c.get("tags") or []))
        lines = []
        for c in clips:
            t = c["start"][11:16]
            cam = (self.config.camera(c["camera"]) or {}).get("name") or c["camera"]
            lines.append(f"{t} {cam}: {c.get('summary') or ('person seen' if c.get('person') else 'movement')}"
                         f" [id {c['id']}]")
        if not clips:
            text = "Quiet night: nothing was recorded in the last 24 hours."
            highlights = []
        else:
            src, model = self._source()
            system = ("You write a short morning digest of a home's security camera recordings for the owner. "
                      "Reply with only JSON: {\"headline\": \"one line under 100 characters counting what "
                      "mattered, like '2 deliveries, 1 unknown person, 3 cars'\", \"details\": \"two to four short "
                      "sentences, most important first, with times\", \"highlights\": [up to 4 recording ids worth "
                      "watching]}. Ignore recordings that show nothing. Don't invent anything.")
            prompt = (f"Recordings from {start:%A %H:%M} to {end:%A %H:%M} ({len(clips)} in total):\n"
                      + "\n".join(lines[-300:]))
            reply = ai.parse_json(ai.chat(src, system, prompt, model=model, max_tokens=600)) or {}
            headline = str(reply.get("headline") or "").strip()[:140]
            details = str(reply.get("details") or "").strip()[:1000]
            text = headline or f"{len(clips)} recordings"
            ids = {c["id"] for c in clips}
            highlights = [h for h in (reply.get("highlights") or []) if h in ids][:4]
            if details:
                text = f"{text}\n{details}"
        undescribed = sum(1 for c in clips if not c.get("summary"))
        d = {"date": end.date().isoformat(), "time": end.isoformat(timespec="seconds"), "count": len(clips),
             "tags": dict(counts), "text": text, "highlights": highlights, "manual": manual,
             "undescribed": undescribed}
        self.digests.append(d)
        try:
            with open(self._digest_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(d) + "\n")
        except OSError:
            pass
        self.hub.events.publish({"type": "ai_digest", "text": text, "count": len(clips),
                                 "clip": highlights[0] if highlights else "", "time": d["time"]})
        return d

    def _load_digests(self):
        try:
            with open(self._digest_path, "r", encoding="utf-8") as f:
                for line in f.readlines()[-30:]:
                    try:
                        self.digests.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass

    # ---- ask your recordings --------------------------------------------------------

    def ask(self, question, days=7):
        end = datetime.datetime.now()
        start = end - datetime.timedelta(days=days)
        clips = self.clips_between(start, end)
        if not clips:
            return {"answer": f"There are no recordings from the last {days} days.", "clips": [],
                    "searched": 0, "undescribed": 0}
        lines = []
        for c in clips[-600:]:
            cam = (self.config.camera(c["camera"]) or {}).get("name") or c["camera"]
            desc = c.get("summary") or ("person seen (no AI description yet)" if c.get("person")
                                        else "movement (no AI description yet)")
            tags = f" ({', '.join(c['tags'])})" if c.get("tags") else ""
            lines.append(f"[{c['id']}] {c['start'][:16].replace('T', ' ')} {cam}: {desc}{tags}")
        src, model = self._source()
        system = (
            "You answer questions about a home's security camera recordings, using only the list of recordings "
            "given (each line: [id] date time camera: description). Today is "
            f"{end:%A %d %B %Y}, time now {end:%H:%M}. Reply with only JSON: "
            '{"answer": "a short, direct answer in plain English with days and times", '
            '"clips": [ids of the matching recordings, best first, at most 8]}. '
            "If nothing matches, say so plainly and return no clips. Don't invent recordings.")
        reply = ai.parse_json(ai.chat(src, system, f"Recordings:\n" + "\n".join(lines) +
                                      f"\n\nQuestion: {question}", model=model, max_tokens=700)) or {}
        ids = {c["id"]: c for c in clips}
        found = [ids[i] for i in (reply.get("clips") or []) if isinstance(i, str) and i in ids][:8]
        return {"answer": str(reply.get("answer") or "The AI didn't give an answer.")[:1500], "clips": found,
                "searched": len(clips), "undescribed": sum(1 for c in clips if not c.get("summary"))}

    def describe_missing(self, days=1, limit=50):
        """Queues recordings from the last few days that don't have a description yet."""
        end = datetime.datetime.now()
        clips = self.clips_between(end - datetime.timedelta(days=days), end)
        n = 0
        for c in reversed(clips):
            if n >= limit:
                break
            if not c.get("summary") and self.queue_summary(c["id"]):
                n += 1
        return n


def wait_until_idle(rec_ai, timeout=30):
    """Test helper: waits for queued descriptions to finish."""
    deadline = time.time() + timeout
    while (rec_ai._queued or not rec_ai._q.empty()) and time.time() < deadline:
        time.sleep(0.05)
