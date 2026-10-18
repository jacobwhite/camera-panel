import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from lookout.preferences import PreferencesStore, feed_layout
from lookout.storage import ConfigError


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lookout-preferences-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "lookout" / "preferences.json"
        self.store = PreferencesStore(self.path)

    def test_missing_preferences_enable_all_cameras_without_writing(self):
        self.assertEqual(self.store.load(), set())
        self.assertFalse(self.path.parent.exists())

    def test_default_path_uses_config_directory_without_config_io(self):
        with patch("lookout.preferences.ConfigStore") as config:
            config.return_value.path = self.path.with_name("config.json")
            self.assertEqual(PreferencesStore().path, self.path)
            config.assert_called_once_with()
            config.return_value.load.assert_not_called()
            config.return_value.save.assert_not_called()
        self.assertFalse(self.path.parent.exists())

    def test_remove_and_add_cameras_survives_restart(self):
        excluded = {"camera-z", "camera-a"}
        self.assertIsNone(self.store.save(excluded))
        self.assertEqual(PreferencesStore(self.path).load(), excluded)
        self.assertEqual(json.loads(self.path.read_bytes()), {
            "version": 2, "excluded_camera_ids": ["camera-a", "camera-z"],
        })
        excluded = PreferencesStore(self.path).load()
        excluded.add("camera-b")
        self.store.save(excluded)
        self.assertEqual(PreferencesStore(self.path).load(), {"camera-a", "camera-b", "camera-z"})
        excluded.remove("camera-a")
        self.store.save(excluded)
        self.assertEqual(PreferencesStore(self.path).load(), {"camera-b", "camera-z"})
        self.store.save(set())
        self.assertEqual(PreferencesStore(self.path).load(), set())
        self.assertEqual(json.loads(self.path.read_bytes()), {"version": 2, "excluded_camera_ids": []})

    def test_valid_v1_ignores_layout_without_rewriting_until_save(self):
        self.path.parent.mkdir()
        for columns in (0, 1, 4, 256):
            with self.subTest(columns=columns):
                original = json.dumps({"version": 1, "feeds_per_row": columns}).encode("utf-8")
                self.path.write_bytes(original)
                before = self.path.stat()
                self.assertEqual(self.store.load(), set())
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(self.path.stat().st_ino, before.st_ino)
                self.store.save(set())
                self.assertEqual(json.loads(self.path.read_bytes()), {
                    "version": 2, "excluded_camera_ids": [],
                })

    def test_id_length_and_count_boundaries(self):
        for excluded in ({"x" * 256, "cam-\u00e9", "cam-\U0001f4f7"},
                         {f"camera-{index}" for index in range(8192)}):
            self.store.save(excluded)
            self.assertEqual(self.store.load(), excluded)

    def test_invalid_schemas(self):
        self.path.parent.mkdir()
        cases = [None, [], {}, {"version": 2},
                 {"version": True, "excluded_camera_ids": []},
                 {"version": 2.0, "excluded_camera_ids": []},
                 {"version": 3, "excluded_camera_ids": []},
                 {"version": 2, "excluded_camera_ids": [], "password": "private"},
                 {"version": 2, "feeds_per_row": 4},
                 {"version": 1, "excluded_camera_ids": []},
                 {"version": 1, "feeds_per_row": 4, "excluded_camera_ids": []}]
        cases.extend({"version": 1, "feeds_per_row": value} for value in (-1, 257, True, 1.0, "4", None))
        cases.extend({"version": 2, "excluded_camera_ids": value} for value in (
            None, True, 4, "camera", {}, ["camera", "camera"], [None], [False], [4], [[]], [{}],
            [""], [" "], ["x" * 257], ["private\x00"], ["private\n"], ["private\x7f"],
            ["private\x85"], ["private\ud800"], [f"camera-{index}" for index in range(8193)],
        ))
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                original = json.dumps(value).encode("utf-8")
                self.path.write_bytes(original)
                with self.assertRaises(ConfigError) as caught:
                    self.store.load()
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_json_and_duplicate_fields(self):
        self.path.parent.mkdir()
        for raw in (b"", b"private", b"\xff", b"[" * 2000,
                    b'{"version":2,"version":2,"excluded_camera_ids":[]}',
                    b'{"version":2,"excluded_camera_ids":[],"excluded_camera_ids":[]}',
                    b'{"version":1,"feeds_per_row":0,"feeds_per_row":1}'):
            with self.subTest(raw=raw[:80]):
                self.path.write_bytes(raw)
                with self.assertRaises(ConfigError) as caught:
                    self.store.load()
                self.assertNotIn("private", str(caught.exception))

    def test_json_read_size_limit(self):
        self.path.parent.mkdir()
        raw = b'{"version":2,"excluded_camera_ids":[]}'.ljust(1024 * 1024, b" ")
        self.path.write_bytes(raw)
        self.assertEqual(self.store.load(), set())
        self.path.write_bytes(raw + b" ")
        with self.assertRaises(ConfigError):
            self.store.load()

    def test_invalid_save_preserves_previous_preferences(self):
        self.store.save({"camera-a"})
        original = self.path.read_bytes()
        cases = [None, 4, True, "camera", [], (), {}, frozenset({"camera"}),
                 {None}, {False}, {4}, {""}, {" "}, {"x" * 257}, {"private\x00"},
                 {"private\n"}, {"private\x7f"}, {"private\x85"}, {"private\udfff"},
                 {f"camera-{index}" for index in range(8193)},
                 {f"{index:04d}" + "x" * 252 for index in range(4096)},
                 {f"{index:04d}" + "\U0001f4f7" * 252 for index in range(512)}]
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ConfigError) as caught:
                    self.store.save(value)
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_save_does_not_create_directory(self):
        with self.assertRaises(ConfigError):
            self.store.save({""})
        self.assertFalse(self.path.parent.exists())

    def test_private_permissions_on_new_and_existing_paths(self):
        self.store.save({"camera-a"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.path.chmod(0o644)
        self.path.parent.chmod(0o755)
        self.store.save(set())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_symlink_parent_is_rejected(self):
        target = Path(self.directory.name) / "target"
        target.mkdir(mode=0o755)
        original = b'{"version":2,"excluded_camera_ids":["camera-a"]}'
        (target / self.path.name).write_bytes(original)
        mode = target.stat().st_mode
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.store.save(set())
        self.assertEqual((target / self.path.name).read_bytes(), original)
        self.assertEqual(target.stat().st_mode, mode)
        self.assertEqual(list(target.iterdir()), [target / self.path.name])

    def test_failed_save_preserves_previous_preferences_and_cleans_up(self):
        self.store.save({"camera-a"})
        original = self.path.read_bytes()
        for operation in ("mkstemp", "fchmod", "fsync", "replace"):
            module = "tempfile" if operation == "mkstemp" else "os"
            with self.subTest(operation=operation):
                with patch(f"lookout.preferences.{module}.{operation}", side_effect=OSError("private")):
                    with self.assertRaises(ConfigError) as caught:
                        self.store.save({"camera-b"})
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_read_errors_are_credential_safe(self):
        with patch("lookout.preferences.Path.open", side_effect=OSError("private")):
            with self.assertRaises(ConfigError) as caught:
                self.store.load()
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_fixed_single_row_compact_dimensions(self):
        for width in (360, 800, 1900):
            self.assertEqual(feed_layout(width, 0), (0, 0, 0))
            for count in range(1, 33):
                with self.subTest(width=width, count=count):
                    columns, tile_width, height = feed_layout(width, count)
                    self.assertEqual(columns, count)
                    self.assertGreaterEqual(tile_width, 1)
                    self.assertLessEqual(tile_width * columns + 4 * (columns - 1) + 18, width)
                    self.assertGreaterEqual(height, 8)
                    self.assertLessEqual(height, 144)
        self.assertEqual(feed_layout(360, 1), (1, 342, 144))
        self.assertEqual(feed_layout(360, 4), (4, 82, 45))
        self.assertEqual(feed_layout(360, 32), (32, 6, 8))
        self.assertEqual(feed_layout(1900, 8), (8, 231, 129))
        with self.assertRaises(TypeError):
            feed_layout(1900, 8, 4)


if __name__ == "__main__":
    unittest.main()
