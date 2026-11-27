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
import time
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


# Ports cameras commonly answer ONVIF on. The Hiseeu WHC905 uses 8888.
COMMON_PORTS = [8888, 8899, 80, 8000, 8080, 5000, 2020]


class OnvifPtz:
    def __init__(self, host, port, username, password, on_port_found=None):
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
        self.zoom = False           # the camera has an optical zoom we can drive
        self.error = None
        self.on_port_found = on_port_found   # called with the right port if it wasn't the saved one
        self._lock = threading.Lock()

    def _call(self, url, body, auth=True, timeout=5):
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
            with urllib.request.urlopen(req, timeout=timeout) as r:
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
            # Match the camera's clock, since the login digest includes the time. This is also
            # how we find the right port: the saved one first, then the usual ones.
            xml = None
            for port in [self.port] + [p for p in COMMON_PORTS if p != self.port]:
                url = f"http://{self.host}:{port}/onvif/device_service"
                try:
                    xml = self._call(url, "<tds:GetSystemDateAndTime/>", auth=False, timeout=3)
                except (OSError, RuntimeError):
                    continue
                if "Envelope" not in xml:
                    continue
                if port != self.port:
                    log.info("The camera at %s answers ONVIF on port %d, not %d", self.host, port, self.port)
                    self.port, self.device_url = port, url
                    if self.on_port_found:
                        self.on_port_found(port)
                break
            else:
                raise RuntimeError("The camera didn't answer on any of the usual control ports")
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
            self.zoom = self._has_zoom()
            self.available = True
            self.error = None
            log.info("Pan and tilt ready on %s (profile %s, zoom %s)", self.host, self.profile,
                     "yes" if self.zoom else "no")
            return True
        except Exception as e:  # network errors, bad replies
            self.error = str(e)
            log.warning("Pan and tilt not available on %s: %s", self.host, e)
            return False

    def _has_zoom(self):
        """Asks the camera whether it has a zoom lens. No answer counts as no zoom."""
        try:
            xml = self._call(self.ptz_url, "<tptz:GetNodes/>")
        except Exception as e:
            log.info("Couldn't ask %s whether it can zoom: %s", self.host, e)
            return False
        return "ContinuousZoomVelocitySpace" in xml

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
            "<tptz:Velocity>"
            + (f'<tt:PanTilt x="{pan:.2f}" y="{tilt:.2f}"/>' if pan or tilt or not zoom else "")
            + (f'<tt:Zoom x="{zoom:.2f}"/>' if zoom else "")
            + "</tptz:Velocity></tptz:ContinuousMove>"
        )
        with self._lock:
            self._call(self.ptz_url, body)

    def presets(self):
        """The camera's own list of positions, as [(token, name)]."""
        with self._lock:
            xml = self._call(self.ptz_url, f"<tptz:GetPresets><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken></tptz:GetPresets>")
        return re.findall(r'<(?:\w+:)?Preset[^>]*token="([^"]+)"[^>]*>\s*(?:<(?:\w+:)?Name>(.*?)</)?', xml)

    def save_preset(self, name, taken=()):
        """Saves where the camera points now under `name`. Returns the camera's token for it.

        Some cameras make a new slot when asked; others (like the WHC905) have fixed numbered
        slots, and we use the first one we haven't used already.
        """
        def set_preset(token=None, named=True):
            body = (f"<tptz:SetPreset><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
                    + (f"<tptz:PresetName>{_esc(name)}</tptz:PresetName>" if named else "")
                    + (f"<tptz:PresetToken>{_esc(token)}</tptz:PresetToken>" if token else "")
                    + "</tptz:SetPreset>")
            with self._lock:
                xml = self._call(self.ptz_url, body)
            return _find(xml, "PresetToken") or token

        slots = self.presets()
        # Lots of slots means fixed numbered ones (the WHC905 lists 255); otherwise the camera
        # makes a new one for each save, and we mustn't overwrite positions saved in its own app.
        if len(slots) < 32:
            try:
                token = set_preset()
                if token and token not in taken:
                    return token
            except RuntimeError as e:
                log.info("Camera wouldn't make a new position slot (%s); using a numbered one", e)
        for token, _ in (slots if len(slots) >= 32 else []):
            if token not in taken:
                try:
                    return set_preset(token)
                except RuntimeError:
                    # Some cameras won't rename a numbered slot; save the position without a name.
                    return set_preset(token, named=False)
        raise RuntimeError("The camera has no free position slots")

    def update_preset(self, token, name):
        """Saves where the camera points now into a slot we already have."""
        body = (f"<tptz:SetPreset><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
                f"<tptz:PresetName>{_esc(name)}</tptz:PresetName>"
                f"<tptz:PresetToken>{_esc(token)}</tptz:PresetToken></tptz:SetPreset>")
        try:
            with self._lock:
                self._call(self.ptz_url, body)
        except RuntimeError:
            # Some cameras won't rename a numbered slot; save the position without a name.
            body = (f"<tptz:SetPreset><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
                    f"<tptz:PresetToken>{_esc(token)}</tptz:PresetToken></tptz:SetPreset>")
            with self._lock:
                self._call(self.ptz_url, body)
        return token

    def goto_preset(self, token):
        body = (f"<tptz:GotoPreset><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
                f"<tptz:PresetToken>{_esc(token)}</tptz:PresetToken></tptz:GotoPreset>")
        with self._lock:
            self._call(self.ptz_url, body)

    def remove_preset(self, token):
        body = (f"<tptz:RemovePreset><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
                f"<tptz:PresetToken>{_esc(token)}</tptz:PresetToken></tptz:RemovePreset>")
        with self._lock:
            self._call(self.ptz_url, body)

    def nudge(self, pan, seconds=0.6):
        """Turns a little to one side, for following movement."""
        self.move(pan, 0)
        time.sleep(seconds)
        self.stop()

    def home(self):
        body = f"<tptz:GotoHomePosition><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken></tptz:GotoHomePosition>"
        with self._lock:
            self._call(self.ptz_url, body)

    def set_home(self):
        body = f"<tptz:SetHomePosition><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken></tptz:SetHomePosition>"
        with self._lock:
            self._call(self.ptz_url, body)

    def stop(self):
        body = (
            f"<tptz:Stop><tptz:ProfileToken>{_esc(self.profile)}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
        )
        with self._lock:
            self._call(self.ptz_url, body)
