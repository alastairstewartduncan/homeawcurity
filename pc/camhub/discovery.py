"""Finding a camera's video streams by trying the addresses its maker usually uses."""
import os
import subprocess
import sys


def _ffprobe():
    from .camera import FFMPEG

    d, name = os.path.split(FFMPEG)
    return os.path.join(d, name.replace("ffmpeg", "ffprobe")) if "ffmpeg" in name else "ffprobe"


def probe(url, timeout=15):
    """Returns (width, height, codec) if the address gives video, else None."""
    cmd = [_ffprobe(), "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(timeout * 1_000_000),
           "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
           "-of", "default=noprint_wrappers=1", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
    except (subprocess.TimeoutExpired, OSError):
        return None
    vals = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if r.returncode == 0 and vals.get("width"):
        return int(vals["width"]), int(vals["height"]), vals.get("codec_name")
    return None


# Paths used by Hiseeu and similar (XMeye-based) cameras, most likely first.
CANDIDATE_PATHS = [
    "/user={u}_password={p}_channel=1_stream=0.sdp?real_stream",
    "/user={u}_password={p}_channel=1_stream=1.sdp?real_stream",
    "/user={u}&password={p}&channel=1&stream=0.sdp?real_stream",
    "/user={u}&password={p}&channel=1&stream=1.sdp?real_stream",
    "/live/main", "/live/sub", "/live/ch00_0", "/live/ch00_1",
    "/stream0", "/stream1", "/11", "/12", "/ch0_0.264", "/ch0_1.264",
    "/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1",
    "/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream", "/onvif1", "/onvif2", "/",
]


def find_streams(ip, user, password, port=554, report=print):
    """Tries the usual stream addresses on a camera. Returns (main_url, sub_url) or (None, None)."""
    from urllib.parse import quote

    import socket

    try:
        socket.create_connection((ip, port), timeout=5).close()
    except OSError as e:
        report(f"Couldn't reach {ip} on port {port} ({e}). Is the camera on, and is that its address?")
        return None, None
    found = []
    report(f"Trying {len(CANDIDATE_PATHS)} addresses on {ip}:{port}. This can take a minute or two.")
    for path in CANDIDATE_PATHS:
        inline = "{u}" in path
        p = path.format(u=quote(user, safe=""), p=quote(password, safe=""))
        auth = "" if inline else (f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else "")
        url = f"rtsp://{auth}{ip}:{port}{p}"
        res = probe(url, timeout=8)
        shown = path.replace("{u}", user).replace("{p}", "****")
        report(f"  {'OK ' if res else '-- '} {shown}" + (f"  ->  {res[0]}x{res[1]} {res[2]}" if res else ""))
        if res:
            found.append((res[0] * res[1], url, res))
            # Two sizes found (main and sub): no need to try the rest.
            if len({f[0] for f in found}) >= 2:
                break
    if not found:
        return None, None
    found.sort(key=lambda f: f[0], reverse=True)
    sub = [f for f in found if f[0] < found[0][0]]
    return found[0][1], (sub[-1][1] if sub else "")
