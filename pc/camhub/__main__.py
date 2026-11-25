"""Start the recorder, or run a setup command.

    python -m camhub run [--config config.json]
    python -m camhub set-password
    python -m camhub find-stream <camera ip> [--user admin]
    camhub.exe setup --answers answers.json [--result result.txt]   (used by the Windows installer)
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

FROZEN = getattr(sys, "frozen", False)  # running as the installed camhub.exe
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FROZEN:
    # Program Files is read-only for normal use, so settings and logs live in ProgramData.
    DATA_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "HomeCamera")
    DEFAULT_CONFIG = os.path.join(DATA_DIR, "config.json")
else:
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
    if not path and FROZEN:
        # The installer puts ffmpeg.exe next to camhub.exe.
        path = os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe")
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
    setup_logging(os.path.join(os.path.dirname(os.path.abspath(args.config)), "logs"))
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


def cmd_find_stream(args):
    config = Config(args.config)
    use_ffmpeg_from(config)
    password = getpass.getpass(f"Camera password for '{args.user}' (leave empty if none): ")
    main_url, sub_url = find_streams(args.ip, args.user, password, args.port)
    if not main_url:
        print("\nNone worked. Check the IP address, password, and that RTSP is switched on in the camera's app.")
        sys.exit(1)
    if args.save:
        if not config.cameras:
            import copy

            from .config import DEFAULT_CAMERA
            config.cameras.append(copy.deepcopy(DEFAULT_CAMERA))
        cam = config.cameras[0]
        cam["main_url"], cam["sub_url"] = main_url, sub_url
        cam["onvif"].update(host=args.ip, username=args.user, password=password)
        config.save()
        print("\nSaved to the settings. Restart Home Camera to use it.")
        return
    print("\nPut these in config.json:")
    print(f'  "main_url": "{main_url}"')
    print(f'  "sub_url": "{sub_url}"' if sub_url else
          '  "sub_url": ""   (no smaller stream found; motion detection will use the main one)')


def cmd_setup(args):
    """Non-interactive setup used by the Windows installer.

    Reads a JSON answers file (deleted straight after, as it holds passwords):
      {"password": "...", "storage": "C:/CameraRecordings", "camera_ip": "192.168.1.50",
       "camera_user": "admin", "camera_password": "...", "camera_name": "Front", "port": 8080}
    Creates or updates config.json, then looks for the camera's streams.
    Exit code: 0 all done, 3 camera not found (config still written), 2 bad answers.
    """
    import json
    import secrets

    from .auth import hash_password
    from .config import DEFAULT_CAMERA, DEFAULT_CONFIG as BASE

    lines = []

    def report(text):
        lines.append(text)
        print(text)

    def finish(code):
        if args.result:
            with open(args.result, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        sys.exit(code)

    try:
        with open(args.answers, "r", encoding="utf-8-sig") as f:
            ans = json.load(f)
    except (OSError, ValueError) as e:
        report(f"Could not read the setup answers: {e}")
        finish(2)
    finally:
        try:
            os.remove(args.answers)
        except OSError:
            pass

    os.makedirs(os.path.dirname(os.path.abspath(args.config)), exist_ok=True)
    if not os.path.exists(args.config):
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(BASE, f, indent=2)
    config = Config(args.config)
    use_ffmpeg_from(config)

    if ans.get("password"):
        config.server["password_hash"] = hash_password(ans["password"])
        config.server["secret"] = secrets.token_hex(32)
    if ans.get("port"):
        config.server["port"] = int(ans["port"])
    if ans.get("storage"):
        config.storage["path"] = ans["storage"].replace("\\", "/")

    code = 0
    ip = (ans.get("camera_ip") or "").strip()
    if ip:
        user = ans.get("camera_user") or "admin"
        cam_pw = ans.get("camera_password") or ""
        if not config.cameras:
            import copy
            config.cameras.append(copy.deepcopy(DEFAULT_CAMERA))
        cam = config.cameras[0]
        if ans.get("camera_name"):
            cam["name"] = ans["camera_name"]
        cam["onvif"] = {"host": ip, "port": int(ans.get("onvif_port") or 8899), "username": user, "password": cam_pw}
        main_url, sub_url = find_streams(ip, user, cam_pw, report=report)
        if main_url:
            cam["main_url"], cam["sub_url"] = main_url, sub_url
            report("Camera found and saved.")
        else:
            report("The camera's video wasn't found. Check its IP address and password, then run")
            report("Start menu > Home Camera > Find camera again.")
            code = 3
    config.save()
    finish(code)


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
    f.add_argument("--save", action="store_true", help="save the result as the first camera's settings")
    st = sub.add_parser("setup", help="non-interactive setup used by the Windows installer")
    st.add_argument("--answers", required=True)
    st.add_argument("--result")
    args = p.parse_args()
    {"set-password": cmd_set_password, "find-stream": cmd_find_stream,
     "setup": cmd_setup}.get(args.cmd, cmd_run)(args)


if __name__ == "__main__":
    main()
