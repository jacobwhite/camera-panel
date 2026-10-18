"""Optional, read-only camera names for positively identified Reolink recorders."""

from __future__ import annotations

import ipaddress
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests


MAX_JSON = 256 * 1024
LOOKUP_TIMEOUT = 8.0
LOGOUT_TIMEOUT = 3.0


def is_reolink_model(manufacturer: str, model: str) -> bool:
    """Some RLN firmware reports the literal manufacturer name 'Manufacturer'."""
    return bool(
        isinstance(manufacturer, str) and re.search(r"\breolink\b", manufacturer, re.I)
        or isinstance(model, str) and re.fullmatch(
            r"RLN(?:8|16)-410(?:-E)?|RLN36|RLN12W", model.strip(), re.I,
        )
    )


def camera_names(address: str, username: str, password: str) -> dict[int, str]:
    """Return zero-based API channel names, or {} if this optional lookup fails.

    Call only after identifying Reolink via ONVIF. Match channels using advertised
    RTSP paths: h264Preview_02_sub is channel 1, not ONVIF VideoS_001's index.
    This synchronous lookup belongs off the UI thread; it never changes settings.
    """
    session = None
    token = None
    try:
        if not isinstance(address, str) or not all(isinstance(v, str) for v in (username, password)):
            return {}
        address = address.strip()
        if (not address or len(address) > 2048 or "\\" in address or "@" in address
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in address)):
            return {}
        if "://" not in address:
            try:
                if ipaddress.ip_address(address).version == 6:
                    address = f"[{address}]"
            except ValueError:
                pass
            address = "http://" + address
        parts = urlsplit(address)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or any(c in parts.hostname for c in "%/?#@")
                or parts.port is not None and not 1 <= parts.port <= 65535):
            return {}
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        # Reolink's ONVIF port is not its web API port. Never downgrade HTTPS.
        if parts.port not in (None, 8000):
            host += f":{parts.port}"
        endpoint = urlunsplit((parts.scheme, host, "/cgi-bin/api.cgi", "", ""))
        session = requests.Session()
        # Bypass ambient proxies and .netrc; credentials go only to this host.
        session.trust_env = False
        deadline = time.monotonic() + LOOKUP_TIMEOUT

        def request(command, param):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError
            response = None
            try:
                response = session.post(
                    endpoint, params={"cmd": command, "token": token or "null"},
                    json=[{"cmd": command, "action": 0, "param": param}],
                    timeout=(min(2.0, remaining / 2), min(3.0, remaining / 2)),
                    verify=True, allow_redirects=False, stream=True,
                )
                if response.status_code != 200:
                    raise ValueError
                data = bytearray()
                for chunk in response.iter_content(16384):
                    if time.monotonic() >= deadline or len(data) + len(chunk) > MAX_JSON:
                        raise ValueError
                    data.extend(chunk)
                result = json.loads(data)
                if (not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict)
                        or result[0].get("cmd") != command or type(result[0].get("code")) is not int
                        or result[0]["code"] != 0 or not isinstance(result[0].get("value"), dict)):
                    raise ValueError
                return result[0]["value"]
            finally:
                if response is not None:
                    response.close()

        login = request("Login", {"User": {"userName": username, "password": password}})
        auth = login.get("Token")
        candidate = auth.get("name") if isinstance(auth, dict) else None
        if not isinstance(candidate, str) or not candidate or len(candidate) > 4096:
            return {}
        token = candidate
        statuses = request("GetChannelstatus", {}).get("status")
        if not isinstance(statuses, list) or len(statuses) > 256:
            return {}
        names = {}
        for status in statuses:
            if not isinstance(status, dict):
                continue
            channel, name = status.get("channel"), status.get("name")
            if type(channel) is not int or not 0 <= channel < 256 or not isinstance(name, str):
                continue
            name = name.strip()
            if (name and len(name) <= 256
                    and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name)):
                names.setdefault(channel, name)
        return names
    except (requests.RequestException, OSError, ValueError, TypeError, RecursionError):
        return {}
    finally:
        if session is not None:
            try:
                if token is not None:
                    # Give logout its own small budget even after a lookup timeout.
                    deadline = time.monotonic() + LOGOUT_TIMEOUT
                    request("Logout", {})
            except (requests.RequestException, OSError, ValueError, TypeError, RecursionError):
                pass
            finally:
                try:
                    session.close()
                except (requests.RequestException, OSError, ValueError):
                    pass
