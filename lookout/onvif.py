"""Bounded ONVIF discovery and media lookup; no credentials are persisted here."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import re
import secrets
import select
import socket
import subprocess
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .reolink import camera_names, is_reolink_model


DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA1 = "http://www.onvif.org/ver10/media/wsdl"
MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
MAX_XML = 2 * 1024 * 1024
MAX_DISCOVERED = 256
DISCOVERY_PORT = 37020
MAX_PROFILES = 64
MAX_SOURCES = 32
MAX_CALLS = 64
DEVICE_TIMEOUT = 30.0


class OnvifError(Exception):
    """A credential-safe error suitable for display in the UI."""


class _UnsupportedAction(OnvifError):
    pass


@dataclass(frozen=True)
class DiscoveredDevice:
    name: str
    address: str
    hardware: str = ""


@dataclass
class Camera:
    id: str
    name: str
    uri: str = field(repr=False)
    nvr_id: str
    nvr_name: str
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    width: int = 640
    height: int = 360


def _url(value: str, schemes: tuple[str, ...]):
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise OnvifError("Invalid endpoint URL.")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in schemes or not parts.hostname or "\\" in value:
            raise ValueError
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError
        # Validate bracketed IPs and forbid URL delimiters hidden in a hostname.
        if any(c in parts.hostname for c in "%/?#@"):
            raise ValueError
        return parts
    except (ValueError, UnicodeError):
        raise OnvifError("Invalid endpoint URL.") from None


def normalize_address(address: str) -> str:
    """Accept a hostname/IP, optionally with a port, or a full device-service URL."""
    if not isinstance(address, str):
        raise OnvifError("Enter a camera hostname, IP address, or HTTP(S) URL.")
    address = address.strip()
    if "://" not in address:
        try:
            if ipaddress.ip_address(address).version == 6:
                address = f"[{address}]"
        except ValueError:
            pass
        address = "http://" + address
    parts = _url(address, ("http", "https"))
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise OnvifError("Enter credentials separately and omit URL queries and fragments.")
    host = parts.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None and parts.port != (443 if parts.scheme == "https" else 80):
        netloc += f":{parts.port}"
    path = parts.path if parts.path not in ("", "/") else "/onvif/device_service"
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def _host_key(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host.lower().rstrip(".")


def _repair_host(uri: str, host: str) -> str:
    parts = _url(uri, ("http", "https", "rtsp", "rtsps"))
    advertised = parts.hostname
    local = advertised.lower().rstrip(".") == "localhost"
    try:
        ip = ipaddress.ip_address(advertised)
        local = local or ip.is_loopback or ip.is_unspecified
    except ValueError:
        pass
    if not local:
        return uri
    replacement = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        replacement += f":{parts.port}"
    if "@" in parts.netloc:
        replacement = parts.netloc.rsplit("@", 1)[0] + "@" + replacement
    return urlunsplit(parts._replace(netloc=replacement))


def stream_uri(camera: Camera) -> str:
    """Return a playback URL without duplicating or replacing advertised credentials.

    ONVIFClient only attaches NVR credentials to streams on the NVR's hostname.
    Callers constructing Camera themselves explicitly opt in by supplying credentials.
    """
    parts = _url(camera.uri, ("rtsp", "rtsps", "http", "https"))
    if parts.username is not None:
        if (parts.password is None and camera.password and camera.username
                and unquote(parts.username) == camera.username):
            userinfo, host = parts.netloc.rsplit("@", 1)
            return urlunsplit(parts._replace(netloc=f"{userinfo}:{quote(camera.password, safe='')}@{host}"))
        return camera.uri
    if not camera.username:
        return camera.uri
    credentials = quote(camera.username, safe="") + ":" + quote(camera.password, safe="")
    return urlunsplit(parts._replace(netloc=credentials + "@" + parts.netloc))


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, name: str, default: str = "") -> str:
    node = next((node for node in element.iter() if _local(node) == name), None)
    return node.text.strip() if node is not None and node.text else default


def _xml(data: bytes) -> ET.Element:
    # ElementTree expands internal entities; ONVIF has no reason to contain a DTD.
    if len(data) > MAX_XML or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper() or b"\x00" in data:
        raise OnvifError("Camera returned unsafe or oversized XML.")
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError):
        raise OnvifError("Camera returned invalid XML.") from None


def _discovery_devices(data: bytes, sender: str) -> list[DiscoveredDevice]:
    try:
        root = _xml(data)
    except OnvifError:
        return []
    devices = []
    for match in root.iter():
        if _local(match) != "ProbeMatch":
            continue
        scopes = {}
        for scope in _text(match, "Scopes").split():
            for kind in ("name", "hardware"):
                prefix = f"onvif://www.onvif.org/{kind}/"
                if scope.startswith(prefix):
                    scopes.setdefault(kind, " ".join(unquote(scope[len(prefix):]).split())[:256])
        for address in _text(match, "XAddrs").split()[:16]:
            try:
                address = normalize_address(_repair_host(address, sender))
            except OnvifError:
                continue
            name = scopes.get("name") or scopes.get("hardware") or urlsplit(address).hostname
            devices.append(DiscoveredDevice(name, address, scopes.get("hardware", "")))
    return devices


def _ipv4_interfaces(timeout: float) -> list[str]:
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "up"], capture_output=True,
            text=True, check=True, timeout=timeout,
        )
        addresses = []
        for interface in json.loads(result.stdout):
            if "LOOPBACK" in interface.get("flags", []):
                continue
            for info in interface.get("addr_info", []):
                if info.get("family") != "inet":
                    continue
                address = ipaddress.IPv4Address(info["local"])
                if not (address.is_loopback or address.is_unspecified or address.is_multicast):
                    addresses.append(str(address))
        return list(dict.fromkeys(addresses)) or ["0.0.0.0"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return ["0.0.0.0"]


def discover(timeout: float = 4.0) -> list[DiscoveredDevice]:
    """Probe each IPv4 interface; a stable reply port permits narrow firewall rules."""
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        return []
    deadline = time.monotonic() + timeout
    interfaces = _ipv4_interfaces(min(0.5, timeout))
    probe = (
        f'<s:Envelope xmlns:s="{SOAP}" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header>'
        f'<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>'
        '<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
        '<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
        '</s:Header><s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>'
        '</d:Probe></s:Body></s:Envelope>'
    ).encode()
    sockets = []
    found = {}
    try:
        for interface in interfaces[:64]:
            if time.monotonic() >= deadline:
                break
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.bind((interface, DISCOVERY_PORT))
                sock.setblocking(False)
                sock.sendto(probe, ("239.255.255.250", 3702))
                sockets.append(sock)
            except OSError:
                if sock is not None:
                    sock.close()
        packets = 0
        while sockets and packets < 1024 and len(found) < MAX_DISCOVERED:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select(sockets, [], [], remaining)
            except OSError:
                break
            if not ready:
                break
            for sock in ready:
                if time.monotonic() >= deadline or packets >= 1024:
                    break
                try:
                    data, sender = sock.recvfrom(65536)
                except OSError:
                    continue
                packets += 1
                for device in _discovery_devices(data, sender[0]):
                    found.setdefault(device.address, device)
                    if len(found) >= MAX_DISCOVERED:
                        break
                if len(found) >= MAX_DISCOVERED:
                    break
    finally:
        for sock in sockets:
            sock.close()
    return sorted(found.values(), key=lambda device: (device.name.casefold(), device.address))


class ONVIFClient:
    def __init__(self, address: str, username: str = "", password: str = ""):
        self.address = normalize_address(address)
        self.device_addresses = {self.address}
        if any(not isinstance(value, str) or any(
            ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF)
            for c in value
        ) for value in (username, password)):
            raise OnvifError("Camera credentials must be text without control characters.")
        self.username = username
        self.password = password
        self._host = urlsplit(self.address).hostname
        self._session = requests.Session()
        # Do not consult .netrc or forward LAN camera credentials through ambient proxies.
        self._session.trust_env = False
        self._clock_offset = 0.0
        self._deadline = 0.0
        self._calls = 0

    def _envelope(self, body: str, authenticate: bool) -> bytes:
        security = ""
        if authenticate and self.username:
            nonce = secrets.token_bytes(16)
            created = datetime.fromtimestamp(time.time() + self._clock_offset, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
            security = (
                '<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken>'
                f'<wsse:Username>{escape(self.username)}</wsse:Username>'
                '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
                f'{digest}</wsse:Password>'
                '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
                f'{base64.b64encode(nonce).decode()}</wsse:Nonce>'
                f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>'
            )
        return (
            f'<s:Envelope xmlns:s="{SOAP}" xmlns:tds="{DEVICE}" xmlns:trt="{MEDIA1}" '
            f'xmlns:tr2="{MEDIA2}" xmlns:tt="http://www.onvif.org/ver10/schema" '
            f'xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><s:Header>{security}</s:Header>'
            f'<s:Body>{body}</s:Body></s:Envelope>'
        ).encode("utf-8")

    def _post(self, endpoint: str, action: str, payload: bytes, auth):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0 or self._calls >= MAX_CALLS:
            raise OnvifError("Camera request limit reached. Try again or select a smaller recorder.")
        self._calls += 1
        response = None
        try:
            response = self._session.post(
                endpoint, data=payload,
                headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"', "SOAPAction": f'"{action}"'},
                auth=auth, timeout=(min(2.0, remaining / 2), min(8.0, remaining / 2)),
                verify=True, allow_redirects=False, stream=True,
            )
            data = bytearray()
            for chunk in response.iter_content(16384):
                if time.monotonic() >= self._deadline:
                    raise OnvifError("Camera request timed out.")
                data.extend(chunk)
                if len(data) > MAX_XML:
                    raise OnvifError("Camera returned oversized XML.")
            return response.status_code, response.headers.get("WWW-Authenticate", ""), bytes(data)
        except requests.exceptions.SSLError:
            raise OnvifError("Camera TLS certificate verification failed.") from None
        except requests.exceptions.Timeout:
            raise OnvifError("Camera request timed out.") from None
        except (requests.exceptions.RequestException, OSError, ValueError):
            raise OnvifError("Could not communicate with the camera.") from None
        finally:
            if response is not None:
                response.close()

    def _soap(self, endpoint: str, namespace: str, operation: str, body: str, authenticate: bool = True):
        auth = HTTPDigestAuth(self.username, self.password) if authenticate and self.username else None
        payload = self._envelope(body, authenticate)
        status, challenge, data = self._post(endpoint, namespace + "/" + operation, payload, auth)
        if status == 401 and auth is not None and re.search(r"(?:^|,)\s*Basic(?:\s|$)", challenge, re.I):
            status, _, data = self._post(endpoint, namespace + "/" + operation, payload, HTTPBasicAuth(self.username, self.password))
        if status in (401, 403):
            raise OnvifError("Camera authentication failed. Check the credentials and camera clock.")
        if status in (404, 405, 501):
            raise _UnsupportedAction("Camera does not support this ONVIF operation.")
        if 300 <= status < 400:
            raise OnvifError("Camera redirected the request; enter its direct ONVIF address.")
        try:
            root = _xml(data)
        except OnvifError:
            if status != 200:
                raise OnvifError(f"Camera returned HTTP status {status}.") from None
            raise
        fault = next((node for node in root.iter() if _local(node) == "Fault"), None)
        if fault is not None:
            codes = " ".join(node.text or "" for node in fault.iter() if _local(node) in ("Value", "faultcode"))
            if any(code in codes for code in ("ActionNotSupported", "NotImplemented", "NotSupported")):
                raise _UnsupportedAction("Camera does not support this ONVIF operation.")
            if any(code in codes for code in ("NotAuthorized", "FailedAuthentication", "InvalidSecurity", "MessageExpired")):
                raise OnvifError("Camera authentication failed. Check the credentials and camera clock.")
            raise OnvifError("Camera rejected the ONVIF request.")
        if status != 200:
            raise OnvifError(f"Camera returned HTTP status {status}.")
        if not any(_local(node) == operation + "Response" for node in root.iter()):
            raise OnvifError("Camera returned an unexpected ONVIF response.")
        return root

    def _sync_clock(self):
        for authenticate in (False, True):
            if authenticate and not self.username:
                break
            before = time.time()
            try:
                root = self._soap(self.address, DEVICE, "GetSystemDateAndTime", "<tds:GetSystemDateAndTime/>", authenticate)
                utc = next((node for node in root.iter() if _local(node) == "UTCDateTime"), None)
                if utc is not None:
                    values = [int(_text(utc, name)) for name in ("Year", "Month", "Day", "Hour", "Minute", "Second")]
                    camera_time = datetime(*values, tzinfo=timezone.utc).timestamp()
                    self._clock_offset = camera_time - (before + time.time()) / 2
                return
            except (OnvifError, ValueError, OverflowError):
                continue

    def _endpoint(self, value: str) -> str:
        try:
            absolute = urljoin(self.address, value)
        except ValueError:
            raise OnvifError("Camera advertised an invalid media service URL.") from None
        endpoint = normalize_address(_repair_host(absolute, self._host))
        parts = urlsplit(endpoint)
        if _host_key(parts.hostname) != _host_key(self._host):
            raise OnvifError("Camera advertised a media service on another host; credentials were not forwarded.")
        if self.address.startswith("https:") and parts.scheme != "https":
            raise OnvifError("Camera advertised an insecure media service for an HTTPS connection.")
        return endpoint

    def _services(self) -> dict[str, str]:
        root = self._soap(self.address, DEVICE, "GetServices", "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
        endpoints = {}
        for service in root.iter():
            if _local(service) != "Service":
                continue
            namespace = _text(service, "Namespace")
            address = _text(service, "XAddr")
            if namespace == DEVICE and address:
                try:
                    self.device_addresses.add(self._endpoint(address))
                except OnvifError:
                    pass
            if namespace in (MEDIA1, MEDIA2) and address:
                endpoints.setdefault(namespace, self._endpoint(address))
        return endpoints

    def _profiles(self, endpoint: str, namespace: str):
        body = "<trt:GetProfiles/>" if namespace == MEDIA1 else "<tr2:GetProfiles><tr2:Type>All</tr2:Type></tr2:GetProfiles>"
        root = self._soap(endpoint, namespace, "GetProfiles", body)
        groups = {}
        seen = set()
        profiles = [node for node in root.iter() if _local(node) == "Profiles"][:MAX_PROFILES]
        for profile in profiles:
            token = profile.get("token", "")
            if not token or token in seen or len(token) > 1024:
                continue
            seen.add(token)
            source = next((node for node in profile.iter() if _local(node) in ("VideoSourceConfiguration", "VideoSource")), None)
            encoder = next((node for node in profile.iter() if _local(node) in ("VideoEncoderConfiguration", "VideoEncoder")), None)
            if source is None and encoder is None:
                continue
            source_token = _text(source, "SourceToken") if source is not None else ""
            configuration_token = source.get("token", "") if source is not None else ""
            # Without source identity, equal resolutions do not imply equal cameras.
            key = ("source", source_token) if source_token else (("configuration", configuration_token) if configuration_token else ("profile", token))
            width, height = 640, 360
            area = float("inf")
            if encoder is not None:
                try:
                    w, h = int(_text(encoder, "Width")), int(_text(encoder, "Height"))
                    if 0 < w <= 16384 and 0 < h <= 16384:
                        width, height, area = w, h, w * h
                except ValueError:
                    pass
            name = _text(source, "Name") if source is not None else ""
            name = name or _text(profile, "Name")
            groups.setdefault(key, []).append((area, token, name, width, height))
        return groups

    def _media_cameras(self, endpoint: str, namespace: str, nvr_id: str, nvr_name: str, *, on_camera=None) -> list[Camera]:
        groups = self._profiles(endpoint, namespace)
        cameras = []
        last_error = None
        for key, profiles in list(groups.items())[:MAX_SOURCES]:
            for _, token, name, width, height in sorted(profiles)[:3]:
                if namespace == MEDIA1:
                    body = (
                        '<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
                        '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
                        f'<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>'
                    )
                else:
                    body = f'<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol><tr2:ProfileToken>{escape(token)}</tr2:ProfileToken></tr2:GetStreamUri>'
                try:
                    root = self._soap(endpoint, namespace, "GetStreamUri", body)
                    uri = _repair_host(_text(root, "Uri"), self._host)
                    same_host = _host_key(urlsplit(uri).hostname) == _host_key(self._host)
                    identity = hashlib.sha256((nvr_id + "\0" + key[0] + "\0" + key[1]).encode()).hexdigest()[:24]
                    cameras.append(Camera(
                        id=identity, name=name or f"{nvr_name} {len(cameras) + 1}", uri=uri,
                        nvr_id=nvr_id, nvr_name=nvr_name,
                        username=self.username if same_host else "", password=self.password if same_host else "",
                        width=width, height=height,
                    ))
                    if on_camera is not None:
                        try:
                            on_camera(replace(cameras[-1]))
                        except Exception:
                            # A failed progress update must not discard a usable stream.
                            pass
                    break
                except _UnsupportedAction:
                    raise
                except OnvifError as error:
                    last_error = error
        if not cameras and last_error is not None:
            raise last_error
        return cameras

    def cameras(self, nvr_id: str, nvr_name: str, *, on_camera=None) -> list[Camera]:
        """Return one preferred low-resolution stream per identifiable video source.

        Calls are synchronous. Run discovery and this method off the UI thread.
        A client is not thread-safe; create one per recorder lookup.
        If provided, on_camera receives an independent Camera copy on the calling
        thread as each URI resolves, before later URI requests and friendly-name
        lookup. Dispatch UI work without blocking; callback exceptions are ignored.
        The returned cameras retain their IDs and include any friendly names.
        """
        self._deadline = time.monotonic() + DEVICE_TIMEOUT
        self._calls = 0
        self._clock_offset = 0.0
        try:
            self._sync_clock()
            endpoints = {}
            try:
                root = self._soap(self.address, DEVICE, "GetCapabilities", "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
                for node in root.iter():
                    if _local(node) == "Device":
                        address = _text(node, "XAddr")
                        if address:
                            try:
                                self.device_addresses.add(self._endpoint(address))
                            except OnvifError:
                                pass
                    if _local(node) in ("Media", "Media2"):
                        address = _text(node, "XAddr")
                        if address:
                            endpoints[MEDIA1 if _local(node) == "Media" else MEDIA2] = self._endpoint(address)
            except _UnsupportedAction:
                pass
            services_loaded = False
            if not endpoints:
                endpoints.update(self._services())
                services_loaded = True
            if MEDIA1 in endpoints:
                try:
                    cameras = self._media_cameras(endpoints[MEDIA1], MEDIA1, nvr_id, nvr_name, on_camera=on_camera)
                    if cameras:
                        self._friendly_names(cameras)
                        return cameras
                except _UnsupportedAction:
                    pass
            if MEDIA2 not in endpoints and not services_loaded:
                endpoints.update(self._services())
            if MEDIA2 in endpoints:
                cameras = self._media_cameras(endpoints[MEDIA2], MEDIA2, nvr_id, nvr_name, on_camera=on_camera)
                if cameras:
                    self._friendly_names(cameras)
                    return cameras
            raise OnvifError("No usable ONVIF video streams were found on this device.")
        finally:
            self._session.close()

    def _friendly_names(self, cameras: list[Camera]):
        candidates = []
        for camera in cameras:
            channel = re.fullmatch(r"/h26[45]Preview_(\d+)_(?:main|sub)/?", urlsplit(camera.uri).path, re.I)
            if channel and re.fullmatch(r"Video[SE]?[_ -]?\d+|Profile\d+[_ -](?:Main|Sub)Stream", camera.name, re.I):
                candidates.append((camera, int(channel[1]) - 1))
        if not candidates:
            return
        try:
            info = self._soap(self.address, DEVICE, "GetDeviceInformation", "<tds:GetDeviceInformation/>")
            if not is_reolink_model(_text(info, "Manufacturer"), _text(info, "Model")):
                return
        except OnvifError:
            return
        # Reolink's source tokens can differ from its actual channel numbers.
        # Its read-only web API provides names that ONVIF exposes only as VideoS_NNN.
        names = camera_names(self.address, self.username, self.password)
        for camera, channel in candidates:
            camera.name = names.get(channel, camera.name)
