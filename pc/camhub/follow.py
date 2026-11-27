"""Following people with the camera, and zooming in on faces.

Two things a pan, tilt and zoom camera can do by itself, both switched on in the Smart tab:

- Follow: when someone walks onto your property, the camera turns to keep them in the middle
  of the picture as they cross it, so the recording is one clear clip of them rather than
  pieces of them walking in and out of view. When they leave your property, go out of sight
  or the time limit is reached, the camera goes back to exactly where it was.
- Zoom on faces: when someone's face is too small or unclear to recognise, the camera points at
  their head and zooms in for a few seconds to get a clear picture, then zooms back out.
  Cameras without an optical zoom just centre on the person (and the picture is enhanced
  before matching, in faces.py).

Rules that always hold:
- Only people standing on your property (the "Our property" area you draw in the Smart tab)
  are followed or zoomed in on. Without that drawn, the camera never moves by itself.
- Privacy areas stay covered while the camera moves: viewmotion.py works out where they are
  in the moved picture. If it can't tell, the whole picture is covered and the camera goes
  straight back.
- Anyone in a privacy area or an ignore zone is never followed.
- Steering the camera by hand stops following at once (and it stays where you put it).

The camera is driven with ONVIF ContinuousMove (ptz.py). Before the first move, the spot it
was pointing at is saved in one of the camera's own position slots, so it can go back exactly.
"""
import logging
import queue
import threading
import time

import cv2
import numpy as np

log = logging.getLogger("camhub.follow")

DEFAULT_TRACKING = {
    "follow": False,        # turn to follow people across your property
    "zoom": False,          # zoom in on faces that are too small to recognise
    "max_seconds": 120,     # never follow for longer than this
}

DEADBAND_X = 0.13       # leave the camera still while the person is this close to the middle
DEADBAND_Y = 0.17
GAIN_X, GAIN_Y = 1.4, 1.1
MIN_SPEED, MAX_SPEED = 0.18, 0.6
LOSE_AFTER = 3.0        # seconds without seeing them before stopping
WAIT_FOR_RETURN = 5.0   # then wait this long in case they reappear, before going back
RESEND = 1.0            # repeat a move this often (some cameras stop a move after a while)
MAX_SHIFT = 1.4         # don't turn further than this many picture widths from the usual view
ZOOM_TARGET = 0.62      # zoom until the person fills this much of the picture's height
ZOOM_MAX_IN = 3.0       # seconds zooming in, at most
ZOOM_HOLD = 3.0         # seconds held zoomed in, for the face checks
ZOOM_CENTRE_TIME = 3.0  # seconds to get the head in the middle before zooming
RETURN_SETTLE = 8.0     # seconds for the camera to get back before the usual view counts again
ZOOMS_PER_PERSON = 1


def clean_tracking(body, current):
    out = dict(current or DEFAULT_TRACKING)
    body = body if isinstance(body, dict) else {}
    for k in ("follow", "zoom"):
        if k in body:
            out[k] = bool(body[k])
    if "max_seconds" in body:
        try:
            out["max_seconds"] = max(20, min(600, int(body["max_seconds"])))
        except (TypeError, ValueError):
            pass
    return out


def _inside(points, p):
    pts = np.array(points, np.float32)
    return len(pts) >= 3 and cv2.pointPolygonTest(pts, (float(p[0]), float(p[1])), False) >= 0


def _near_polygon(points, p, margin=0.04):
    """Inside the area, or just outside its edge (people's feet wobble at the boundary)."""
    pts = np.array(points, np.float32)
    if len(pts) < 3:
        return False
    return cv2.pointPolygonTest(pts, (float(p[0]), float(p[1])), True) >= -margin


def _feet(box):
    return ((box[0] + box[2]) / 2, box[3])


def _speed(err, deadband, gain):
    if abs(err) < deadband:
        return 0.0
    v = err * gain
    v = max(MIN_SPEED, min(MAX_SPEED, abs(v)))
    return v if err > 0 else -v


