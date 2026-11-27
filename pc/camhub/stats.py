"""Stats: when things happen, who comes, where the movement is, and how the system is doing.

- Activity: every recording, person, family member, stranger, vehicle and parcel is counted
  by hour (from the live events), so the Stats tab can show the busiest times of the week and
  visitors per day. Counts are kept after the recordings themselves are deleted.
- Hot spots: while something moves, where in the picture it moved is added to a small grid
  for each camera (64 x 36 cells). Old movement fades with a half-life of 30 days.
- System health: every 5 minutes, whether each camera is online, how many pictures a second
  arrive, how long it takes to answer on the network, and (if the camera reports it over
  ONVIF) its Wi-Fi signal. Every hour, the free disk space.
- Weekly report: on Monday morning the phone gets a short summary of the week before.

Everything stays on this PC: stats/stats.db and stats/heat_<camera>.npy next to config.json.
These are small and written rarely (a few rows every 5 minutes, hot spots every 10 minutes).

Settings in config.json under "stats": {"weekly_report": true, "weekly_time": "08:00"}.
"""
import datetime
import logging
import os
import queue
import re
import socket
import sqlite3
import threading
import time
import urllib.parse

import cv2
import numpy as np

log = logging.getLogger("camhub.stats")

DEFAULTS = {"weekly_report": True, "weekly_time": "08:00"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SAMPLE_SECONDS = 300
DISK_SECONDS = 3600
HEAT_SAVE_SECONDS = 600
HEAT_W, HEAT_H = 64, 36
HEAT_HALF_LIFE_DAYS = 30
KEEP_DAYS = 400               # activity counts; health samples are kept 90 days
HEALTH_KEEP_DAYS = 90
SIGNAL_NAMES = {"none": 0, "very bad": 1, "bad": 2, "good": 3, "very good": 4}
KINDS = ("activity", "person", "family", "stranger", "vehicle", "parcel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS counts (
    hour TEXT NOT NULL, camera TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour, camera, kind)
);
CREATE TABLE IF NOT EXISTS recorded (
    day TEXT NOT NULL, camera TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, camera)
);
CREATE TABLE IF NOT EXISTS samples (
    t INTEGER NOT NULL, camera TEXT NOT NULL, online INTEGER, fps REAL, rtt REAL, signal INTEGER
);
CREATE INDEX IF NOT EXISTS samples_t ON samples(t);
CREATE TABLE IF NOT EXISTS disk (t INTEGER PRIMARY KEY, free INTEGER, total INTEGER, recordings INTEGER);
CREATE TABLE IF NOT EXISTS reports (week TEXT PRIMARY KEY, made TEXT, title TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def clean_settings(body, current=None):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (current or {}).items() if k in DEFAULTS})
    body = body if isinstance(body, dict) else {}
    if "weekly_report" in body:
        out["weekly_report"] = bool(body["weekly_report"])
    if "weekly_time" in body and TIME_RE.match(str(body["weekly_time"])):
        out["weekly_time"] = str(body["weekly_time"])
    return out


def kinds_of(event):
    """What an event counts as: a list of kinds (see KINDS)."""
    t = event.get("type")
    if t == "clip_started":
        if event.get("trigger") == "continued":
            return []
        return ["activity", "person"] if event.get("person") else ["activity"]
    if t == "person":
        return ["person"]
    if t == "known_person":
        return ["family"]
    if t in ("stranger", "returning_stranger"):
        return ["stranger"]
    if t == "vehicle":
        return ["vehicle"]
    if t == "parcel_arrived":
        return ["parcel"]
    return []


def _hour_of(event):
    try:
        when = datetime.datetime.fromisoformat(str(event.get("time")))
    except (TypeError, ValueError):
        when = datetime.datetime.now()
    return when.strftime("%Y-%m-%dT%H")


def people_count(row):
    """Visitors for one day or week. Face recognition (when on) counts each visit as family or
    stranger; otherwise a recording with a person in it is the best guide."""
    return max(row.get("family", 0) + row.get("stranger", 0), row.get("person", 0))


def week_start(day):
    return day - datetime.timedelta(days=day.weekday())


