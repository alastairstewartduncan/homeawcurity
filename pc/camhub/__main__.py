"""Start the recorder, or run a setup command.

    python -m camhub run [--config config.json]
    python -m camhub set-password
    python -m camhub find-stream <camera ip> [--user admin]
"""
import argparse
import getpass
import logging
import logging.handlers
import os
import re
import subprocess
import sys

from .config import Config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(HERE, "config.json")


def setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Small rotating log: at most 3 x 1 MB, so the disk is barely touched.
    fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "camhub.log"), maxBytes=1_000_000,
                                              backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


class Hub:
    def __init__(self, config):
        from .camera import Camera
        from .detector import PersonDetector
        from .events import EventBus
        from .ptz import OnvifPtz
        from .storage import Storage

        self.config = config
        self.storage = Storage(config)
        self.events = EventBus()
        self.detector = PersonDetector()
        self.cameras = {}
        for c in config.cameras:
            on = c.get("onvif") or {}
            ptz = OnvifPtz(on["host"], int(on.get("port") or 8899), on.get("username", ""),
                           on.get("password", "")) if on.get("host") else None
            self.cameras[c["id"]] = Camera(c, self.storage, self.events, self.detector, ptz)

    def start(self):
        self.storage.start_cleanup_thread()
        for cam in self.cameras.values():
            cam.start()

    def stop(self):
        for cam in self.cameras.values():
            cam.stop()


def use_ffmpeg_from(config):
    path = config.data.get("ffmpeg_path")
    if path:
        os.environ["CAMHUB_FFMPEG"] = path


def cmd_run(args):
    import uvicorn

    config = Config(args.config)
    use_ffmpeg_from(config)
    from .server import create_app

    for c in config.cameras:
        if not re.match(r"^[A-Za-z0-9-]+$", c["id"]):
            sys.exit(f"Camera id '{c['id']}' may only use letters, numbers and dashes.")
    setup_logging(os.path.join(HERE, "logs"))
    log = logging.getLogger("camhub")
    if not config.server.get("password_hash"):
        sys.exit("No password set yet. Run:  python -m camhub set-password")
    hub = Hub(config)
    hub.start()
    app = create_app(hub)
    s = config.server
    ssl = {}
    if s.get("https_cert") and s.get("https_key"):
        ssl = {"ssl_certfile": s["https_cert"], "ssl_keyfile": s["https_key"]}
    log.info("Home Camera running on %s://%s:%s", "https" if ssl else "http", s["host"], s["port"])
    try:
        uvicorn.run(app, host=s["host"], port=int(s["port"]), log_level="warning", access_log=False,
                    log_config=None, **ssl)
    finally:
        hub.stop()


def cmd_set_password(args):
    from .auth import hash_password
    import secrets

    config = Config(args.config)
    while True:
        p1 = getpass.getpass("New password for the viewer and phone app: ")
        if len(p1) < 8:
            print("Please use at least 8 characters.")
            continue
        if getpass.getpass("Type it again: ") != p1:
            print("Those didn't match, try again.")
            continue
        break
    config.server["password_hash"] = hash_password(p1)
    config.server["secret"] = secrets.token_hex(32)  # signs everyone out
    config.save()
    print("Password saved. Restart Home Camera for it to take effect.")


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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
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


def cmd_find_stream(args):
    """Tries the usual stream addresses on a camera and reports which work."""
    from urllib.parse import quote

    config = Config(args.config)
    use_ffmpeg_from(config)
    user = args.user
    password = getpass.getpass(f"Camera password for '{user}' (leave empty if none): ")
    found = []
    print(f"Trying {len(CANDIDATE_PATHS)} addresses on {args.ip}:{args.port}. This can take a minute or two.")
    for path in CANDIDATE_PATHS:
        inline = "{u}" in path
        p = path.format(u=quote(user, safe=""), p=quote(password, safe=""))
        auth = "" if inline else (f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else "")
        url = f"rtsp://{auth}{args.ip}:{args.port}{p}"
        res = probe(url, timeout=8)
        shown = path.replace("{u}", user).replace("{p}", "****")
        print(f"  {'OK ' if res else '-- '} {shown}" + (f"  ->  {res[0]}x{res[1]} {res[2]}" if res else ""))
        if res:
            found.append((res[0] * res[1], url, res))
    if not found:
        print("\nNone worked. Check the IP address, password, and that RTSP is switched on in the camera's app.")
        sys.exit(1)
    found.sort(reverse=True)
    print("\nPut these in config.json:")
    print(f'  "main_url": "{found[0][1]}"')
    sub = [f for f in found if f[0] < found[0][0]]
    if sub:
        print(f'  "sub_url": "{sub[-1][1]}"')
    else:
        print('  "sub_url": ""   (no smaller stream found; motion detection will use the main one)')


def main():
    p = argparse.ArgumentParser(prog="camhub")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("set-password")
    f = sub.add_parser("find-stream", help="find the camera's stream addresses")
    f.add_argument("ip")
    f.add_argument("--user", default="admin")
    f.add_argument("--port", type=int, default=554)
    args = p.parse_args()
    {"set-password": cmd_set_password, "find-stream": cmd_find_stream}.get(args.cmd, cmd_run)(args)


if __name__ == "__main__":
    main()
