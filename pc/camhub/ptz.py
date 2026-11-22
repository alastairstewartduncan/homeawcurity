"""Pan and tilt control over ONVIF, using plain SOAP requests.

Only the calls we need are implemented: find the services, pick the first media
profile, then ContinuousMove and Stop. Not every camera supports ONVIF PTZ, so
`available` tells the apps whether to show the controls.
"""
import base64
import datetime
import hashlib
import logging
import os
import re
import threading
import urllib.error
import urllib.request

log = logging.getLogger("camhub.ptz")

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}


def _security_header(username, password, offset):
    if not username:
        return ""
    nonce = os.urandom(16)
    created = (datetime.datetime.now(datetime.timezone.utc) + offset).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" '
        'xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        "<UsernameToken>"
        f"<Username>{_esc(username)}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</Password>"
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</Nonce>"
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created>"
        "</UsernameToken></Security></s:Header>"
    )


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _find(xml, tag):
    m = re.search(r"<(?:\w+:)?" + tag + r"\b[^>]*>(.*?)</(?:\w+:)?" + tag + ">", xml, re.S)
    return m.group(1).strip() if m else None


class OnvifPtz:
    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.media_url = None
        self.ptz_url = None
        self.profile = None
        self.offset = datetime.timedelta(0)
        self.available = False
        self.error = None
        self._lock = threading.Lock()

    def _call(self, url, body, auth=True):
        header = _security_header(self.username, self.password, self.offset) if auth else ""
        env = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" '
            f'xmlns:tptz="{NS["tptz"]}" xmlns:tt="{NS["tt"]}">'
            f"{header}<s:Body>{body}</s:Body></s:Envelope>"
        )
        req = urllib.request.Request(
            url, data=env.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"ONVIF error {e.code}: {_find(text, 'Text') or text[:200]}")

    def connect(self):
        """Finds the PTZ service. Safe to call again after a failure."""
        if not self.host:
            self.error = "No ONVIF address set"
            return False
        try:
            # Match the camera's clock, since the login digest includes the time.
            xml = self._call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
            utc = re.search(r"UTCDateTime>(.*?)</(?:\w+:)?UTCDateTime", xml, re.S)
            if utc:
                part = utc.group(1)
                vals = {k: int(_find(part, k) or 0) for k in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
                cam = datetime.datetime(vals["Year"], vals["Month"], vals["Day"], vals["Hour"],
                                        vals["Minute"], vals["Second"], tzinfo=datetime.timezone.utc)
                self.offset = cam - datetime.datetime.now(datetime.timezone.utc)
            xml = self._call(self.device_url, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>')
            media = re.search(r"<(?:\w+:)?Media>.*?XAddr>(.*?)</", xml, re.S)
            ptz = re.search(r"<(?:\w+:)?PTZ>.*?XAddr>(.*?)</", xml, re.S)
            if not media or not ptz:
                self.error = "This camera does not offer pan and tilt over ONVIF"
                return False
            self.media_url = self._fix_host(media.group(1).strip())
            self.ptz_url = self._fix_host(ptz.group(1).strip())
            xml = self._call(self.media_url, "<trt:GetProfiles/>")
            m = re.search(r'<(?:\w+:)?Profiles[^>]*token="([^"]+)"', xml)
            if not m:
                self.error = "No media profile found"
                return False
            self.profile = m.group(1)
            self.available = True
            self.error = None
            log.info("Pan and tilt ready on %s (profile %s)", self.host, self.profile)
            return True
        except Exception as e:  # network errors, bad replies
            self.error = str(e)
            log.warning("Pan and tilt not available on %s: %s", self.host, e)
            return False

    def _fix_host(self, url):
        # Some cameras report an internal address; keep the path, use the host we know.
        m = re.match(r"https?://[^/]+(/.*)?$", url)
        path = m.group(1) if m and m.group(1) else "/onvif/device_service"
        return f"http://{self.host}:{self.port}{path}"

    def move(self, pan, tilt, zoom=0.0):
        pan = max(-1.0, min(1.0, float(pan)))
        tilt = max(-1.0, min(1.0, float(tilt)))
        zoom = max(-1.0, min(1.0, float(zoom)))
        body = (
            f"<tptz:ContinuousMove><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
            f'<tptz:Velocity><tt:PanTilt x="{pan:.2f}" y="{tilt:.2f}"/>'
            + (f'<tt:Zoom x="{zoom:.2f}"/>' if zoom else "")
            + "</tptz:Velocity></tptz:ContinuousMove>"
        )
        with self._lock:
            self._call(self.ptz_url, body)

    def stop(self):
        body = (
            f"<tptz:Stop><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
        )
        with self._lock:
            self._call(self.ptz_url, body)