def fmt_hour(h):
    return datetime.time(h).strftime("%I%p").lstrip("0").lower()


def tcp_time(host, port=554, timeout=2.0):
    """How long the camera takes to answer on the network, in milliseconds (None: no answer)."""
    if not host:
        return None
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return round((time.perf_counter() - t0) * 1000, 1)


def camera_host(cfg):
    host = (cfg.get("onvif") or {}).get("host")
    if host:
        return host
    try:
        return urllib.parse.urlsplit(cfg.get("main_url") or "").hostname
    except ValueError:
        return None


def wifi_signal(ptz):
    """The camera's Wi-Fi signal from ONVIF (0 none to 4 very good), or None if it doesn't say.

    Many cameras don't support this. ptz is a ptz.OnvifPtz (it holds the login and clock offset).
    """
    if ptz is None or not ptz.host:
        return None
    xml = ptz._call(ptz.device_url, "<tds:GetNetworkInterfaces/>", timeout=4)
    tokens = re.findall(r'<(?:\w+:)?NetworkInterfaces[^>]*token="([^"]+)"', xml)
    for tok in tokens:
        try:
            reply = ptz._call(ptz.device_url,
                              f"<tds:GetDot11Status><tds:InterfaceToken>{tok}</tds:InterfaceToken></tds:GetDot11Status>",
                              timeout=4)
        except Exception:
            continue
        m = re.search(r"<(?:\w+:)?SignalStrength>\s*([^<]+?)\s*</", reply)
        if m:
            return SIGNAL_NAMES.get(m.group(1).strip().lower())
    return None