class Follower:
    """Follows people and zooms in on faces for one camera. Stepped by the camera's analyser
    with each picture it looks at; the camera is driven from a thread of its own, so a slow
    camera never holds up the picture."""

    def __init__(self, cam, on_save=None):
        self.cam = cam
        self.on_save = on_save          # called after the return slot is saved in the settings
        self.state = "idle"             # idle, follow, zoom, back
        self.phase = None               # while zooming: centre, in, hold, out
        self.target = None              # {"home": feet in the usual view, "box", "seen", "zooms"}
        self.started = 0.0
        self.phase_since = 0.0
        self.zoom_in_time = 0.0
        self.moved = False              # the camera has moved since we began
        self.back_since = 0.0
        self.reason = ""
        self.last_end = 0.0
        self._sent = (0.0, 0.0, 0.0)
        self._sent_at = 0.0
        self._ops = queue.Queue()
        self._move = None               # the newest move wanted: (pan, tilt, zoom)
        self._move_cond = threading.Condition()
        self._thread = None

    # ---- settings ----

    @property
    def settings(self):
        smart = self.cam.smart
        if smart is None:
            return DEFAULT_TRACKING
        return smart.hub.settings.get("tracking") or DEFAULT_TRACKING

    def enabled(self):
        s = self.settings
        ptz = self.cam.ptz
        if not (s.get("follow") or s.get("zoom")) or ptz is None or not ptz.available:
            return False
        return bool(self._property())

    def _property(self):
        smart = self.cam.smart
        if smart is None:
            return []
        return smart.rules.get("property") or []

    @property
    def active(self):
        return self.state != "idle"

    @property
    def steering(self):
        """True while we're driving the camera (motion detection can't work meanwhile)."""
        return self.state in ("follow", "zoom", "back") and (self.moved or self.state == "back")

    def status(self):
        return {"state": self.state, "phase": self.phase, "reason": self.reason}

    # ---- the camera's commands, in their own thread ----

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._drive, name=f"follow-{self.cam.id}", daemon=True)
            self._thread.start()

    def _drive(self):
        while True:
            try:
                op = self._ops.get(timeout=0.05)
            except queue.Empty:
                op = None
            if op is not None:
                try:
                    op()
                except Exception as e:
                    log.warning("[%s] Camera command failed: %s", self.cam.id, e)
                continue
            with self._move_cond:
                if not self._ops.empty():
                    continue    # saving the spot comes before any move
                move, self._move = self._move, None
            if move is None:
                continue
            try:
                if move == (0.0, 0.0, 0.0):
                    self.cam.ptz.stop()
                else:
                    self.cam.ptz.move(*move)
            except Exception as e:
                log.warning("[%s] Couldn't steer the camera: %s", self.cam.id, e)

    def _send(self, pan, tilt, zoom, now, force=False):
        want = (round(pan, 2), round(tilt, 2), round(zoom, 2))
        if not force and want == self._sent and (want == (0.0, 0.0, 0.0) or now - self._sent_at < RESEND):
            return
        self._sent, self._sent_at = want, now
        with self._move_cond:
            if want != (0.0, 0.0, 0.0) and not self.moved:
                self.moved = True
                self.cam.view.start(self.cam.raw_frame())
                self._ops.put(self._remember_spot)
            self._move = want
        self._ensure_thread()

    def _remember_spot(self):
        """Saves where the camera points now in a slot of its own, to come back to it."""
        p = self.cam.ptz_cfg
        token = p.get("return_token")
        try:
            if token:
                self.cam.ptz.update_preset(token, "A.S.D return")
            else:
                taken = {x.get("token") for x in p.get("presets") or []}
                token = self.cam.ptz.save_preset("A.S.D return", taken=taken)
                p["return_token"] = token
                if self.on_save:
                    self.on_save()
        except Exception as e:
            log.warning("[%s] Couldn't save where the camera points, it will go to its home position "
                        "afterwards: %s", self.cam.id, e)
            p.pop("return_token", None)

    def _go_back(self):
        token = self.cam.ptz_cfg.get("return_token")
        try:
            self.cam.ptz.stop()
        except Exception:
            pass
        try:
            if token:
                self.cam.ptz.goto_preset(token)
            else:
                self.cam.ptz.home()
        except Exception as e:
            log.warning("[%s] Couldn't send the camera back: %s", self.cam.id, e)

    # ---- each picture ----

    def cancel(self):
        """Someone steered by hand: stop now and leave the camera where they put it."""
        if self.state == "idle":
            return
        log.info("[%s] Following stopped: steered by hand", self.cam.id)
        self.state, self.phase, self.target, self.reason = "idle", None, None, "steered by hand"
        with self._move_cond:
            self._move = None
        self._sent = (0.0, 0.0, 0.0)
        self.moved = False
        self.last_end = time.monotonic()
        # From here the person steering decides where it points, as with any turn by hand.
        self.cam.view.stop()

    def step(self, now, objects, view_ok=True):
        """Called with each picture looked at. objects: people and vehicles found in it (each with
        "home", its box in the usual view), or None if nobody looked this time."""
        if self.state == "idle":
            if objects is not None and now - self.last_end > 4 and self.enabled():
                self._maybe_start(now, objects)
            return
        if self.state == "back":
            if now - self.back_since >= RETURN_SETTLE:
                self.cam.view.stop()
                self.state, self.phase = "idle", None
                self.last_end = now
            return
        if not self.enabled():
            return self._end(now, "switched off")
        if not view_ok:
            return self._end(now, "lost track of where the camera points")
        if now - self.started > float(self.settings.get("max_seconds") or 120):
            return self._end(now, "time limit")
        if objects is None:
            return
        person = self._find_target(now, objects)
        if person is None:
            gone = now - self.target["seen"]
            if gone > LOSE_AFTER:
                self._send(0, 0, 0, now)
                if self.state == "zoom" and self.phase in ("in", "hold"):
                    self._zoom_out(now)
                elif gone > LOSE_AFTER + WAIT_FOR_RETURN:
                    return self._end(now, "out of sight")
            return
        home_feet = _feet(person["home"])
        prop = self._property()
        if not _near_polygon(prop, home_feet) or self._private(home_feet):
            return self._end(now, "left the property")
        self.cam.keep_recording(now)
        if self.state == "zoom":
            self._zoom_step(now, person)
        else:
            self._follow_step(now, person)

    def _private(self, p):
        from . import privacy
        for m in privacy.settings_of(self.cam.cfg)["masks"]:
            if _inside(m.get("points") or [], p):
                return True
        return False

    @staticmethod
    def _people(objects):
        return [dict(o, home=o.get("home") or o["box"]) for o in objects
                if o["kind"] == "person" and not o.get("ignored")]

    def _candidates(self, objects):
        """People who may be followed: standing on the property, not in a privacy area."""
        prop = self._property()
        return [o for o in self._people(objects)
                if _inside(prop, _feet(o["home"])) and not self._private(_feet(o["home"]))]

    def _maybe_start(self, now, objects):
        people = self._candidates(objects)
        if not people:
            return
        s = self.settings
        p = max(people, key=lambda o: (o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1]))
        self.target = {"home": _feet(p["home"]), "box": p["box"], "seen": now, "zooms": 0}
        self.started, self.moved, self.reason = now, False, ""
        self._sent = (0.0, 0.0, 0.0)
        if s.get("follow"):
            self.state = "follow"
            log.info("[%s] Following someone on the property", self.cam.id)
        elif self._wants_zoom(p):
            self._begin_zoom(now)
        else:
            self.target = None

    def _find_target(self, now, objects):
        """The person we're following in this picture: the one standing nearest where they were."""
        best, best_d = None, None
        allowed = 0.25 * max(1.0, now - self.target["seen"])
        for o in self._people(objects):
            f = _feet(o["home"])
            d = ((f[0] - self.target["home"][0]) ** 2 + (f[1] - self.target["home"][1]) ** 2) ** 0.5
            if d <= allowed and (best_d is None or d < best_d):
                best, best_d = o, d
        if best is not None:
            self.target.update(home=_feet(best["home"]), box=best["box"], seen=now)
        return best

    # ---- following ----

    def _aim(self, box, head=False):
        """How far the person (or their head) is from the middle of the picture: (x, y)."""
        x1, y1, x2, y2 = box
        cx = (x1 + x2) / 2
        cy = y1 + (y2 - y1) * (0.12 if head else 0.4)
        return cx - 0.5, cy - 0.5

    def _limit(self, pan, tilt):
        """Don't turn ever further from the usual view."""
        c = self.cam.view.centre_in_home()
        if (pan < 0 and c[0] < 0.5 - MAX_SHIFT) or (pan > 0 and c[0] > 0.5 + MAX_SHIFT):
            pan = 0.0
        if (tilt > 0 and c[1] < 0.5 - MAX_SHIFT) or (tilt < 0 and c[1] > 0.5 + MAX_SHIFT):
            tilt = 0.0
        return pan, tilt

    def _follow_step(self, now, person):
        if self._wants_zoom(person):
            return self._begin_zoom(now)
        ex, ey = self._aim(person["box"])
        pan, tilt = self._limit(_speed(ex, DEADBAND_X, GAIN_X), -_speed(ey, DEADBAND_Y, GAIN_Y))
        self._send(pan, tilt, 0.0, now)

    # ---- zooming in on a face ----

    def _wants_zoom(self, person):
        if not self.settings.get("zoom") or self.target is None:
            return False
        if self.target["zooms"] >= ZOOMS_PER_PERSON:
            return False
        smart = self.cam.smart
        return bool(smart is not None and smart.wants_face_zoom(person["box"]))

    def _begin_zoom(self, now):
        self.state, self.phase, self.phase_since = "zoom", "centre", now
        self.target["zooms"] += 1
        self.zoom_in_time = 0.0
        log.info("[%s] Zooming in for a clearer look at a face", self.cam.id)

    def _zoom_step(self, now, person):
        ex, ey = self._aim(person["box"], head=True)
        tight = self.phase in ("centre", "in")
        pan = _speed(ex, 0.07 if tight else DEADBAND_X, GAIN_X * 0.8)
        tilt = -_speed(ey, 0.08 if tight else DEADBAND_Y, GAIN_Y * 0.8)
        pan, tilt = self._limit(pan, tilt)
        since = now - self.phase_since
        tall = person["box"][3] - person["box"][1]
        can_zoom = bool(getattr(self.cam.ptz, "zoom", False))
        if self.phase == "centre":
            if (pan == 0 and tilt == 0) or since > ZOOM_CENTRE_TIME:
                self.phase, self.phase_since = ("in" if can_zoom else "hold"), now
                self.cam.smart_look_now()
            self._send(pan, tilt, 0.0, now)
        elif self.phase == "in":
            if tall >= ZOOM_TARGET or since > ZOOM_MAX_IN:
                self.zoom_in_time = since
                self.phase, self.phase_since = "hold", now
                self._send(pan, tilt, 0.0, now)
                self.cam.smart_look_now()
            else:
                self._send(pan, tilt, 0.5, now)
        elif self.phase == "hold":
            self._send(pan, tilt, 0.0, now)
            smart = self.cam.smart
            done = smart is not None and not smart.wants_face_zoom(person["box"], zoomed=True)
            if done or since > ZOOM_HOLD:
                self._zoom_out(now)
        elif self.phase == "out":
            self._zoom_out_step(now)

    def _zoom_out(self, now):
        if self.phase == "out":
            return self._zoom_out_step(now)
        self.phase, self.phase_since = "out", now
        self._zoom_out_step(now)

    def _zoom_out_step(self, now):
        # Zooming out for longer than we zoomed in always ends fully zoomed out.
        need = self.zoom_in_time * 1.5 + 0.6 if self.zoom_in_time else 0
        if now - self.phase_since < need:
            self._send(0.0, 0.0, -0.6, now)
            return
        self._send(0.0, 0.0, 0.0, now)
        if self.settings.get("follow"):
            self.state, self.phase = "follow", None
        else:
            self._end(now, "face checked")

    # ---- ending ----

    def _end(self, now, reason):
        log.info("[%s] Stopped following (%s)%s", self.cam.id, reason, ", going back" if self.moved else "")
        self.reason = reason
        self.target, self.phase = None, None
        with self._move_cond:
            self._move = None
        self._sent = (0.0, 0.0, 0.0)
        if self.moved:
            self.state, self.back_since = "back", now
            self.moved = False
            self._ops.put(self._go_back)
            self._ensure_thread()
            self.cam.ptz_moved(manual=False, seconds=RETURN_SETTLE)
        else:
            self.state = "idle"
            self.last_end = now
