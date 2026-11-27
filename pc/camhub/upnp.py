"""Asks the home router to open a port for away-from-home access (UPnP IGD).

Plain standard-library code that talks only to the router on the home network: it finds
the router with an SSDP broadcast, then uses the router's own WANIPConnection service to
add or remove a port mapping and to read the home's internet address. Nothing outside the
house is contacted.
"""
import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("camhub.upnp")

SSDP = ("239.255.255.250", 1900)
SEARCH_TYPES = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
)
SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


class UPnPError(Exception):
    pass


def _private(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


def local_ip_towards(host="192.0.2.1"):
    """This PC's address on the home network (no packet is sent: UDP connect only picks a route)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def discover(timeout=3.0):
    """Description URLs of routers that answered, best first. Only addresses on the home network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    found = []
    try:
        for st in SEARCH_TYPES:
            msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n')
            sock.sendto(msg.encode(), SSDP)
        while True:
            try:
                data, (addr, _) = sock.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if not m:
                continue
            url = m.group(1).decode(errors="replace")
            host = urllib.parse.urlparse(url).hostname or ""
            # Only trust answers from the home network that point back at the device that answered.
            if host != addr or not _private(host):
                continue
            if url not in found:
                found.append(url)
    finally:
        sock.close()
    return found


def _fetch(url, data=None, headers=None, timeout=5):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(512 * 1024)


def _strip(tag):
    return tag.rsplit("}", 1)[-1]


def find_service(location):
    """(control URL, service type) for the router's WAN connection service."""
    root = ET.fromstring(_fetch(location))
    base = location
    for el in root.iter():
        if _strip(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
    services = {}
    for svc in root.iter():
        if _strip(svc.tag) != "service":
            continue
        kids = {_strip(k.tag): (k.text or "").strip() for k in svc}
        if kids.get("serviceType") in SERVICES and kids.get("controlURL"):
            services[kids["serviceType"]] = urllib.parse.urljoin(base, kids["controlURL"])
    for st in SERVICES:
        if st in services:
            control = services[st]
            if urllib.parse.urlparse(control).hostname != urllib.parse.urlparse(location).hostname:
                raise UPnPError("The router pointed somewhere else.")
            return control, st
    raise UPnPError("The router doesn't offer port opening (UPnP).")


def soap(control, service, action, args=()):
    body = "".join(f"<{k}>{_xml(v)}</{k}>" for k, v in args)
    envelope = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'}
    try:
        raw = _fetch(control, envelope.encode(), headers)
    except urllib.error.HTTPError as e:
        raw = e.read(64 * 1024)
        code = re.search(rb"<errorCode>(\d+)</errorCode>", raw)
        desc = re.search(rb"<errorDescription>([^<]*)</errorDescription>", raw)
        raise UPnPError(f"The router refused ({code.group(1).decode() if code else e.code}"
                        f"{': ' + desc.group(1).decode(errors='replace') if desc else ''})") from None
    out = {}
    for el in ET.fromstring(raw).iter():
        if not list(el) and _strip(el.tag) not in ("Envelope", "Body"):
            out[_strip(el.tag)] = (el.text or "").strip()
    return out


def _xml(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Router:
    """The home router found on the network."""

    def __init__(self, control, service):
        self.control = control
        self.service = service

    @classmethod
    def find(cls, timeout=3.0):
        last = None
        for loc in discover(timeout):
            try:
                return cls(*find_service(loc))
            except Exception as e:  # try the next device that answered
                last = e
        raise UPnPError(str(last) if last else "No router answered. Port opening (UPnP) may be turned off on it.")

    @property
    def host(self):
        return urllib.parse.urlparse(self.control).hostname

    def external_ip(self):
        return soap(self.control, self.service, "GetExternalIPAddress").get("NewExternalIPAddress", "")

    def add_mapping(self, external_port, internal_ip, internal_port, description, lease=3600):
        args = [("NewRemoteHost", ""), ("NewExternalPort", int(external_port)), ("NewProtocol", "TCP"),
                ("NewInternalPort", int(internal_port)), ("NewInternalClient", internal_ip),
                ("NewEnabled", 1), ("NewPortMappingDescription", description),
                ("NewLeaseDuration", int(lease))]
        try:
            soap(self.control, self.service, "AddPortMapping", args)
        except UPnPError as e:
            # 725: this router only does permanent mappings.
            if lease and "725" in str(e):
                args[-1] = ("NewLeaseDuration", 0)
                soap(self.control, self.service, "AddPortMapping", args)
            else:
                raise

    def delete_mapping(self, external_port):
        soap(self.control, self.service, "DeletePortMapping",
             [("NewRemoteHost", ""), ("NewExternalPort", int(external_port)), ("NewProtocol", "TCP")])
