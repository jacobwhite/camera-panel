"""Synthetic pixels only; no cameras, display server, or real cache access."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import random
import stat
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from lookout.onvif import Camera
from lookout.snapshots import Snapshot, SnapshotError, SnapshotStore
from lookout.storage import NVR, StorageError


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="camera-panel-snapshots-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "lookout" / "snapshots"
        self.store = SnapshotStore(self.directory)
        self.camera = Camera(
            "camera-1", "Front", "rtsp://uri-user:uri-password@never-contact.invalid/live?token=stream-secret",
            "nvr-1", "Home", "private-user", "private-password", width=1920, height=1080,
        )
        self.nvrs = [NVR("nvr-1", "Renamed recorder", "never-contact.invalid", "nvr-private-user")]
        self.rgb = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 12, 34, 56))
        self.timestamp = 1700000000.125

    def path(self, camera_id=None):
        identity = self.camera.id if camera_id is None else camera_id
        return self.directory / (hashlib.sha256(identity.encode()).hexdigest() + ".png")

    def save(self, camera=None, captured_at=None, rgb=None):
        self.store.save(
            self.camera if camera is None else camera,
            self.rgb if rgb is None else rgb,
            self.timestamp if captured_at is None else captured_at, 2, 2,
        )

    def chunks(self, png):
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        offset, result = 8, []
        while offset < len(png):
            length = struct.unpack_from(">I", png, offset)[0]
            kind = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + length]
            crc = struct.unpack_from(">I", png, offset + 8 + length)[0]
            self.assertEqual(crc, zlib.crc32(kind + data))
            result.append((kind, data))
            offset += length + 12
        self.assertEqual(offset, len(png))
        return result

    def rewrite(self, chunks):
        self.path().write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(chunk(kind, data) for kind, data in chunks))

    def test_roundtrip_rgb_metadata_and_no_credentials(self):
        self.save()
        snapshot = SnapshotStore(self.directory).load(self.camera.id)
        self.assertIsInstance(snapshot, Snapshot)
        self.assertEqual(snapshot.png, self.path().read_bytes())
        self.assertEqual(snapshot.captured_at, self.timestamp)
        self.assertEqual(snapshot.camera, replace(self.camera, uri="", username="", password="", width=2, height=2))
        self.assertEqual(self.camera.username, "private-user")
        chunks = self.chunks(snapshot.png)
        self.assertEqual([kind for kind, _ in chunks], [b"IHDR", b"tEXt", b"IDAT", b"IEND"])
        self.assertEqual(struct.unpack(">IIBBBBB", chunks[0][1]), (2, 2, 8, 2, 0, 0, 0))
        keyword, text = chunks[1][1].split(b"\0", 1)
        self.assertEqual(keyword, b"lookout")
        self.assertEqual(json.loads(text), {
            "version": 1, "camera_id": "camera-1", "name": "Front", "nvr_id": "nvr-1", "nvr_name": "Home",
            "captured_at": self.timestamp, "width": 2, "height": 2,
        })
        self.assertEqual(zlib.decompress(chunks[2][1]), b"\0" + self.rgb[:6] + b"\0" + self.rgb[6:])
        for secret in (self.camera.uri, self.camera.username, self.camera.password,
                       "uri-user", "uri-password", "stream-secret", "never-contact.invalid"):
            self.assertNotIn(secret.encode(), snapshot.png)
        self.assertEqual(chunks[3][1], b"")

    def test_default_size_and_unicode_names(self):
        camera = replace(self.camera, name="Entr\u00e9e \U0001f3e0", nvr_name="M\u00e9nage")
        self.store.save(camera, b"\x12\x34\x56" * (640 * 360), self.timestamp)
        snapshot = self.store.load(camera.id)
        self.assertEqual((snapshot.camera.width, snapshot.camera.height), (640, 360))
        self.assertEqual(snapshot.camera.name, camera.name)
        self.assertEqual(snapshot.camera.nvr_name, camera.nvr_name)

    def test_png_is_loadable_by_gdk_pixbuf_without_ui(self):
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
        except (ImportError, ValueError):
            self.skipTest("GdkPixbuf is not installed; stdlib decoding is covered separately")
        self.save()
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(self.path().read_bytes())
        loader.close()
        image = loader.get_pixbuf()
        self.assertEqual((image.get_width(), image.get_height(), image.get_n_channels()), (2, 2, 3))
        pixels = bytes(image.get_pixels())
        self.assertEqual(pixels[:6] + pixels[image.get_rowstride():image.get_rowstride() + 6], self.rgb)

    def test_permissions_and_atomic_replacement(self):
        self.save()
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.directory.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
        self.directory.chmod(0o755)
        self.path().chmod(0o644)
        self.save(captured_at=self.timestamp + 1)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path().stat().st_mode), 0o600)
        self.assertEqual(list(self.directory.iterdir()), [self.path()])
        original = self.path().read_bytes()
        with patch("lookout.snapshots.os.replace", side_effect=OSError("private-password")):
            with self.assertRaises(SnapshotError) as caught:
                self.save(captured_at=self.timestamp + 2)
        self.assertIsInstance(caught.exception, StorageError)
        self.assertNotIn("private-password", str(caught.exception))
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [self.path()])

    def test_missing_cache_and_default_paths_do_not_create_files(self):
        self.assertIsNone(self.store.load(self.camera.id))
        self.assertEqual(self.store.cached_cameras(self.nvrs), [])
        self.store.remove_nvr(self.camera.nvr_id)
        self.store.flush()
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root)}):
            self.assertEqual(SnapshotStore().directory, self.directory)
        for base in ("", "relative/path"):
            with patch.dict(os.environ, {"XDG_CACHE_HOME": base}), \
                    patch("lookout.snapshots.Path.home", return_value=self.root):
                self.assertEqual(SnapshotStore().directory, self.root / ".cache" / "lookout" / "snapshots")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_older_and_equal_frames_are_discarded_across_restart(self):
        self.save()
        original = self.path().read_bytes()
        self.store = SnapshotStore(self.directory)
        for timestamp in (self.timestamp - 1, self.timestamp):
            self.save(captured_at=timestamp, rgb=b"\xff" * 12)
            self.assertEqual(self.path().read_bytes(), original)
        self.save(captured_at=self.timestamp + 1, rgb=b"\xff" * 12)
        self.assertEqual(self.store.load(self.camera.id).captured_at, self.timestamp + 1)
        self.assertNotEqual(self.path().read_bytes(), original)

    def test_concurrent_saves_keep_newest_and_flush_waits(self):
        timestamps = [self.timestamp + offset for offset in (12, 3, 16, 1, 6, 9, 14, 2)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda timestamp: self.save(captured_at=timestamp), timestamps))
        self.assertEqual(self.store.load(self.camera.id).captured_at, max(timestamps))
        entered, release, flushed = threading.Event(), threading.Event(), threading.Event()
        real_replace = os.replace

        def blocked_replace(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Save did not get released")
            return real_replace(*args, **kwargs)

        def flush():
            self.store.flush()
            flushed.set()

        with patch("lookout.snapshots.os.replace", side_effect=blocked_replace), \
                ThreadPoolExecutor(max_workers=2) as executor:
            saving = executor.submit(self.save, captured_at=max(timestamps) + 1)
            try:
                self.assertTrue(entered.wait(2))
                flushing = executor.submit(flush)
                self.assertFalse(flushed.wait(0.05))
            finally:
                release.set()
            saving.result(timeout=2)
            flushing.result(timeout=2)
        self.assertTrue(flushed.is_set())

    def test_invalid_save_is_safe_and_preserves_existing_preview(self):
        self.save()
        original = self.path().read_bytes()
        cases = [
            {"camera": None}, {"rgb": None}, {"rgb": bytearray(self.rgb)}, {"rgb": self.rgb[:-1]},
            {"rgb": self.rgb + b"x"}, {"width": 0}, {"height": -1}, {"width": True},
            {"width": 2.0}, {"width": 4097}, {"width": 2048, "height": 2048},
            *({"captured_at": value} for value in (0, -1, float("nan"), float("inf"), -float("inf"), True, "secret", 10 ** 400)),
            *({"camera": replace(self.camera, **{field: value})}
              for field in ("id", "name", "nvr_id", "nvr_name")
              for value in ("", " ", "bad\0secret", "bad\nsecret", "x" * 257, "\ud800", None)),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                args = {"camera": self.camera, "rgb": self.rgb, "captured_at": self.timestamp + 1, "width": 2, "height": 2}
                args.update(invalid)
                with self.assertRaises(SnapshotError) as caught:
                    self.store.save(**args)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(self.path().read_bytes(), original)

    def test_malformed_oversized_and_truncated_files_are_ignored(self):
        self.save()
        original = self.path().read_bytes()
        cases = [b"", b"not png", b"x" * (2 * 1024 * 1024 + 1), original + b"trailing", original[:-1],
                 original[:33] + b"\xff\xff\xff\xff" + original[37:]]
        cases.extend(original[:length] for length in range(len(original)))
        damaged = bytearray(original)
        damaged[20] ^= 1
        cases.append(bytes(damaged))
        for index, raw in enumerate(cases):
            with self.subTest(index=index):
                self.path().write_bytes(raw)
                self.assertIsNone(self.store.load(self.camera.id))
        self.assertEqual(self.store.cached_cameras(self.nvrs), [])
        self.save(captured_at=self.timestamp + 1)
        self.assertIsNotNone(self.store.load(self.camera.id))

    def test_oversized_encoded_image_does_not_replace_existing_preview(self):
        self.save()
        original = self.path().read_bytes()
        rgb = random.Random(0).randbytes(1024 * 1024 * 3)
        with self.assertRaises(SnapshotError):
            self.store.save(self.camera, rgb, self.timestamp + 1, 1024, 1024)
        self.assertEqual(self.path().read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [self.path()])

    def test_invalid_metadata_and_png_pixel_data_are_ignored(self):
        self.save()
        original = self.chunks(self.path().read_bytes())
        metadata = json.loads(original[1][1].split(b"\0", 1)[1])
        invalid_metadata = [
            {**metadata, "password": "secret"}, {**metadata, "uri": "rtsp://secret"},
            {**metadata, "version": 2}, {**metadata, "version": True},
            {**metadata, "camera_id": "other"}, {**metadata, "width": 3},
            {**metadata, "height": 4097}, {**metadata, "width": 2048, "height": 2048},
            {**metadata, "captured_at": float("nan")}, {**metadata, "captured_at": 0},
            {**metadata, "captured_at": True}, {**metadata, "nvr_id": []}, {}, [], None,
        ]
        texts = [b"lookout\0" + json.dumps(data).encode() for data in invalid_metadata]
        texts.extend((b"wrong-keyword\0{}", b"lookout\0\xff", b"lookout\0" + b"[" * 2000,
                      b"lookout\0" + b" " * 16385,
                      b"lookout\0" + json.dumps(metadata).encode()[:-1] + b', "version": 1}'))
        for text in texts:
            with self.subTest(text=text[:80]):
                self.rewrite([original[0], (b"tEXt", text), *original[2:]])
                self.assertIsNone(self.store.load(self.camera.id))
        rows = zlib.decompress(original[2][1])
        bad_pixels = [b"invalid zlib", original[2][1][:-1], original[2][1] + b"trailing",
                      zlib.compress(rows[:-1]), zlib.compress(rows + b"x"),
                      zlib.compress(b"\1" + rows[1:]), zlib.compress(b"x" * (4 * 1024 * 1024))]
        for pixels in bad_pixels:
            with self.subTest(pixels=pixels[:30]):
                self.rewrite([*original[:2], (b"IDAT", pixels), original[3]])
                self.assertIsNone(self.store.load(self.camera.id))
        for header in (b"", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0), struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)):
            self.rewrite([(b"IHDR", header), *original[1:]])
            self.assertIsNone(self.store.load(self.camera.id))
        for chunks in (original + [(b"tEXt", b"password\0secret")],
                       [original[0], original[1], original[1], *original[2:]],
                       [*original[:3], (b"IEND", b"invalid")]):
            self.rewrite(chunks)
            self.assertIsNone(self.store.load(self.camera.id))

    def test_canonical_filenames_and_path_traversal(self):
        camera = replace(self.camera, id="../../escape")
        self.save(camera)
        self.assertIsNotNone(self.store.load(camera.id))
        self.assertEqual(list(self.directory.iterdir()), [self.path(camera.id)])
        self.assertFalse((self.root / "escape").exists())
        original = self.path(camera.id).read_bytes()
        self.path().write_bytes(original)
        (self.directory / "alias.png").write_bytes(original)
        self.assertIsNone(self.store.load(self.camera.id))
        self.assertEqual([item.id for item in self.store.cached_cameras(self.nvrs)], [camera.id])
        self.store.remove_nvr(camera.nvr_id)
        self.assertFalse(self.path(camera.id).exists())
        self.assertEqual(self.path().read_bytes(), original)
        self.assertTrue((self.directory / "alias.png").exists())
        for camera_id in (None, "", "\0", "\ud800", "x" * 257):
            self.assertIsNone(self.store.load(camera_id))

    def test_symlink_directories_and_ancestors_are_not_followed(self):
        target = self.root / "target"
        target.mkdir(mode=0o755)
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        for directory in (link, link / "nested", link / ".." / "escape"):
            store = SnapshotStore(directory)
            with self.subTest(directory=directory):
                self.assertIsNone(store.load(self.camera.id))
                self.assertEqual(store.cached_cameras(self.nvrs), [])
                with self.assertRaises(SnapshotError):
                    store.save(self.camera, self.rgb, self.timestamp, 2, 2)
                with self.assertRaises(SnapshotError):
                    store.remove_nvr(self.camera.nvr_id)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertFalse((self.root / "escape").exists())

    def test_symlink_files_nonregular_files_and_hardlinks_are_ignored(self):
        self.save()
        outside = self.root / "outside.png"
        original = self.path().read_bytes()
        self.path().rename(outside)
        self.path().symlink_to(outside)
        self.assertIsNone(self.store.load(self.camera.id))
        self.assertEqual(self.store.cached_cameras(self.nvrs), [])
        self.store.remove_nvr(self.camera.nvr_id)
        self.assertTrue(self.path().is_symlink())
        self.save(captured_at=self.timestamp + 1)
        self.assertFalse(self.path().is_symlink())
        self.assertEqual(outside.read_bytes(), original)
        self.path().unlink()
        os.link(outside, self.path())
        self.assertIsNone(self.store.load(self.camera.id))
        self.path().unlink()
        os.mkfifo(self.path())
        self.assertIsNone(self.store.load(self.camera.id))
        self.path().unlink()
        self.path().mkdir()
        self.assertIsNone(self.store.load(self.camera.id))

    def test_directory_swap_cannot_redirect_atomic_write(self):
        self.save()
        moved = self.root / "moved"
        outside = self.root / "outside"
        outside.mkdir()
        real_replace = os.replace

        def swap_directory(*args, **kwargs):
            self.directory.rename(moved)
            self.directory.symlink_to(outside, target_is_directory=True)
            return real_replace(*args, **kwargs)

        with patch("lookout.snapshots.os.replace", side_effect=swap_directory):
            self.save(captured_at=self.timestamp + 1)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(self.store.load(self.camera.id))
        self.assertEqual(SnapshotStore(moved).load(self.camera.id).captured_at, self.timestamp + 1)
        self.assertEqual(list(moved.iterdir()), [moved / self.path().name])

    def test_unreadable_cache_is_ignored_and_write_errors_are_safe(self):
        self.save()
        with patch("lookout.snapshots.os.open", side_effect=PermissionError("private-password")):
            self.assertIsNone(self.store.load(self.camera.id))
            self.assertEqual(self.store.cached_cameras(self.nvrs), [])
            for operation in (self.save, lambda: self.store.remove_nvr(self.camera.nvr_id)):
                with self.assertRaises(SnapshotError) as caught:
                    operation()
                self.assertNotIn("private-password", str(caught.exception))
        with patch("lookout.snapshots.os.fsync", side_effect=OSError("private-password")):
            with self.assertRaises(SnapshotError):
                self.save(captured_at=self.timestamp + 1)
        self.assertEqual(self.store.load(self.camera.id).captured_at, self.timestamp)
        self.assertEqual(list(self.directory.iterdir()), [self.path()])
        real_open = os.open

        def deny_preview(path, *args, **kwargs):
            if path == self.path().name:
                raise PermissionError("private-password")
            return real_open(path, *args, **kwargs)

        with patch("lookout.snapshots.os.open", side_effect=deny_preview):
            self.assertIsNone(self.store.load(self.camera.id))
            self.assertEqual(self.store.cached_cameras(self.nvrs), [])
            self.store.remove_nvr(self.camera.nvr_id)
        self.assertTrue(self.path().exists())

    def test_cached_cameras_filter_ids_and_use_current_nvr_names(self):
        back = replace(self.camera, id="camera-2", name="Back")
        removed = replace(self.camera, id="camera-3", nvr_id="removed", nvr_name="Renamed recorder")
        for camera in (self.camera, back, removed):
            self.save(camera)
        placeholders = SnapshotStore(self.directory).cached_cameras(self.nvrs)
        self.assertEqual([camera.id for camera in placeholders], [back.id, self.camera.id])
        self.assertEqual([camera.name for camera in placeholders], ["Back", "Front"])
        for camera in placeholders:
            self.assertEqual(camera.nvr_id, "nvr-1")
            self.assertEqual(camera.nvr_name, "Renamed recorder")
            self.assertEqual((camera.uri, camera.username, camera.password), ("", "", ""))
        self.assertEqual(self.store.cached_cameras([]), [])
        self.assertEqual(self.store.load(self.camera.id).camera.nvr_name, "Home")

    def test_remove_nvr_leaves_other_recorders_and_unrelated_files(self):
        own = replace(self.camera, id="camera-2")
        other = replace(self.camera, id="camera-3", nvr_id="nvr-10")
        for camera in (self.camera, own, other):
            self.save(camera)
        other_png = self.path(other.id).read_bytes()
        unrelated = self.directory / "unrelated.txt"
        unrelated.write_text("leave alone")
        self.store.remove_nvr(self.camera.nvr_id)
        self.store.remove_nvr(self.camera.nvr_id)
        self.assertIsNone(self.store.load(self.camera.id))
        self.assertIsNone(self.store.load(own.id))
        self.assertEqual(self.path(other.id).read_bytes(), other_png)
        self.assertEqual(unrelated.read_text(), "leave alone")
        self.assertEqual(set(self.directory.iterdir()), {self.path(other.id), unrelated})


if __name__ == "__main__":
    unittest.main()
