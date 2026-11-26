"""Home and away: send phone alerts only when nobody is home.

Each phone with the app reports whether it is at home (from a circle drawn around the house).
While anyone is home, motion alerts are held back. Recording carries on as normal either way.

Settings (config.json, "presence"):
  mode         "auto" (follow the phones), "home" or "away" (set by hand)
  home_alerts  what still alerts while someone is home: "none" or "people"
  schedule     times when alerts are on even if someone is home, such as overnight:
               [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "23:00", "end": "06:30"}]  (0 is Monday)
  phones       filled in by the phones: {id: {"name", "home", "seen", "changed"}}
"""
import copy
import datetime
import logging
import re
import threading
import time

log = logging.getLogger("camhub.presence")

# A phone that hasn't checked in for this long no longer counts, so a lost or flat phone
# can't keep alerts switched off. The app checks in every few hours.
STALE_SECONDS = 12 * 3600
ALERT_TYPES = ("clip_started", "person")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULT_PRESENCE = {
    "mode": "auto",
    "home_alerts": "none",
    "schedule": [],
    "phones": {},
}


def _minutes(hhmm):
    m = TIME_RE.match(hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def clean_schedule(rows):
    """Checks schedule rows from the viewer. Returns the rows to keep."""
    out = []
    for r in (rows or [])[:20]:
        if not isinstance(r, dict):
            continue
        start, end = str(r.get("start", "")).strip(), str(r.get("end", "")).strip()
        if _minutes(start) is None or _minutes(end) is None or start == end:
            continue
        days = sorted({int(d) for d in r.get("days", []) if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6})
        if days:
            out.append({"days": days, "start": start, "end": end})
    return out


def in_schedule(schedule, now):
    """True if `now` (a local datetime) falls in one of the rows. Rows may run past midnight."""
    t = now.hour * 60 + now.minute
    today = now.weekday()
    yesterday = (today - 1) % 7
    for r in schedule or []:
        s, e = _minutes(r.get("start")), _minutes(r.get("end"))
        if s is None or e is None:
            continue
        days = r.get("days", [])
        if s < e:
            if today in days and s <= t < e:
                return True
        else:  # overnight, e.g. 23:00 to 06:30
            if today in days and t >= s:
                return True
            if yesterday in days and t < e:
                return True
    return False


class Presence:
    def __init__(self, config, events=None, clock=time.time, local_now=datetime.datetime.now):
        self.config = config
        self.events = events
        self.clock = clock
        self.local_now = local_now
        self._lock = threading.RLock()
        cur = config.data.get("presence") or {}
        merged = copy.deepcopy(DEFAULT_PRESENCE)
        merged.update({k: v for k, v in cur.items() if k in DEFAULT_PRESENCE})
        if merged["mode"] not in ("auto", "home", "away"):
            merged["mode"] = "auto"
        if merged["home_alerts"] not in ("none", "people"):
            merged["home_alerts"] = "none"
        merged["schedule"] = clean_schedule(merged["schedule"])
        if not isinstance(merged["phones"], dict):
            merged["phones"] = {}
        config.data["presence"] = merged
        self._last_state = None

    @property
    def data(self):
        return self.config.data["presence"]

    # ---- working out the state ----------------------------------------------------

    def _active_phones(self, now):
        return {pid: p for pid, p in self.data["phones"].items()
                if now - float(p.get("seen", 0)) < STALE_SECONDS}

    def state(self):
        """Who's home, and which alerts are on right now."""
        with self._lock:
            now = self.clock()
            d = self.data
            active = self._active_phones(now)
            home_names = [p.get("name") or "A phone" for p in active.values() if p.get("home")]
            if d["mode"] == "home":
                where, reason = "home", "Set to Home by hand"
            elif d["mode"] == "away":
                where, reason = "away", "Set to Away by hand"
            elif not active:
                where = "away"
                reason = ("No phone is sharing its location yet, so alerts stay on" if not d["phones"]
                          else "No phone has checked in lately, so alerts stay on")
            elif home_names:
                where = "home"
                reason = (f"{home_names[0]} is home" if len(home_names) == 1
                          else f"{', '.join(home_names[:-1])} and {home_names[-1]} are home")
            else:
                where, reason = "away", "Everyone is out"
            alert_hours = in_schedule(d["schedule"], self.local_now())
            if where == "away":
                alerts = "all"
            elif alert_hours:
                alerts = "all"
                reason += ", but it's alert hours"
            else:
                alerts = d["home_alerts"]
            phones = [{"id": pid, "name": p.get("name") or "Phone", "home": bool(p.get("home")),
                       "seen": p.get("seen"), "changed": p.get("changed"),
                       "stale": pid not in active}
                      for pid, p in sorted(d["phones"].items(), key=lambda kv: kv[1].get("name") or "")]
            return {"mode": d["mode"], "where": where, "reason": reason, "alerts": alerts,
                    "alert_hours": alert_hours, "home_alerts": d["home_alerts"],
                    "schedule": list(d["schedule"]), "phones": phones}

    def should_alert(self, event):
        alerts = self.state()["alerts"]
        if alerts == "all":
            return True
        if alerts == "people":
            return event.get("type") == "person" or bool(event.get("person"))
        return False

    def tag_event(self, event):
        """Marks motion events with whether the phone should alert. Called for every event."""
        if event.get("type") in ALERT_TYPES and "alert" not in event:
            event["alert"] = self.should_alert(event)
        return event

    def _announce(self):
        """Tells the viewer and phones when home/away or the alerts change."""
        st = self.state()
        key = (st["where"], st["alerts"])
        if key != self._last_state:
            self._last_state = key
            log.info("Home/away: %s, alerts %s (%s)", st["where"], st["alerts"], st["reason"])
            if self.events is not None:
                self.events.publish({"type": "presence", "where": st["where"], "alerts": st["alerts"],
                                     "reason": st["reason"]})

    def check(self):
        """Called now and then, to notice alert hours starting or ending and phones going quiet."""
        self._announce()

    # ---- changes --------------------------------------------------------------------

    def report(self, phone_id, name, home):
        """A phone says it arrived home or left. Arriving or leaving also ends a mode set by hand."""
        phone_id = re.sub(r"[^A-Za-z0-9-]", "", str(phone_id or ""))[:64]
        if not phone_id:
            raise ValueError("Missing phone id")
        name = (str(name or "").strip() or "Phone")[:40]
        home = bool(home)
        with self._lock:
            now = self.clock()
            phones = self.data["phones"]
            old = phones.get(phone_id)
            changed = old is None or bool(old.get("home")) != home
            phones[phone_id] = {"name": name, "home": home, "seen": now,
                                "changed": now if changed else old.get("changed", now)}
            if changed and old is not None and self.data["mode"] != "auto":
                log.info("%s %s, so home/away goes back to automatic", name, "arrived home" if home else "left")
                self.data["mode"] = "auto"
            self.config.save()
        self._announce()
        return self.state()

    def forget_phone(self, phone_id):
        with self._lock:
            found = self.data["phones"].pop(str(phone_id), None) is not None
            if found:
                self.config.save()
        self._announce()
        return found

    def update(self, body):
        """Changes from the viewer: mode, home_alerts and schedule (any of them)."""
        with self._lock:
            d = self.data
            if "mode" in body:
                if body["mode"] not in ("auto", "home", "away"):
                    raise ValueError("Mode must be auto, home or away")
                d["mode"] = body["mode"]
            if "home_alerts" in body:
                if body["home_alerts"] not in ("none", "people"):
                    raise ValueError("Choose none or people")
                d["home_alerts"] = body["home_alerts"]
            if "schedule" in body:
                d["schedule"] = clean_schedule(body["schedule"])
            self.config.save()
        self._announce()
        return self.state()
