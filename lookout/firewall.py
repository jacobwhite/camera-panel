"""Read-only discovery diagnosis, with a separate, explicitly authorized UFW action."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

from .onvif import DISCOVERY_PORT
from .storage import StorageError


class FirewallError(StorageError):
    """A firewall error safe to display without command output or local data."""


@dataclass(frozen=True)
class DiscoveryPermission:
    interface: str
    subnet: str
    confirmed: bool = False

    @property
    def key(self) -> str:
        return f"{self.interface}:{self.subnet}"


def _network(rule: DiscoveryPermission) -> ipaddress.IPv4Network:
    try:
        if (not isinstance(rule, DiscoveryPermission) or not isinstance(rule.interface, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", rule.interface)
                or not isinstance(rule.subnet, str)):
            raise ValueError
        network = ipaddress.IPv4Network(rule.subnet)
        if (str(network) != rule.subnet or network.prefixlen > 30
                or not any(network.subnet_of(ipaddress.IPv4Network(lan)) for lan in
                           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))):
            raise ValueError
        return network
    except (ValueError, TypeError):
        raise FirewallError("Invalid discovery permission; select a current local IPv4 network.") from None


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _setting(text: str, name: str) -> str:
    values = re.findall(r"(?m)^\s*" + name + r"\s*=\s*(['\"]?)([A-Za-z]+)\1\s*(?:#.*)?$", text)
    return values[-1][1].lower() if values else ""


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=5, check=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _networks() -> dict[DiscoveryPermission, set[str]]:
    try:
        interfaces = json.loads(_run(["ip", "-j", "-d", "-4", "addr", "show", "up"]) or "[]")
        routes = json.loads(_run(["ip", "-j", "-4", "route", "show", "default"]) or "[]")
        if not isinstance(interfaces, list) or not isinstance(routes, list):
            return {}
    except (ValueError, RecursionError):
        return {}
    defaults = {route.get("dev") for route in routes if isinstance(route, dict)
                and route.get("dst") == "default" and isinstance(route.get("dev"), str)}
    networks = {}
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        name = interface.get("ifname", "")
        flags = interface.get("flags", [])
        info = interface.get("linkinfo", {})
        if (not isinstance(name, str) or not isinstance(flags, list) or not isinstance(info, dict)
                or interface.get("link_type") != "ether" or info.get("info_kind", "") not in ("", "vlan")
                or not all(flag in flags for flag in ("UP", "BROADCAST", "MULTICAST"))
                or any(flag in flags for flag in ("LOOPBACK", "POINTOPOINT"))
                or interface.get("operstate") == "DOWN"
                or name.startswith(("docker", "veth", "br-", "virbr", "tun", "tap", "wg", "tailscale",
                                    "zt", "vpn", "cni", "flannel", "podman", "vbox", "vmnet"))
                or not (name.startswith(("en", "eth", "wl")) or name in defaults or interface.get("parentbus"))):
            continue
        addresses = interface.get("addr_info", [])
        if not isinstance(addresses, list):
            continue
        for address in addresses:
            if not isinstance(address, dict) or address.get("family") != "inet":
                continue
            try:
                local = ipaddress.IPv4Interface(f"{address['local']}/{address['prefixlen']}")
                rule = DiscoveryPermission(name, str(local.network))
                _network(rule)
                if local.ip not in (local.network.network_address, local.network.broadcast_address):
                    networks.setdefault(rule, set()).add(str(local.ip))
            except (KeyError, ValueError, FirewallError):
                continue
    return networks


def _port_matches(value: str, port: int) -> bool:
    for item in value.split(","):
        if not re.fullmatch(r"\d+(?::\d+)?", item):
            raise ValueError
        bounds = item.split(":")
        if int(bounds[0]) <= port <= int(bounds[-1]):
            return True
    return False


def _already_allowed(text: str, rule: DiscoveryPermission, addresses: set[str]) -> bool:
    """Recognize simple covering accepts, not a general firewall evaluator.

    An earlier possibly relevant deny or unsupported rule prevents certainty.
    Narrower source accepts cannot establish that the whole LAN is allowed.
    """
    network = _network(rule)
    aliases = {"--protocol": "-p", "--source": "-s", "--destination": "-d", "--in-interface": "-i",
               "--source-port": "--sport", "--destination-port": "--dport",
               "--sports": "--sport", "--dports": "--dport", "--jump": "-j"}
    for line in text.splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            return False
        if tokens[:2] != ["-A", "ufw-user-input"]:
            continue
        tokens = tokens[2:]
        if "!" in tokens:
            return False
        fields = {}
        simple = len(tokens) % 2 == 0
        for option, value in zip(tokens[::2], tokens[1::2]):
            option = aliases.get(option, option)
            if option == "-m":
                simple &= value in ("udp", "comment", "multiport")
            elif option == "--comment":
                continue
            else:
                simple &= option in ("-p", "-s", "-d", "-i", "--sport", "--dport", "-j") and option not in fields
                fields[option] = value
        try:
            source = ipaddress.IPv4Network(fields.get("-s", "0.0.0.0/0"), strict=False)
            destination = ipaddress.IPv4Network(fields.get("-d", "0.0.0.0/0"), strict=False)
            incoming = fields.get("-i", rule.interface)
            if (fields.get("-p", "all") not in ("udp", "17", "all", "0")
                    or not (rule.interface.startswith(incoming[:-1]) if incoming.endswith("+") else incoming == rule.interface)
                    or not network.overlaps(source)
                    or not any(ipaddress.IPv4Address(ip) in destination for ip in addresses)
                    or not _port_matches(fields.get("--sport", "0:65535"), 3702)
                    or not _port_matches(fields.get("--dport", "0:65535"), DISCOVERY_PORT)):
                continue
            if not simple or fields.get("-j") != "ACCEPT":
                return False
            if network.subnet_of(source) and all(ipaddress.IPv4Address(ip) in destination for ip in addresses):
                return True
        except ValueError:
            return False
    return False


def _blocked(since: float, networks: dict[DiscoveryPermission, set[str]]) -> set[str]:
    output = _run(["journalctl", "--kernel", "--boot", "--since", f"@{since:.6f}",
                   "--output=json", "--no-pager", "--quiet", "--lines=1000"])
    blocked = set()
    for line in (output or "").splitlines():
        try:
            event = json.loads(line)
            timestamp = float(event["__REALTIME_TIMESTAMP"]) / 1_000_000
            message = event["MESSAGE"]
            if (event.get("_TRANSPORT") != "kernel" or not math.isfinite(timestamp) or timestamp < since
                    or not isinstance(message, str) or "[UFW BLOCK] " not in message):
                continue
            pairs = re.findall(r"\b(IN|OUT|SRC|DST|PROTO|SPT|DPT)=([^\s]*)", message)
            fields = dict(pairs)
            if (len(fields) != len(pairs) or fields.get("PROTO") != "UDP" or fields.get("SPT") != "3702"
                    or fields.get("DPT") != str(DISCOVERY_PORT) or fields.get("OUT") != ""):
                continue
            source = ipaddress.IPv4Address(fields["SRC"])
            for rule, addresses in networks.items():
                network = _network(rule)
                if (fields.get("IN") == rule.interface and fields.get("DST") in addresses and source in network
                        and str(source) not in addresses and source not in (network.network_address, network.broadcast_address)):
                    blocked.add(rule.key)
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            continue
    return blocked


class FirewallSetup:
    def __init__(self, state_path: Path | None = None):
        if state_path is None:
            base = os.environ.get("XDG_STATE_HOME", "")
            directory = Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "state"
            state_path = directory / "lookout" / "discovery.json"
        self.state_path = Path(state_path)

    def _reviewed(self) -> set[str]:
        try:
            with self.state_path.open("rb") as handle:
                raw = handle.read(65537)
        except FileNotFoundError:
            return set()
        except OSError:
            raise FirewallError("Could not read saved discovery firewall decisions.") from None
        try:
            data = json.loads(raw)
            if (len(raw) > 65536 or not isinstance(data, dict) or set(data) != {"version", "reviewed"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["reviewed"], list)
                    or any(not isinstance(key, str) or len(key) > 64 for key in data["reviewed"])):
                raise ValueError
            return set(data["reviewed"])
        except (ValueError, UnicodeError, RecursionError):
            raise FirewallError("Saved discovery firewall decisions are invalid.") from None

    def check(self, since: float, force: bool = False) -> list[DiscoveryPermission]:
        """Call in a worker after zero discovery replies, or for explicit settings review.

        ``since`` is the scan's Unix start time. Missing/rate-limited journal
        evidence never confirms a block: enabled UFW with default DROP is only
        a fallback suspicion. Readable config is not a full live ruleset audit.
        """
        try:
            if type(since) not in (int, float) or not math.isfinite(since) or since < 0:
                raise ValueError
        except (ValueError, OverflowError):
            raise FirewallError("Invalid discovery scan time.") from None
        if not os.access("/usr/bin/ufw", os.X_OK):
            return []
        if _setting(_read("/etc/ufw/ufw.conf"), "ENABLED") != "yes":
            return []
        reviewed = set() if force else self._reviewed()
        networks = {rule: addresses for rule, addresses in _networks().items()
                    if rule.key not in reviewed}
        if not networks:
            return []
        blocked = _blocked(since, networks)
        default_drop = _setting(_read("/etc/default/ufw"), "DEFAULT_INPUT_POLICY") == "drop"
        rules = _read("/etc/ufw/user.rules")
        # Fresh kernel evidence outweighs an accept in a potentially stale config.
        return [DiscoveryPermission(rule.interface, rule.subnet, rule.key in blocked)
                for rule, addresses in networks.items()
                if rule.key in blocked or (default_drop and not _already_allowed(rules, rule, addresses))]

    def remember(self, rule: DiscoveryPermission) -> None:
        """Record review only when the UI declines or has successfully called allow."""
        _network(rule)
        reviewed = self._reviewed() | {rule.key}
        payload = (json.dumps({"version": 1, "reviewed": sorted(reviewed)}) + "\n").encode("utf-8")
        if len(payload) > 65536:
            raise FirewallError("Too many saved discovery firewall decisions.")
        temporary = None
        try:
            self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.state_path.parent.is_symlink():
                raise FirewallError("The discovery state directory must not be a symbolic link.")
            fd, temporary = tempfile.mkstemp(prefix=".discovery-", suffix=".tmp", dir=self.state_path.parent)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            temporary = None
        except OSError:
            raise FirewallError("Could not securely save discovery firewall decisions.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def allow(self, rule: DiscoveryPermission) -> None:
        """Request exactly one narrow rule through the desktop authorization agent.

        Success does not remember the decision; that remains the caller's job.
        """
        _network(rule)
        if DiscoveryPermission(rule.interface, rule.subnet) not in _networks():
            raise FirewallError("The local network changed. Review discovery firewall access again.")
        if not os.access("/usr/bin/ufw", os.X_OK) or _setting(_read("/etc/ufw/ufw.conf"), "ENABLED") != "yes":
            raise FirewallError("UFW is unavailable or disabled; no discovery rule was requested.")
        argv = ["pkexec", "/usr/bin/ufw", "allow", "in", "on", rule.interface, "proto", "udp",
                "from", rule.subnet, "port", "3702", "to", "any", "port", str(DISCOVERY_PORT),
                 "comment", "Camera Panel ONVIF discovery"]
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            raise FirewallError("Firewall authorization timed out. Review the firewall before retrying.") from None
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise FirewallError("Could not request firewall authorization. Check UFW and the desktop authorization agent.") from None
        if result.returncode != 0:
            raise FirewallError("The discovery rule was not authorized or UFW could not add it. No decision was saved.")
