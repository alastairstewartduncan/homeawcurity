"""Privacy areas: parts of the picture that are always blurred or blacked out.

A camera's areas live in its settings as
    "privacy": {"style": "blur" | "black", "masks": [{"name": "...", "points": [[x, y], ...]}]}
with x and y from 0 to 1, so the same areas fit both of the camera's streams.

- The live picture, snapshots, clip thumbnails and phone alert pictures are covered as each
  picture arrives from the camera (apply_to_frame).
- Recordings are copied from the camera without being re-made, so each finished clip is made
  again once with the areas covered (mask_clip), before it shows up in the recordings list.
"""
import json
import logging
import os
import subprocess
import tempfile
import threading

import cv2
import numpy as np

log = logging.getLogger("camhub.privacy")

STYLES = ("blur", "black")
MAX_MASKS = 20
MAX_POINTS = 50

_cache = {}
_cache_lock = threading.Lock()
# Making a clip again is heavy work for the PC, so do one at a time.
_encode_lock = threading.Lock()


def settings_of(cam_cfg):
    """The camera's privacy settings, always in the full form."""
    p = cam_cfg.get("privacy") or {}
    style = p.get("style") if p.get("style") in STYLES else "blur"
    return {"style": style, "masks": list(p.get("masks") or [])}


def clean(body):
    """Checks privacy settings sent from the viewer. Returns the form to save."""
    style = body.get("style") if body.get("style") in STYLES else "blur"
    masks = []
    for m in (body.get("masks") or [])[:MAX_MASKS]:
        try:
            pts = [[round(max(0.0, min(1.0, float(x))), 4), round(max(0.0, min(1.0, float(y))), 4)]
                   for x, y in (m.get("points") or [])[:MAX_POINTS]]
        except (TypeError, ValueError):
            continue
        if len(pts) >= 3:
            masks.append({"name": str(m.get("name") or "Private area")[:40], "points": pts})
    return {"style": style, "masks": masks}


def for_recording(cam_cfg):
    """A copy of the settings to use for one clip, or None if nothing is covered."""
    s = settings_of(cam_cfg)
    return s if s["masks"] else None


def mask_image(width, height, masks):
    """A black and white picture: white where the picture must be covered."""
    key = (width, height, json.dumps(masks, sort_keys=True))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    m = np.zeros((height, width), np.uint8)
    for z in masks:
        pts = np.array([[round(x * (width - 1)), round(y * (height - 1))] for x, y in z.get("points", [])], np.int32)
        if len(pts) >= 3:
            cv2.fillPoly(m, [pts], 255)
    # Grow the areas a little, so the edge of a window or a face just outside doesn't show.
    grow = max(1, width // 200)
    m = cv2.dilate(m, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))
    with _cache_lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = m
    return m


def _block(width):
    # Big enough squares that faces and writing can't be read.
    return max(6, width // 40)


def apply_to_frame(frame, settings):
    """Returns the picture with the privacy areas covered (a new array if anything changed)."""
    masks = settings.get("masks") if settings else None
    if not masks or frame is None or frame.size == 0:
        return frame
    h, w = frame.shape[:2]
    m = mask_image(w, h, masks) > 0
    if not m.any():
        return frame
    out = frame.copy()
    if settings.get("style") == "black":
        out[m] = 0
    else:
        b = _block(w)
        small = cv2.resize(frame, (max(1, w // b), max(1, h // b)), interpolation=cv2.INTER_AREA)
        big = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
        out[m] = big[m]
    return out


def _video_size(path):
    from .discovery import _ffprobe

    try:
        r = subprocess.run([_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60, creationflags=_flags())
        w, h = r.stdout.strip().splitlines()[0].split(",")[:2]
        return int(w), int(h)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _flags():
    from .camera import NO_WINDOW

    below_normal = 0x00004000 if os.name == "nt" else 0  # leave the PC responsive
    return NO_WINDOW | below_normal


def mask_clip(src, dst, settings):
    """Makes the clip at src again with the privacy areas covered, writing an MP4 to dst.

    Returns True if dst was written. The sound is copied unchanged.
    """
    from .camera import FFMPEG

    size = _video_size(src)
    if not size:
        log.error("Couldn't read the size of %s to cover the privacy areas", src)
        return False
    w, h = size
    fd, mask_png = tempfile.mkstemp(suffix=".png", prefix="camhub-mask-")
    os.close(fd)
    try:
        cv2.imwrite(mask_png, mask_image(w, h, settings["masks"]))
        if settings.get("style") == "black":
            cover = "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill"
        else:
            b = _block(w)
            sw, sh = max(2, w // b // 2 * 2), max(2, h // b // 2 * 2)
            cover = f"scale={sw}:{sh}:flags=area,scale={w}:{h}:flags=neighbor"
        graph = (f"[0:v]split=2[base][fx];[fx]{cover}[cov];"
                 f"[1:v]format=gray,scale={w}:{h}[m];[cov][m]alphamerge[top];"
                 f"[base][top]overlay=shortest=1:format=auto,format=yuv420p[out]")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-loop", "1", "-i", mask_png,
               "-filter_complex", graph, "-map", "[out]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy",
               "-movflags", "+faststart", "-f", "mp4", dst]
        with _encode_lock:
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=3600, creationflags=_flags())
            except (OSError, subprocess.TimeoutExpired) as e:
                log.error("Covering the privacy areas failed: %s", e)
                return False
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            log.error("Covering the privacy areas failed: %s", r.stderr.decode(errors="replace")[-500:])
            try:
                os.remove(dst)
            except OSError:
                pass
            return False
        return True
    finally:
        try:
            os.remove(mask_png)
        except OSError:
            pass


def add_privacy_routes(app, hub):
    from fastapi import HTTPException, Request

    def cam_cfg(cam_id):
        c = hub.config.camera(cam_id)
        if not c:
            raise HTTPException(404, "No such camera")
        return c

    @app.get("/api/cameras/{cam_id}/privacy")
    def get_privacy(cam_id: str):
        return settings_of(cam_cfg(cam_id))

    @app.put("/api/cameras/{cam_id}/privacy")
    async def put_privacy(cam_id: str, request: Request):
        c = cam_cfg(cam_id)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON")
        new = clean(body if isinstance(body, dict) else {})
        c["privacy"] = new
        # The running camera may hold its own copy of the settings after a restart or a change.
        cam = hub.cameras.get(cam_id)
        if cam is not None and cam.cfg is not c:
            cam.cfg["privacy"] = new
        hub.config.save()
        return new
