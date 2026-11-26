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
import sys

from .config import Config
from .discovery import find_streams

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
        from .backup import Backup
        from .detector import PersonDetector
        from .events import EventBus
        from .storage import Storage

        self.config = config
        self.storage = Storage(config)
        self.events = EventBus()
        from .health import HealthMonitor
        from .presence import Presence
        self.presence = Presence(config, self.events)
        self.events.enrich = self.presence.tag_event
        self.health = HealthMonitor(self)
        self.backup = Backup(config, self.storage, self.events)
        self.detector = PersonDetector()
        # Smart alerts: strangers and family, your cars, parcels, lines and loitering.
        from .smart import SmartHub
        self.smart = SmartHub(self)
        self.events.enrich = lambda e: self.presence.tag_event(self.smart.tag_event(e))
        self.cameras = {}
        self.started = False
        for c in config.cameras:
            self.cameras[c["id"]] = self._make(c)
        from .agents import AgentRunner
        self.agents = AgentRunner(self)
        from .updater import Updater
        self.updater = Updater(config, os.path.dirname(config.path), hub=self)
        from .homelinks import HomeLinks
        self.home_links = HomeLinks(self)

    def _make(self, c):
        from .camera import Camera
        from .ptz import OnvifPtz

        on = c.get("onvif") or {}

        def save_port(port):
            # The camera answered on a different port from the saved one: remember it.
            on["port"] = port
            self.config.save()

        ptz = OnvifPtz(on["host"], int(on.get("port") or 8888), on.get("username", ""),
                       on.get("password", ""), on_port_found=save_port) if on.get("host") else None
        cam = Camera(c, self.storage, self.events, self.detector, ptz)
        if getattr(self, "smart", None) is not None:
            cam.smart = self.smart.watcher(cam)
        return cam

    def start(self):
        self.started = True
        self.health.start()
        self.storage.start_cleanup_thread()
        self.backup.start()
        self.agents.start()
        self.updater.start()
        self.home_links.start()
        for cam in self.cameras.values():
            cam.start()

    def stop(self):
        self.health.stop()
        self.agents.stop()
        self.updater.stop()
        self.home_links.stop()
        for cam in self.cameras.values():
            cam.stop()

    def put_camera(self, cfg):
        """Adds a camera, or replaces one with the same id, and starts it straight away."""
        old = self.cameras.pop(cfg["id"], None)
        if old:
            old.stop()
        cams = [c for c in self.config.cameras if c["id"] != cfg["id"]]
        pos = next((i for i, c in enumerate(self.config.cameras) if c["id"] == cfg["id"]), len(cams))
        cams.insert(pos, cfg)
        self.config.data["cameras"] = cams
        self.config.save()
        cam = self._make(cfg)
        self.cameras[cfg["id"]] = cam
        if self.started:
            cam.start()
        return cam

    def remove_camera(self, cam_id):
        old = self.cameras.pop(cam_id, None)
        if old:
            old.stop()
        self.config.data["cameras"] = [c for c in self.config.cameras if c["id"] != cam_id]
        self.config.save()
        return old is not None


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
    log.info("A.S.D Home Security running on %s://%s:%s", "https" if ssl else "http", s["host"], s["port"])
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
    print("Password saved. Restart A.S.D Home Security for it to take effect.")


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
        print("\nSaved to the settings. Restart A.S.D Home Security to use it.")
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
        cam["onvif"] = {"host": ip, "port": int(ans.get("onvif_port") or 8888), "username": user, "password": cam_pw}
        main_url, sub_url = find_streams(ip, user, cam_pw, report=report)
        if main_url:
            cam["main_url"], cam["sub_url"] = main_url, sub_url
            report("Camera found and saved.")
        else:
            report("The camera's video wasn't found. Check its IP address and password in")
            report("A.S.D Home Security > Settings > Cameras, then press Find and save.")
            code = 3
    config.save()
    finish(code)


def main():
    p = argparse.ArgumentParser(prog="camhub")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    from . import __version__
    p.add_argument("--version", action="version", version=f"A.S.D Home Security {__version__}")
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
