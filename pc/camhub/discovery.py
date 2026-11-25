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


def _run(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return "timeout", out, err
    except OSError as e:
        return "error", "", str(e)


def diagnose(main_url, sub_url, report=print, seconds=5):
    """Tries several ways of reading a camera's streams and reports what each one does.

    Used by Settings > Cameras > Check video, so a person can send the result when a camera
    won't come online. Passwords are hidden in everything reported.
    """
    from .camera import FFMPEG, redact

    rtsp_args = ["-rtsp_transport", "tcp", "-timeout", "10000000"]

    def last_lines(text, n=3):
        lines = [redact(line.strip()) for line in text.splitlines() if line.strip()]
        lines = [line for line in lines if not line.startswith("Last message repeated")]
        return lines[-n:]

    for label, url in (("Main stream", main_url), ("Small stream", sub_url)):
        if not url:
            report(f"{label}: none saved")
            continue
        rtsp = rtsp_args if url.lower().startswith("rtsp") else []
        report(f"== {label}: {redact(url)}")
        code, out, err = _run([_ffprobe(), "-v", "error", *rtsp, "-show_entries",
                               "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,sample_rate",
                               "-of", "compact=p=0", url], 25)
        for line in out.splitlines():
            report("  stream " + line)
        if code != 0:
            report(f"  probe failed ({code}): " + " | ".join(last_lines(err)))
            continue
        code, out, err = _run([_ffprobe(), "-v", "error", *rtsp, "-select_streams", "v:0", "-show_entries",
                               "packet=pts_time,dts_time,size,flags", "-read_intervals", "%+#40",
                               "-of", "compact=p=0:nk=1", url], 25)
        pk = [line.split("|") for line in out.splitlines() if line.strip()]
        no_pts = sum(1 for p in pk if p and p[0] in ("N/A", ""))
        keys = sum(1 for p in pk if p and "K" in p[-1])
        report(f"  first {len(pk)} video packets: {no_pts} without timestamps, {keys} keyframes")
        for p in pk[:6]:
            report("    packet " + "|".join(p))
        tests = [
            ("copy to MPEG-TS", [], ["-map", "0:v:0", "-c", "copy", "-f", "mpegts"]),
            ("copy to MPEG-TS, PC clock", ["-use_wallclock_as_timestamps", "1"],
             ["-map", "0:v:0", "-c", "copy", "-f", "mpegts"]),
            ("copy to MPEG-TS, generated timestamps", ["-fflags", "+genpts+igndts"],
             ["-map", "0:v:0", "-c", "copy", "-f", "mpegts"]),
            ("copy to Matroska", [], ["-map", "0:v:0", "-c", "copy", "-f", "matroska"]),
            ("decode pictures", [], ["-map", "0:v:0", "-f", "null"]),
            ("decode pictures, PC clock", ["-use_wallclock_as_timestamps", "1"], ["-map", "0:v:0", "-f", "null"]),
        ]
        for name, before, after in tests:
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-stats", *rtsp, *before, "-i", url,
                   "-t", str(seconds), *after, "-y", os.devnull if after[-1] != "null" else "-"]
            code, out, err = _run(cmd, seconds + 25)
            stats = [" ".join(line.split()) for line in err.replace("\r", "\n").splitlines()
                     if "time=" in line and ("size=" in line or "frame=" in line)]
            errors = [line for line in last_lines(err, 8) if "time=" not in line]
            ok = code == 0 and bool(stats)
            report(f"  {'OK ' if ok else 'FAIL'} {name}" + (f"  ({stats[-1][:70]})" if stats else "")
                   + ("" if ok else f"  [{code}] " + " | ".join(errors[-3:])))


def list_paths(ip, user, password, port=554, report=print):
    """Reports every usual stream address that gives video, with its size and codec."""
    from urllib.parse import quote

    report(f"== Other addresses on {ip}:{port}")
    for path in CANDIDATE_PATHS:
        inline = "{u}" in path
        p = path.format(u=quote(user, safe=""), p=quote(password, safe=""))
        auth = "" if inline else (f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else "")
        res = probe(f"rtsp://{auth}{ip}:{port}{p}", timeout=6)
        shown = path.replace("{u}", user).replace("{p}", "****")
        report(f"  {'OK ' if res else '-- '} {shown}" + (f"  ->  {res[0]}x{res[1]} {res[2]}" if res else ""))
