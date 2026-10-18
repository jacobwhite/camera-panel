"""Persistent, unencrypted local previews: private images, not a credential store.

PNG files contain only pixels and allowlisted camera identity/display metadata.
Permissions restrict local access, but do not encrypt images or camera names.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import threading
import zlib

from .onvif import Camera
from .storage import NVR, StorageError


_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_KEYWORD = b"lookout\0"
_MAX_FILE = 2 * 1024 * 1024
_MAX_PIXELS = 1024 * 1024
_FIELDS = {"version", "camera_id", "name", "nvr_id", "nvr_name", "captured_at", "width", "height"}


class SnapshotError(StorageError):
    """A snapshot error safe to display without exposing images or stored values."""


@dataclass(frozen=True)
class Snapshot:
    png: bytes
    captured_at: float
    camera: Camera


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError


def _filename(camera_id):
    _text(camera_id)
    return hashlib.sha256(camera_id.encode("utf-8")).hexdigest() + ".png"


def _metadata(data):
    if not isinstance(data, dict) or set(data) != _FIELDS or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError
    for key in ("camera_id", "name", "nvr_id", "nvr_name"):
        _text(data[key])
    for key in ("width", "height"):
        if type(data[key]) is not int or not 1 <= data[key] <= 4096:
            raise ValueError
    if data["width"] * data["height"] > _MAX_PIXELS:
        raise ValueError
    timestamp = data["captured_at"]
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


class SnapshotStore:
    """Synchronous, thread-safe storage of unencrypted local preview PNGs.

    Run disk operations off the UI thread. Each save is atomic and fsynced;
    there is no internal worker or deferred write queue. One store should own
    a directory; coordination between separate processes is not provided.
    """

    def __init__(self, directory: Path | None = None):
        if directory is None:
            base = os.environ.get("XDG_CACHE_HOME", "")
            root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".cache"
            directory = root / "lookout" / "snapshots"
        self.directory = Path(directory)
        self._lock = threading.Lock()

    @contextmanager
    def _open_directory(self, create=False):
        # Walk every component with O_NOFOLLOW, then keep all I/O relative to
        # the opened directory so a path swap cannot redirect file operations.
        path = self.directory.absolute()
        if ".." in path.parts or len(path.parts) < 2:
            raise OSError
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            if os.fstat(fd).st_uid != os.getuid():
                raise OSError
            if create:
                os.fchmod(fd, 0o700)
            yield fd
        finally:
            os.close(fd)

    def _read(self, directory_fd, filename):
        if re.fullmatch(r"[0-9a-f]{64}\.png", filename) is None:
            return None
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE
                        or info.st_uid != os.getuid() or info.st_nlink != 1):
                    return None
                png = handle.read(_MAX_FILE + 1)
            if len(png) > _MAX_FILE or not png.startswith(_SIGNATURE):
                return None
            offset, chunks = len(_SIGNATURE), []
            # Only accept our own small PNG dialect, including exactly one
            # metadata chunk. Unknown ancillary chunks could conceal secrets.
            for kind in (b"IHDR", b"tEXt", b"IDAT", b"IEND"):
                if offset + 12 > len(png):
                    return None
                length = struct.unpack_from(">I", png, offset)[0]
                end = offset + 8 + length
                if end + 4 > len(png) or png[offset + 4:offset + 8] != kind:
                    return None
                if zlib.crc32(png[offset + 4:end]) != struct.unpack_from(">I", png, end)[0]:
                    return None
                chunks.append(png[offset + 8:end])
                offset = end + 4
            header, text, compressed, trailer = chunks
            if offset != len(png) or trailer or len(text) > 16384 or not text.startswith(_KEYWORD):
                return None
            data = json.loads(text[len(_KEYWORD):].decode("ascii"), object_pairs_hook=_unique_object)
            _metadata(data)
            width, height = data["width"], data["height"]
            if (filename != _filename(data["camera_id"])
                    or header != struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)):
                return None
            stride = width * 3 + 1
            expected = stride * height
            decoder = zlib.decompressobj()
            rows = decoder.decompress(compressed, expected + 1)
            if (len(rows) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                    or any(rows[index] != 0 for index in range(0, expected, stride))):
                return None
            camera = Camera(
                id=data["camera_id"], name=data["name"], uri="",
                nvr_id=data["nvr_id"], nvr_name=data["nvr_name"], width=width, height=height,
            )
            return Snapshot(png, float(data["captured_at"]), camera)
        except (OSError, ValueError, OverflowError, RecursionError, zlib.error):
            return None

    def load(self, camera_id: str) -> Snapshot | None:
        """Return a validated preview, or None for missing/unreadable/unsafe data."""
        try:
            filename = _filename(camera_id)
            with self._lock, self._open_directory() as directory_fd:
                return self._read(directory_fd, filename)
        except (OSError, ValueError):
            return None

    def save(self, camera: Camera, rgb: bytes, captured_at: float, width: int = 640, height: int = 360) -> None:
        """Persist packed RGB8 pixels. Older or equal timestamps are discarded.

        Limits: 4096 per dimension, 1 megapixel (1024**2), 2 MiB encoded PNG.
        Invalid data or failed writes raise a credential-safe SnapshotError.
        """
        try:
            with self._lock:
                if not isinstance(camera, Camera):
                    raise ValueError
                data = {
                    "version": 1, "camera_id": camera.id, "name": camera.name,
                    "nvr_id": camera.nvr_id, "nvr_name": camera.nvr_name,
                    "captured_at": captured_at, "width": width, "height": height,
                }
                _metadata(data)
                if not isinstance(rgb, bytes) or len(rgb) != width * height * 3:
                    raise ValueError
                stride = width * 3
                rows = b"".join(b"\0" + rgb[start:start + stride] for start in range(0, len(rgb), stride))
                png = (_SIGNATURE
                       + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                       + _chunk(b"tEXt", _KEYWORD + json.dumps(data, ensure_ascii=True, allow_nan=False).encode("ascii"))
                       + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b""))
                if len(png) > _MAX_FILE:
                    raise ValueError
                filename = _filename(data["camera_id"])
                with self._open_directory(create=True) as directory_fd:
                    previous = self._read(directory_fd, filename)
                    if previous is not None and previous.captured_at >= captured_at:
                        return
                    temporary = ".lookout-" + secrets.token_hex(16) + ".tmp"
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            os.fchmod(handle.fileno(), 0o600)
                            handle.write(png)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                        temporary = None
                        os.fsync(directory_fd)
                    finally:
                        if temporary is not None:
                            try:
                                os.unlink(temporary, dir_fd=directory_fd)
                            except OSError:
                                pass
        except (OSError, ValueError, OverflowError, zlib.error):
            raise SnapshotError("Could not securely save the camera preview; check image data and cache access.") from None

    def cached_cameras(self, nvrs: list[NVR]) -> list[Camera]:
        """Offline placeholders for saved NVR IDs, using their current names."""
        names = {nvr.id: nvr.name for nvr in nvrs}
        cameras = []
        if not names:
            return cameras
        try:
            with self._lock, self._open_directory() as directory_fd:
                for filename in os.listdir(directory_fd):
                    snapshot = self._read(directory_fd, filename)
                    if snapshot is not None and snapshot.camera.nvr_id in names:
                        snapshot.camera.nvr_name = names[snapshot.camera.nvr_id]
                        cameras.append(snapshot.camera)
        except OSError:
            pass
        return sorted(cameras, key=lambda camera: (camera.nvr_name.casefold(), camera.name.casefold(), camera.id))

    def remove_nvr(self, nvr_id: str) -> None:
        """Remove only valid, canonically named previews belonging to this NVR."""
        try:
            _text(nvr_id)
            with self._lock, self._open_directory() as directory_fd:
                changed = False
                for filename in os.listdir(directory_fd):
                    snapshot = self._read(directory_fd, filename)
                    if snapshot is not None and snapshot.camera.nvr_id == nvr_id:
                        try:
                            os.unlink(filename, dir_fd=directory_fd)
                            changed = True
                        except FileNotFoundError:
                            pass
                if changed:
                    os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            raise SnapshotError("Could not remove the recorder's cached previews.") from None

    def flush(self) -> None:
        """Wait for active operations; drain the caller's executor before calling.

        Writes are already fsynced on return, so there are no buffered images.
        """
        with self._lock:
            pass
