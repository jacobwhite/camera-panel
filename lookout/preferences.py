"""Camera selection preferences, separate from recorder credentials and metadata."""

import json
import os
from pathlib import Path
import tempfile

from .storage import ConfigError, ConfigStore


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate preferences fields are not allowed.")
        result[key] = value
    return result


def _validate_ids(ids):
    if len(ids) > 8192:
        raise ConfigError("Too many excluded cameras (maximum 8192).")
    for value in ids:
        if (not isinstance(value, str) or not value.strip() or len(value) > 256
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ConfigError("Invalid excluded camera ID.")
    result = set(ids)
    if len(result) != len(ids):
        raise ConfigError("Excluded camera IDs must be unique.")
    return result


class PreferencesStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else ConfigStore().path.with_name("preferences.json")

    def load(self) -> set[str]:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return set()
        except OSError:
            raise ConfigError("Could not read camera preferences.") from None
        if len(raw) > 1024 * 1024:
            raise ConfigError("Camera preferences are too large.")
        try:
            data = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise ConfigError("Camera preferences are not valid JSON.") from None
        if not isinstance(data, dict) or type(data.get("version")) is not int:
            raise ConfigError("Invalid camera preferences.")
        if (data["version"] == 1 and set(data) == {"version", "feeds_per_row"}
                and type(data["feeds_per_row"]) is int and 0 <= data["feeds_per_row"] <= 256):
            # Shipped layout settings no longer affect camera selection or layout.
            return set()
        if (data["version"] != 2 or set(data) != {"version", "excluded_camera_ids"}
                or not isinstance(data["excluded_camera_ids"], list)):
            raise ConfigError("Invalid camera preferences.")
        return _validate_ids(data["excluded_camera_ids"])

    def save(self, excluded_camera_ids: set[str]) -> None:
        if not isinstance(excluded_camera_ids, set):
            raise ConfigError("Excluded camera IDs must be a set.")
        ids = _validate_ids(excluded_camera_ids)
        payload = json.dumps({"version": 2, "excluded_camera_ids": sorted(ids)}, ensure_ascii=True).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ConfigError("Camera preferences are too large.")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.is_symlink():
                raise ConfigError("The preferences directory must not be a symbolic link.")
            self.path.parent.chmod(0o700)
            fd, temporary = tempfile.mkstemp(prefix=".preferences-", dir=self.path.parent)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ConfigError("Could not save camera preferences.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def feed_layout(width, count):
    """Single-row columns, tile width, and compact video height in logical pixels."""
    if not count:
        return 0, 0, 0
    columns = count
    tile_width = max(1, (width - 18 - 4 * (columns - 1)) // columns)
    return columns, tile_width, min(144, max(8, round((tile_width - 2) * 9 / 16)))