class HeatMap:
    """Where movement happens in one camera's picture."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.grid = np.zeros((HEAT_H, HEAT_W), np.float32)
        self.changed = False
        try:
            g = np.load(path)
            if g.shape == self.grid.shape:
                self.grid = g.astype(np.float32)
        except (OSError, ValueError):
            pass

    def add(self, mask):
        """mask: the motion detector's picture of what moved (non-zero = moved)."""
        if mask is None or mask.size == 0:
            return
        small = cv2.resize((mask > 0).astype(np.float32), (HEAT_W, HEAT_H), interpolation=cv2.INTER_AREA)
        with self._lock:
            self.grid += small
            self.changed = True

    def fade(self, seconds):
        with self._lock:
            self.grid *= np.float32(0.5 ** (seconds / (HEAT_HALF_LIFE_DAYS * 86400)))

    def save(self):
        with self._lock:
            if not self.changed:
                return
            grid = self.grid.copy()
            self.changed = False
        tmp = self.path + ".tmp.npy"
        np.save(tmp, grid)
        os.replace(tmp, self.path)

    def clear(self):
        with self._lock:
            self.grid[:] = 0
            self.changed = True
        self.save()

    def picture(self, width=640, height=360):
        """A see-through PNG to lay over the camera picture: brighter and warmer where more moved."""
        with self._lock:
            g = self.grid.copy()
        top = float(np.percentile(g, 99.5)) if g.any() else 0.0
        rgba = np.zeros((height, width, 4), np.uint8)
        if top > 0:
            v = np.clip(g / top, 0, 1)
            v = cv2.resize(v, (width, height), interpolation=cv2.INTER_CUBIC)
            v = np.clip(cv2.GaussianBlur(v, (0, 0), width / 90), 0, 1)
            colours = cv2.applyColorMap((v * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)   # BGR
            rgba[..., :3] = colours[..., ::-1]
            alpha = np.where(v < 0.06, 0, np.power(v, 0.6) * 200)
            rgba[..., 3] = alpha.astype(np.uint8)
        ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
        return buf.tobytes() if ok else b""

    def total(self):
        with self._lock:
            return float(self.grid.sum())


class Stats:
    def __init__(self, hub, folder=None, threaded=True):
        self.hub = hub
        self.config = hub.config
        self.folder = folder or os.path.join(os.path.dirname(os.path.abspath(self.config.path)), "stats")
        os.makedirs(self.folder, exist_ok=True)
        self.config.data["stats"] = clean_settings({}, self.config.data.get("stats"))
        self._lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(self.folder, "stats.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.heat = {}
        self._frames = {}            # camera id -> (frame id, time) at the last sample
        self._signal_next = {}       # camera id -> when to ask the camera for its Wi-Fi signal again
        self._stop = threading.Event()
        self._events = None
        self._threaded = threaded
        self._summary_cache = {}

    @property
    def settings(self):
        return self.config.data["stats"]

    # ---- running ---------------------------------------------------------------------

    def start(self):
        self._events = self.hub.events.subscribe()
        threading.Thread(target=self._loop, name="stats", daemon=True).start()
        threading.Thread(target=self._backfill_quietly, name="stats-backfill", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._events is not None:
            self.hub.events.unsubscribe(self._events)
        for h in list(self.heat.values()):
            try:
                h.save()
            except OSError:
                pass

    def _loop(self):
        last_sample = time.monotonic() - SAMPLE_SECONDS + 60   # first sample a minute after start
        last_disk = 0.0
        last_heat = time.monotonic()
        while not self._stop.wait(5):
            try:
                self.drain()
                now = time.monotonic()
                if now - last_sample >= SAMPLE_SECONDS:
                    last_sample = now
                    self.sample_cameras()
                if now - last_disk >= DISK_SECONDS:
                    last_disk = now
                    self.sample_disk()
                    self.tidy()
                if now - last_heat >= HEAT_SAVE_SECONDS:
                    for h in list(self.heat.values()):
                        h.fade(now - last_heat)
                        h.save()
                    last_heat = now
                self.weekly_tick()
            except Exception:
                log.exception("Stats update failed")

    def drain(self):
        while self._events is not None:
            try:
                e = self._events.get_nowait()
            except queue.Empty:
                return
            self.on_event(e)

    # ---- counting --------------------------------------------------------------------

    def on_event(self, e):
        if e.get("type") == "clip_saved" and e.get("meta"):
            self._add_recorded(e)
            return
        kinds = kinds_of(e)
        if not kinds:
            return
        hour, cam = _hour_of(e), str(e.get("camera") or "")
        with self._lock:
            for k in kinds:
                self.db.execute("INSERT INTO counts (hour, camera, kind, n) VALUES (?, ?, ?, 1) "
                                "ON CONFLICT(hour, camera, kind) DO UPDATE SET n = n + 1", (hour, cam, k))
            self.db.commit()
        self._summary_cache.clear()

    def _add_recorded(self, e):
        base = self.hub.storage.clip_base(e.get("clip"))
        try:
            size = os.path.getsize(base + ".mp4") if base else 0
        except OSError:
            size = 0
        day = str(e["meta"].get("start") or "")[:10] or datetime.date.today().isoformat()
        with self._lock:
            self.db.execute("INSERT INTO recorded (day, camera, bytes) VALUES (?, ?, ?) "
                            "ON CONFLICT(day, camera) DO UPDATE SET bytes = bytes + excluded.bytes",
                            (day, str(e.get("camera") or ""), size))
            self.db.commit()

    def _backfill_quietly(self):
        try:
            self.backfill()
        except Exception:
            log.exception("Couldn't read the existing recordings for the stats")

    def backfill(self):
        """The first time: count the recordings already on disk, so the charts aren't empty."""
        with self._lock:
            if self.db.execute("SELECT v FROM meta WHERE k = 'backfilled'").fetchone():
                return 0
        storage = self.hub.storage
        rows, sizes, n = {}, {}, 0
        for d in storage.days():
            for c in storage.clips(d["date"]):
                n += 1
                sizes[(d["date"], c["camera"])] = sizes.get((d["date"], c["camera"]), 0) + int(c.get("size") or 0)
                if c.get("trigger") == "continued":
                    continue
                hour = str(c["start"])[:13]
                kinds = ["activity"] + (["person"] if c.get("person") else [])
                for label in c.get("seen") or []:
                    low = str(label).lower()
                    if low in ("stranger", "returning stranger"):
                        kinds.append("stranger")
                    elif low == "parcel":
                        kinds.append("parcel")
                    elif low == "unknown vehicle":
                        kinds.append("vehicle")
                for k in kinds:
                    key = (hour, c["camera"], k)
                    rows[key] = rows.get(key, 0) + 1
        with self._lock:
            if self.db.execute("SELECT v FROM meta WHERE k = 'backfilled'").fetchone():
                return 0
            self.db.executemany("INSERT INTO counts (hour, camera, kind, n) VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(hour, camera, kind) DO UPDATE SET n = n + excluded.n",
                                [(h, c, k, v) for (h, c, k), v in rows.items()])
            self.db.executemany("INSERT OR IGNORE INTO recorded (day, camera, bytes) VALUES (?, ?, ?)",
                                [(d, c, b) for (d, c), b in sizes.items()])
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('backfilled', ?)",
                            (datetime.datetime.now().isoformat(timespec="seconds"),))
            self.db.commit()
        self._summary_cache.clear()
        if n:
            log.info("Stats: counted %d recordings already on disk", n)
        return n

    # ---- hot spots ---------------------------------------------------------------------

    def heat_for(self, cam_id):
        h = self.heat.get(cam_id)
        if h is None:
            safe = re.sub(r"[^A-Za-z0-9-]", "_", cam_id)
            h = self.heat[cam_id] = HeatMap(os.path.join(self.folder, f"heat_{safe}.npy"))
        return h

    def attach(self, cam):
        """Gives a camera somewhere to put where things moved (camera.on_motion_mask)."""
        cam.on_motion_mask = self.heat_for(cam.id).add

    # ---- health samples ----------------------------------------------------------------

    def sample_cameras(self, now=None):
        now = int(now or time.time())
        rows = []
        for cam_id, cam in list(self.hub.cameras.items()):
            try:
                st = cam.status()
            except Exception:
                continue
            online = bool(st.get("online"))
            fid = getattr(cam, "_frame_id", 0)
            prev = self._frames.get(cam_id)
            self._frames[cam_id] = (fid, time.monotonic())
            fps = None
            if prev and online:
                secs = time.monotonic() - prev[1]
                if secs > 1 and fid >= prev[0]:
                    fps = round((fid - prev[0]) / secs, 2)
            rtt = tcp_time(camera_host(cam.cfg)) if self._threaded else None
            signal = None
            if online and time.monotonic() >= self._signal_next.get(cam_id, 0):
                try:
                    signal = wifi_signal(getattr(cam, "ptz", None))
                except Exception:
                    signal = None
                # Ask every sample while the camera answers; otherwise once a day.
                self._signal_next[cam_id] = time.monotonic() + (0 if signal is not None else 86400)
            rows.append((now, cam_id, int(online), fps, rtt, signal))
        if rows:
            with self._lock:
                self.db.executemany("INSERT INTO samples (t, camera, online, fps, rtt, signal) VALUES (?, ?, ?, ?, ?, ?)", rows)
                self.db.commit()
        return rows

    def sample_disk(self, now=None):
        try:
            u = self.hub.storage.usage()
        except OSError:
            return None
        row = (int(now or time.time()), int(u["disk_free"]), int(u["disk_total"]), int(u["recordings_bytes"]))
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO disk (t, free, total, recordings) VALUES (?, ?, ?, ?)", row)
            self.db.commit()
        self._summary_cache.pop("health", None)
        return row

    def tidy(self):
        old = (datetime.datetime.now() - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H")
        cut = int(time.time() - HEALTH_KEEP_DAYS * 86400)
        with self._lock:
            self.db.execute("DELETE FROM counts WHERE hour < ?", (old,))
            self.db.execute("DELETE FROM recorded WHERE day < ?", (old[:10],))
            self.db.execute("DELETE FROM samples WHERE t < ?", (cut,))
            self.db.execute("DELETE FROM disk WHERE t < ?", (cut,))
            self.db.commit()

    # ---- reading -----------------------------------------------------------------------

    def _counts(self, first_day, last_day, camera=None):
        sql = "SELECT hour, kind, SUM(n) n FROM counts WHERE hour >= ? AND hour < ?"
        args = [first_day.isoformat(), (last_day + datetime.timedelta(days=1)).isoformat()]
        if camera:
            sql += " AND camera = ?"
            args.append(camera)
        with self._lock:
            return self.db.execute(sql + " GROUP BY hour, kind", args).fetchall()

    def activity(self, days=7, camera=None, today=None):
        """Charts for the last `days` days: busiest times of the week, and visitors per day."""
        days = max(1, min(365, int(days)))
        today = today or datetime.date.today()
        first = today - datetime.timedelta(days=days - 1)
        grid = {k: [[0] * 24 for _ in range(7)] for k in ("activity", "people")}
        per_day = {(first + datetime.timedelta(days=i)).isoformat(): {k: 0 for k in KINDS} for i in range(days)}
        per_hour = {}
        for r in self._counts(first, today, camera):
            day = r["hour"][:10]
            if day in per_day:
                per_day[day][r["kind"]] += r["n"]
            per_hour.setdefault(r["hour"], {k: 0 for k in KINDS})[r["kind"]] += r["n"]
        for hour, c in per_hour.items():
            d = datetime.date.fromisoformat(hour[:10])
            h = int(hour[11:13])
            grid["activity"][d.weekday()][h] += c["activity"]
            grid["people"][d.weekday()][h] += people_count(c)
        daily = []
        for day, c in per_day.items():
            daily.append({"day": day, **c, "people": people_count(c)})
        totals = {k: sum(d[k] for d in daily) for k in KINDS + ("people",)}
        return {"days": days, "from": first.isoformat(), "to": today.isoformat(), "camera": camera or "",
                "heatmap": grid, "daily": daily, "totals": totals, "busiest": busiest(per_hour),
                "face_recognition": totals["family"] + totals["stranger"] > 0}

    def health(self, now=None):
        now = int(now or time.time())
        cams = []
        with self._lock:
            samples = self.db.execute("SELECT t, camera, online, fps, rtt, signal FROM samples WHERE t >= ? "
                                      "ORDER BY t", (now - 7 * 86400,)).fetchall()
            disk_rows = self.db.execute("SELECT t, free, total, recordings FROM disk WHERE t >= ? ORDER BY t",
                                        (now - 30 * 86400,)).fetchall()
            rec = self.db.execute("SELECT day, SUM(bytes) b FROM recorded WHERE day >= ? GROUP BY day",
                                  ((datetime.date.today() - datetime.timedelta(days=8)).isoformat(),)).fetchall()
        by_cam = {}
        for s in samples:
            by_cam.setdefault(s["camera"], []).append(s)
        for cam_id, cam in list(self.hub.cameras.items()):
            rows = by_cam.get(cam_id, [])
            cams.append(camera_health(cam_id, cam.cfg.get("name") or cam_id, rows, now,
                                      online_now=bool(cam.status().get("online"))))
        return {"cameras": cams, "disk": self.disk_outlook(disk_rows, rec), "sample_minutes": SAMPLE_SECONDS // 60}

    def disk_outlook(self, disk_rows, recorded_rows):
        st = self.config.storage
        try:
            u = self.hub.storage.usage()
            free, total, used = u["disk_free"], u["disk_total"], u["recordings_bytes"]
        except OSError:
            free = total = used = None
        today = datetime.date.today().isoformat()
        full_days = [r["b"] or 0 for r in recorded_rows if r["day"] < today]
        per_day = (sum(full_days) / len(full_days)) if full_days else None
        min_free = float(st.get("min_free_gb") or 0) * 1024 ** 3
        keep = int(st.get("retention_days") or 0)
        out = {"free": free, "total": total, "recordings": used, "per_day": per_day, "min_free": min_free,
               "retention_days": keep, "days_left": None, "levels_off": False,
               "history": [{"t": r["t"], "free": r["free"]} for r in disk_rows]}
        oldest = self.hub.storage.days()
        out["oldest_day"] = oldest[-1]["date"] if oldest else None
        if free is not None and per_day:
            room = max(0.0, free - min_free)
            out["days_left"] = int(room // per_day) if per_day > 0 else None
            # With "keep for N days", old recordings make way for new ones once N days are kept.
            if keep and per_day * keep <= (used or 0) + room:
                out["levels_off"] = True
        return out

    def summary(self, days=7, camera=None):
        key = (int(days), camera or "")
        hit = self._summary_cache.get(key)
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        out = self.activity(days, camera)
        self._summary_cache[key] = (time.monotonic(), out)
        return out

    # ---- weekly report -----------------------------------------------------------------

    def weekly_tick(self, now=None):
        s = self.settings
        now = now or datetime.datetime.now()
        if not s.get("weekly_report"):
            return None
        # Monday after the set time, or Tuesday if the PC was off on Monday morning.
        if now.weekday() > 1 or (now.weekday() == 0 and now.strftime("%H:%M") < s["weekly_time"]):
            return None
        week = (week_start(now.date()) - datetime.timedelta(days=7)).isoformat()
        with self._lock:
            if self.db.execute("SELECT 1 FROM reports WHERE week = ?", (week,)).fetchone():
                return None
        return self.send_weekly(now.date())

    def weekly(self, today=None):
        """The report for the last full week (Monday to Sunday) before `today`."""
        today = today or datetime.date.today()
        start = week_start(today) - datetime.timedelta(days=7)
        end = start + datetime.timedelta(days=6)
        this = self.activity(7, today=end)
        before = self.activity(7, today=start - datetime.timedelta(days=1))
        t, b = this["totals"], before["totals"]
        parts = []
        people = t["people"]
        line = f"{people} visitor{'s' if people != 1 else ''}"
        if this["face_recognition"]:
            line += f", {t['stranger']} stranger{'s' if t['stranger'] != 1 else ''}"
        parts.append(line)
        if t["vehicle"]:
            parts.append(f"{t['vehicle']} vehicle{'s' if t['vehicle'] != 1 else ''}")
        if t["parcel"]:
            parts.append(f"{t['parcel']} parcel{'s' if t['parcel'] != 1 else ''}")
        title = ", ".join(parts)
        lines = []
        if this["busiest"]:
            bz = this["busiest"]
            lines.append(f"Busiest: {bz['weekday']} {fmt_hour(bz['hour'])}.")
        lines.append(f"{t['activity']} recording{'s' if t['activity'] != 1 else ''}"
                     + (f" ({trend(t['activity'], b['activity'])})" if b["activity"] else "") + ".")
        try:
            h = self.health()
            ups = [c for c in h["cameras"] if c["uptime_7d"] is not None]
            if ups:
                worst = min(ups, key=lambda c: c["uptime_7d"])
                lines.append("Cameras online all week." if worst["uptime_7d"] >= 99.5 else
                             f"{worst['name']} was online {worst['uptime_7d']:.0f}% of the week.")
            d = h["disk"]
            if d["levels_off"]:
                lines.append(f"Disk: fine, keeps {d['retention_days']} days of recordings.")
            elif d["days_left"] is not None:
                lines.append(f"Disk: room for about {d['days_left']} more days.")
        except Exception:
            log.exception("Weekly report: health part failed")
        return {"week": start.isoformat(), "to": end.isoformat(), "title": title, "text": " ".join(lines),
                "totals": t, "before": b, "busiest": this["busiest"]}

    def send_weekly(self, today=None, test=False):
        r = self.weekly(today)
        made = datetime.datetime.now().isoformat(timespec="seconds")
        if not test:
            with self._lock:
                self.db.execute("INSERT OR REPLACE INTO reports (week, made, title, text) VALUES (?, ?, ?, ?)",
                                (r["week"], made, r["title"], r["text"]))
                self.db.commit()
        self.hub.events.publish({"type": "weekly_report", "week": r["week"], "title": r["title"],
                                 "text": r["text"], "time": made, "test": bool(test)})
        log.info("Weekly report for %s: %s. %s", r["week"], r["title"], r["text"])
        return r

    def reports(self, limit=12):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT week, made, title, text FROM reports ORDER BY week DESC LIMIT ?", (limit,))]


def trend(now, before):
    if not before:
        return "first week"
    change = round(100 * (now - before) / before)
    if abs(change) < 5:
        return "about the same as the week before"
    return f"{abs(change)}% {'more' if change > 0 else 'fewer'} than the week before"


def busiest(per_hour):
    """The busiest weekday and hour, going by people (or all activity if no people were seen)."""
    by = {}
    use_people = any(people_count(c) for c in per_hour.values())
    for hour, c in per_hour.items():
        d = datetime.date.fromisoformat(hour[:10])
        key = (d.weekday(), int(hour[11:13]))
        by[key] = by.get(key, 0) + (people_count(c) if use_people else c["activity"])
    if not by or max(by.values()) == 0:
        return None
    (wd, h), n = max(by.items(), key=lambda x: (x[1], -x[0][0], -x[0][1]))
    return {"weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][wd],
            "dow": wd, "hour": h, "count": n, "of": "people" if use_people else "activity"}


def camera_health(cam_id, name, rows, now, online_now):
    """Uptime and connection quality for one camera from its 5-minute samples."""
    def uptime(since):
        r = [s["online"] for s in rows if s["t"] >= since]
        return round(100.0 * sum(r) / len(r), 1) if r else None

    # Online since: the newest sample that was offline (samples only cover the last 7 days).
    since = None
    if online_now:
        off = [s["t"] for s in rows if not s["online"]]
        since = (off[-1] + SAMPLE_SECONDS) if off else (rows[0]["t"] if rows else None)
    # Hourly blocks for the last 7 days: share online, mean pictures a second, response time, signal.
    hours = {}
    for s in rows:
        b = hours.setdefault(s["t"] // 3600 * 3600, {"on": [], "fps": [], "rtt": [], "sig": []})
        b["on"].append(s["online"])
        if s["fps"] is not None:
            b["fps"].append(s["fps"])
        if s["rtt"] is not None:
            b["rtt"].append(s["rtt"])
        if s["signal"] is not None:
            b["sig"].append(s["signal"])
    mean = lambda v: round(sum(v) / len(v), 2) if v else None  # noqa: E731
    timeline = [{"t": t, "online": mean(b["on"]), "fps": mean(b["fps"]), "rtt": mean(b["rtt"]), "signal": mean(b["sig"])}
                for t, b in sorted(hours.items())]
    last_signal = next((s["signal"] for s in reversed(rows) if s["signal"] is not None), None)
    rtts = [s["rtt"] for s in rows if s["rtt"] is not None and s["t"] >= now - 86400]
    return {"id": cam_id, "name": name, "online": online_now, "online_since": since,
            "uptime_24h": uptime(now - 86400), "uptime_7d": uptime(now - 7 * 86400),
            "signal": last_signal, "signal_supported": any(s["signal"] is not None for s in rows),
            "rtt_24h": round(sorted(rtts)[len(rtts) // 2], 1) if rtts else None,
            "timeline": timeline}


def add_routes(app, hub):
    from fastapi import HTTPException, Request
    from fastapi.responses import Response

    st = hub.stats

    @app.get("/api/stats")
    def get_stats(days: int = 7, camera: str = None):
        out = st.summary(days, camera or None)
        return {**out, "cameras": [{"id": c.id, "name": c.cfg.get("name") or c.id} for c in hub.cameras.values()],
                "settings": st.settings, "reports": st.reports(4)}

    @app.get("/api/stats/health")
    def get_health():
        return st.health()

    @app.get("/api/stats/hotspots/{cam_id}.png")
    def hotspots(cam_id: str):
        if cam_id not in hub.cameras:
            raise HTTPException(404, "No such camera")
        return Response(st.heat_for(cam_id).picture(), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.delete("/api/stats/hotspots/{cam_id}")
    def clear_hotspots(cam_id: str):
        if cam_id not in hub.cameras:
            raise HTTPException(404, "No such camera")
        st.heat_for(cam_id).clear()
        return {"ok": True}

    @app.put("/api/stats/settings")
    async def put_settings(request: Request):
        body = await request.json()
        hub.config.data["stats"] = clean_settings(body, st.settings)
        hub.config.save()
        return st.settings

    @app.get("/api/stats/weekly")
    def preview_weekly():
        return st.weekly()

    @app.post("/api/stats/weekly/send")
    def send_weekly_now():
        """Sends last week's report to the phones now, as a test (the Monday one still goes)."""
        return st.send_weekly(test=True)
