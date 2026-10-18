import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from lookout.storage import ConfigError, ConfigStore, KeyringError, NVR, PasswordVault


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lookout-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "lookout" / "config.json"
        self.store = ConfigStore(self.path)
        self.records = [NVR("one", "Front recorder", "http://recorder/onvif/device_service", "admin"), NVR("two", "Back", "192.168.1.8:8000")]

    def write(self, data):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(data, encoding="utf-8")

    def test_missing_config_does_not_create_files(self):
        self.assertEqual(self.store.load(), [])
        self.assertFalse(self.path.parent.exists())

    def test_roundtrip_permissions_and_metadata_only(self):
        self.store.save(self.records)
        self.assertEqual(self.store.load(), self.records)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 1)
        for record in data["nvrs"]:
            self.assertEqual(set(record), {"id", "name", "address", "username"})
        self.assertNotIn("password", self.path.read_text())
        self.store.save([])
        self.assertEqual(self.store.load(), [])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_permissions_are_restricted(self):
        self.store.save(self.records)
        self.path.chmod(0o644)
        self.path.parent.chmod(0o755)
        self.store.save(self.records)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_bare_and_bracketed_ipv6_metadata(self):
        records = [NVR("one", "Front", "2001:db8::1"), NVR("two", "Back", "http://[2001:db8::2]:8080/onvif/device_service")]
        self.store.save(records)
        self.assertEqual(self.store.load(), records)

    def test_atomic_replace_failure_preserves_previous_file(self):
        self.store.save(self.records)
        original = self.path.read_bytes()
        with patch("lookout.storage.os.replace", side_effect=OSError("secret-path")):
            with self.assertRaises(ConfigError) as caught:
                self.store.save([])
        self.assertNotIn("secret-path", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_schema_and_json(self):
        valid = {"id": "id", "name": "Name", "address": "recorder", "username": ""}
        cases = [None, [], {}, {"version": 2, "nvrs": []}, {"version": True, "nvrs": []},
                 {"version": 1, "nvrs": {}}, {"version": 1, "nvrs": [None]},
                 {"version": 1, "nvrs": [valid, valid]}, {"version": 1, "nvrs": [valid], "password": "private"},
                 {"version": 1, "nvrs": [{**valid, "password": "private"}]},
                 {"version": 1, "nvrs": [{**valid, "username": 12}]},
                 {"version": 1, "nvrs": [{**valid, "name": ""}]},
                 {"version": 1, "nvrs": [{**valid, "id": "bad\nvalue"}]},
                 {"version": 1, "nvrs": [{**valid, "address": "http://u:private@r"}]},
                 {"version": 1, "nvrs": [valid] * 257}]
        for data in cases:
            with self.subTest(data=data):
                self.write(json.dumps(data))
                with self.assertRaises(ConfigError) as caught:
                    self.store.load()
                self.assertNotIn("private", str(caught.exception))
        for raw in ('not-json', '{"version":1,"version":1,"nvrs":[]}', "x" * (1024 * 1024 + 1), "[" * 2000):
            self.write(raw)
            with self.assertRaises(ConfigError):
                self.store.load()

    def test_invalid_save_does_not_touch_existing_config(self):
        self.store.save(self.records)
        before = self.path.read_bytes()
        invalid = [None, (), ["not an NVR"], [NVR("", "Name", "recorder")], self.records * 2,
                   [NVR("id", "Name", "rtsp://recorder/live")], [NVR("id", "Name", "http://u:private@r")],
                   [NVR("id", "Name", "http://r?password=private")], [NVR("id", "Name", "http://r:99999")],
                   [NVR("id", "Name", "bad host")], [NVR("id", "Name", "http://%72ecorder")]]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                self.store.save(data)
            self.assertEqual(self.path.read_bytes(), before)

    def test_default_path_uses_xdg_without_io(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.directory.name}):
            self.assertEqual(ConfigStore().path, Path(self.directory.name) / "lookout" / "config.json")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path"}), patch("lookout.storage.Path.home", return_value=Path(self.directory.name)):
            self.assertEqual(ConfigStore().path, Path(self.directory.name) / ".config" / "lookout" / "config.json")
        self.assertFalse(self.path.parent.exists())

    def test_symlink_directory_is_rejected(self):
        target = Path(self.directory.name) / "target"
        target.mkdir()
        self.path.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.store.save(self.records)
        self.assertEqual(list(target.iterdir()), [])


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.secret = Mock()
        self.secret.ServiceFlags.OPEN_SESSION = 1
        self.secret.ServiceFlags.LOAD_COLLECTIONS = 2
        self.secret.CollectionFlags.NONE = 0
        self.secret.SchemaFlags.NONE = 0
        self.secret.SchemaAttributeType.STRING = 0
        self.secret.COLLECTION_DEFAULT = "default"
        self.secret.Collection.for_alias_sync.return_value.get_locked.return_value = False
        self.secret.password_lookup_sync.return_value = None
        self.secret.password_store_sync.return_value = True
        self.gi = types.ModuleType("gi")
        self.gi.require_version = Mock()
        repository = types.ModuleType("gi.repository")
        repository.Secret = self.secret
        self.modules = patch.dict(sys.modules, {"gi": self.gi, "gi.repository": repository})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.vault = PasswordVault()

    def test_lazy_import_and_missing_password(self):
        self.gi.require_version.assert_not_called()
        self.assertEqual(self.vault.get("id"), "")
        self.gi.require_version.assert_called_once_with("Secret", "1")
        self.secret.password_lookup_sync.assert_called_once_with(self.secret.Schema.new.return_value, {"nvr-id": "id"}, None)

    def test_passwords_stored_only_via_secret_service(self):
        self.vault.set("id", "private-password")
        self.secret.password_store_sync.assert_called_once_with(self.secret.Schema.new.return_value, {"nvr-id": "id"}, "default", "Camera Panel recorder password", "private-password", None)
        self.secret.password_lookup_sync.return_value = "private-password"
        self.assertEqual(self.vault.get("id"), "private-password")
        self.vault.delete("id")
        self.secret.password_clear_sync.assert_called_once_with(self.secret.Schema.new.return_value, {"nvr-id": "id"}, None)
        self.secret.password_clear_sync.return_value = False
        self.vault.delete("missing-id")

    def test_locked_keyring_is_reported_for_all_operations(self):
        self.secret.Collection.for_alias_sync.return_value.get_locked.return_value = True
        for method, args in ((self.vault.get, ("id",)), (self.vault.set, ("id", "private-password")), (self.vault.delete, ("id",))):
            with self.assertRaisesRegex(KeyringError, "locked"):
                method(*args)
        self.secret.password_lookup_sync.assert_not_called()
        self.secret.password_store_sync.assert_not_called()
        self.secret.password_clear_sync.assert_not_called()

    def test_unavailable_service_or_collection(self):
        self.secret.Service.get_sync.return_value = None
        with self.assertRaisesRegex(KeyringError, "unavailable"):
            self.vault.get("id")
        self.secret.Service.get_sync.return_value = Mock()
        self.secret.Collection.for_alias_sync.return_value = None
        with self.assertRaisesRegex(KeyringError, "unavailable"):
            self.vault.get("id")

    def test_missing_bindings_have_no_fallback(self):
        with patch.dict(sys.modules, {"gi": None}):
            with self.assertRaisesRegex(KeyringError, "unavailable"):
                self.vault.set("id", "private-password")
        self.secret.password_store_sync.assert_not_called()

    def test_errors_never_echo_passwords(self):
        operations = ((self.vault.get, ("id",), self.secret.password_lookup_sync),
                      (self.vault.set, ("id", "private-password"), self.secret.password_store_sync),
                      (self.vault.delete, ("id",), self.secret.password_clear_sync))
        for method, args, operation in operations:
            operation.side_effect = RuntimeError("private-password")
            with self.assertRaises(KeyringError) as caught:
                method(*args)
            self.assertNotIn("private-password", str(caught.exception))
            self.assertIn("locked or unavailable", str(caught.exception))
            operation.side_effect = None
        self.secret.password_store_sync.return_value = False
        with self.assertRaises(KeyringError):
            self.vault.set("id", "private-password")

    def test_bad_values_are_rejected_before_keyring_access(self):
        for id in ("", " ", None, "a\x00b", "x" * 257):
            with self.assertRaises(KeyringError):
                self.vault.get(id)
        for password in (None, 123, "p\x00secret"):
            with self.assertRaises(KeyringError):
                self.vault.set("id", password)
        self.secret.Service.get_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
