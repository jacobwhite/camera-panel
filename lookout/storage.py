"""Validated metadata storage and Secret Service passwords, with no plaintext fallback."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


class StorageError(Exception):
    """A storage error safe to display without revealing stored values."""


class ConfigError(StorageError):
    pass


class KeyringError(StorageError):
    pass


@dataclass
class NVR:
    id: str
    name: str
    address: str
    username: str = ""


def _record(value) -> NVR:
    if not isinstance(value, dict) or set(value) != {"id", "name", "address", "username"}:
        raise ConfigError("Invalid recorder metadata fields; passwords must not be stored in the config.")
    for key, limit in (("id", 256), ("name", 256), ("address", 2048), ("username", 1024)):
        text = value[key]
        if (not isinstance(text, str) or len(text) > limit
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in text)):
            raise ConfigError("Invalid recorder metadata value.")
        if key != "username" and not text.strip():
            raise ConfigError("Recorder ID, name, and address are required.")
    address = value["address"]
    try:
        # Addresses are metadata, never a second route for saving URL passwords.
        if any(c.isspace() for c in address) or "\\" in address or "@" in address:
            raise ValueError
        try:
            if ipaddress.ip_address(address).version == 6:
                address = f"[{address}]"
        except ValueError:
            pass
        parts = urlsplit(address if "://" in address else "http://" + address)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
            raise ValueError
        if parts.port is not None and not 0 < parts.port <= 65535:
            raise ValueError
        if any(c in parts.hostname for c in "%/?#"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ConfigError("Invalid recorder address; use HTTP(S) without embedded credentials or URL queries.") from None
    return NVR(**value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate config fields are not allowed.")
        result[key] = value
    return result


class ConfigStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            base = os.environ.get("XDG_CONFIG_HOME", "")
            directory = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
            path = directory / "lookout" / "config.json"
        self.path = Path(path)

    def _validate(self, data) -> list[NVR]:
        if (not isinstance(data, dict) or set(data) != {"version", "nvrs"}
                or type(data["version"]) is not int or data["version"] != 1):
            raise ConfigError("Unsupported or invalid Camera Panel config schema.")
        if not isinstance(data["nvrs"], list) or len(data["nvrs"]) > 256:
            raise ConfigError("Invalid recorder list (maximum 256 recorders).")
        records = [_record(value) for value in data["nvrs"]]
        if len({record.id for record in records}) != len(records):
            raise ConfigError("Recorder IDs must be unique.")
        return records

    def load(self) -> list[NVR]:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return []
        except OSError:
            raise ConfigError("Could not read the Camera Panel config.") from None
        if len(raw) > 1024 * 1024:
            raise ConfigError("Camera Panel config is too large.")
        try:
            data = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ConfigError("Camera Panel config is not valid JSON.") from None
        return self._validate(data)

    def save(self, nvrs: list[NVR]):
        if not isinstance(nvrs, list) or any(not isinstance(nvr, NVR) for nvr in nvrs):
            raise ConfigError("Expected a list of NVR metadata records.")
        data = {"version": 1, "nvrs": [
            {"id": nvr.id, "name": nvr.name, "address": nvr.address, "username": nvr.username}
            for nvr in nvrs
        ]}
        self._validate(data)
        payload = (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ConfigError("Camera Panel config is too large.")
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.parent.is_symlink():
                raise ConfigError("The Camera Panel config directory must not be a symbolic link.")
            self.path.parent.chmod(0o700)
            fd, temporary = tempfile.mkstemp(prefix=".lookout-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise ConfigError("Could not securely save the Camera Panel config.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class PasswordVault:
    """Passwords are held only by the desktop Secret Service, under recorder IDs."""

    def _service(self):
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError):
            raise KeyringError("Secret Service is unavailable. Install PyGObject and libsecret and start a desktop keyring.") from None
        try:
            service = Secret.Service.get_sync(Secret.ServiceFlags.OPEN_SESSION | Secret.ServiceFlags.LOAD_COLLECTIONS, None)
            if service is None:
                raise KeyringError("Secret Service is unavailable. Start a desktop keyring.")
            collection = Secret.Collection.for_alias_sync(service, Secret.COLLECTION_DEFAULT, Secret.CollectionFlags.NONE, None)
            if collection is None:
                raise KeyringError("The default Secret Service keyring is unavailable. Create or unlock a desktop keyring.")
            if collection.get_locked():
                raise KeyringError("The Secret Service keyring is locked. Unlock it and try again.")
            schema = Secret.Schema.new("io.github.lookout.NVR", Secret.SchemaFlags.NONE, {"nvr-id": Secret.SchemaAttributeType.STRING})
            return Secret, schema
        except KeyringError:
            raise
        except Exception:
            raise KeyringError("Secret Service is locked or unavailable. Unlock or start the desktop keyring.") from None

    def _attributes(self, id: str):
        if not isinstance(id, str) or not id.strip() or len(id) > 256 or any(ord(c) < 32 for c in id):
            raise KeyringError("A valid recorder ID is required for keyring access.")
        return {"nvr-id": id}

    def get(self, id: str) -> str:
        attributes = self._attributes(id)
        Secret, schema = self._service()
        try:
            return Secret.password_lookup_sync(schema, attributes, None) or ""
        except Exception:
            raise KeyringError("Could not read the password. The Secret Service keyring may be locked or unavailable.") from None

    def set(self, id: str, password: str):
        attributes = self._attributes(id)
        if not isinstance(password, str) or "\x00" in password:
            raise KeyringError("The password must be text without null characters.")
        Secret, schema = self._service()
        try:
            saved = Secret.password_store_sync(schema, attributes, Secret.COLLECTION_DEFAULT, "Camera Panel recorder password", password, None)
            if not saved:
                raise KeyringError("Could not save the password. Unlock the Secret Service keyring and try again.")
        except KeyringError:
            raise
        except Exception:
            raise KeyringError("Could not save the password. The Secret Service keyring may be locked or unavailable.") from None

    def delete(self, id: str):
        attributes = self._attributes(id)
        Secret, schema = self._service()
        try:
            Secret.password_clear_sync(schema, attributes, None)
        except Exception:
            raise KeyringError("Could not delete the password. The Secret Service keyring may be locked or unavailable.") from None
