"""Controller regression tests, without opening windows or using the real LAN."""

import inspect
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from lookout.app import Application, CameraTile, Editor, Panel, Settings
from lookout.onvif import Camera, DiscoveredDevice
from lookout.preferences import PreferencesStore
from lookout.storage import NVR, StorageError
from lookout.firewall import DiscoveryPermission, FirewallError


class CameraSelectionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [Camera("front", "Front", "rtsp://192.0.2.1/front", "one", "Home"),
                        Camera("back", "Back", "rtsp://192.0.2.1/back", "one", "Home")]
        self.app = MagicMock(demo=False, preferences_busy=False, scanning=False,
                             generation=0, stopping=False, scan_message="")
        self.app.nvrs = [NVR("one", "Home", "192.0.2.1")]
        self.app.camera_sets = {"one": self.catalog.copy()}
        self.app.excluded_camera_ids = set()
        self.app.errors = {}
        self.app.loading = set()
        self.app.device_addresses = {}
        self.app.unconfigured_devices.return_value = []
        self.app.store.load.return_value = self.app.nvrs
        self.app.preferences.load.return_value = set()
        self.app.snapshots.cached_cameras.return_value = self.catalog
        self.app.set_camera_selected.side_effect = lambda key, selected: Application.set_camera_selected(
            self.app, key, selected)

    def startup(self):
        self.app.camera_sets = {}
        with (patch("lookout.app.Gtk.Application.do_startup"), patch("lookout.app.GLib"),
              patch("lookout.app.Theme"), patch("lookout.app.Tray")):
            Application.do_startup(self.app)

    def test_default_shows_all_cameras_from_saved_recorders_only(self):
        self.app.camera_sets["removed"] = [Camera("old", "Old", "", "removed", "Old NVR")]
        self.app.camera_sets["one"].append(Camera("wrong", "Wrong", "", "removed", "Old NVR"))
        self.assertEqual(Application.cameras.fget(self.app), self.catalog)

    def test_remove_and_add_apply_only_after_save_without_changing_catalog_or_nvr(self):
        app = self.app
        for selected, expected in ((False, {"front"}), (True, set())):
            with self.subTest(selected=selected):
                before = app.excluded_camera_ids.copy()
                app.set_camera_selected("front", selected)
                self.assertTrue(app.preferences_busy)
                self.assertEqual(app.excluded_camera_ids, before)
                work, finished = app.run_async.call_args.args
                work()
                app.preferences.save.assert_called_with(expected)
                self.assertEqual(app.excluded_camera_ids, before)
                finished(None, None)
                self.assertEqual(app.excluded_camera_ids, expected)
                self.assertFalse(app.preferences_busy)
                self.assertEqual(Application.cameras.fget(app), self.catalog if selected else self.catalog[1:])
                self.assertEqual(app.camera_sets["one"], self.catalog)
        app.store.save.assert_not_called()
        app.vault.set.assert_not_called()
        app.vault.delete.assert_not_called()
        app.snapshots.remove_nvr.assert_not_called()
        self.assertEqual(app.update_views.call_count, 4)

    def test_selection_saves_are_serialized_and_noop_requests_do_not_write(self):
        app = self.app
        app.set_camera_selected("front", True)
        app.run_async.assert_not_called()
        app.set_camera_selected("front", False)
        app.set_camera_selected("back", False)
        app.set_camera_selected("front", True)
        app.run_async.assert_called_once()
        work, finished = app.run_async.call_args.args
        finished(work(), None)
        self.assertEqual(app.excluded_camera_ids, {"front"})
        app.set_camera_selected("front", False)
        app.run_async.assert_called_once()

    def test_failed_atomic_remove_or_add_keeps_previous_selection_and_file(self):
        app = self.app
        with tempfile.TemporaryDirectory() as directory:
            app.preferences = PreferencesStore(Path(directory) / "preferences.json")
            for selected, before in ((False, {"offline"}), (True, {"offline", "front"})):
                with self.subTest(selected=selected):
                    app.excluded_camera_ids = before.copy()
                    app.preferences.save(before)
                    original = app.preferences.path.read_bytes()
                    app.set_camera_selected("front", selected)
                    work, finished = app.run_async.call_args.args
                    with patch("lookout.preferences.os.replace", side_effect=OSError("Disk full")):
                        with self.assertRaises(StorageError) as caught:
                            work()
                    finished(None, str(caught.exception))
                    self.assertEqual(app.excluded_camera_ids, before)
                    self.assertEqual(app.preferences.path.read_bytes(), original)
                    self.assertFalse(app.preferences_busy)
                    self.assertIn("selection", app.errors)
                    self.assertEqual(list(Path(directory).iterdir()), [app.preferences.path])

    def test_unknown_stale_and_mismatched_camera_ids_cannot_change_preferences(self):
        app = self.app
        app.excluded_camera_ids = {"offline"}
        app.camera_sets["removed"] = [Camera("stale", "Stale", "", "removed", "Removed")]
        app.camera_sets["one"].append(Camera("wrong", "Wrong", "", "removed", "Removed"))
        for key in ("unknown", "stale", "wrong", "offline"):
            for selected in (True, False):
                app.set_camera_selected(key, selected)
                self.assertEqual(app.excluded_camera_ids, {"offline"})
                self.assertIn("no longer available", app.errors["selection"])
        app.run_async.assert_not_called()
        app.preferences.save.assert_not_called()

    def test_catalog_replacement_during_save_does_not_lose_exclusions(self):
        app = self.app
        app.excluded_camera_ids = {"offline"}
        app.set_camera_selected("front", False)
        work, finished = app.run_async.call_args.args
        app.camera_sets["one"] = self.catalog[1:]
        app.settings = None
        finished(work(), None)
        self.assertEqual(app.excluded_camera_ids, {"offline", "front"})
        app.preferences.save.assert_called_once_with({"offline", "front"})
        self.assertEqual(app.camera_sets["one"], self.catalog[1:])
        app.camera_sets["one"] = self.catalog.copy()
        self.assertEqual(Application.cameras.fget(app), self.catalog[1:])

    @patch("lookout.app.ONVIFClient")
    @patch("lookout.app.GLib.idle_add", side_effect=lambda callback: callback())
    def test_progressive_refresh_and_rescan_do_not_restore_hidden_cameras(self, _idle, factory):
        app = self.app
        app.excluded_camera_ids = {"front", "offline"}
        app.camera_sets = {}
        client = factory.return_value

        def discover_cameras(_id, _name, on_camera):
            for camera in self.catalog:
                on_camera(camera)
                self.assertNotIn(self.catalog[0], Application.cameras.fget(app))
            return self.catalog

        client.cameras.side_effect = discover_cameras
        Application.refresh_cameras(app)
        work, finished = app.run_async.call_args.args
        finished(work(), None)
        Application.scan(app)
        app.run_async.call_args.args[1]([DiscoveredDevice("Home", "http://192.0.2.1")], None)
        self.assertEqual(Application.cameras.fget(app), self.catalog[1:])
        self.assertEqual(app.camera_sets["one"], self.catalog)
        self.assertEqual(app.excluded_camera_ids, {"front", "offline"})
        app.preferences.save.assert_not_called()

    def test_restart_load_filters_cached_catalog_and_keeps_hidden_cameras_for_readding(self):
        app = self.app
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            app.preferences = PreferencesStore(path)
            app.set_camera_selected("front", False)
            work, finished = app.run_async.call_args.args
            finished(work(), None)
            app.excluded_camera_ids = set()
            app.preferences = PreferencesStore(path)
            self.startup()
            self.assertEqual(app.excluded_camera_ids, {"front"})
            self.assertEqual(Application.cameras.fget(app), self.catalog[1:])
            self.assertEqual(app.camera_sets["one"], self.catalog)
            app.set_camera_selected("front", True)
            work, finished = app.run_async.call_args.args
            finished(work(), None)
            self.assertEqual(PreferencesStore(path).load(), set())

    def test_invalid_preferences_fail_closed_and_cannot_be_overwritten(self):
        app = self.app
        app.preferences.load.side_effect = StorageError("Invalid camera preferences.")
        self.startup()
        self.assertEqual(Application.cameras.fget(app), [])
        self.assertIn("restart Camera Panel", app.errors["preferences"])
        self.assertEqual(app.camera_sets["one"], self.catalog)
        app.set_camera_selected("front", False)
        app.set_camera_selected("back", True)
        app.preferences.save.assert_not_called()
        app.run_async.assert_not_called()

    def test_demo_selection_and_startup_never_access_persistent_stores(self):
        app = self.app
        app.demo = True
        self.startup()
        camera = app.camera_sets["demo"][0]
        app.set_camera_selected(camera.id, False)
        self.assertNotIn(camera, Application.cameras.fget(app))
        app.set_camera_selected(camera.id, True)
        self.assertIn(camera, Application.cameras.fget(app))
        app.preferences.load.assert_not_called()
        app.preferences.save.assert_not_called()
        app.store.load.assert_not_called()
        app.store.save.assert_not_called()
        app.snapshots.cached_cameras.assert_not_called()
        app.run_async.assert_not_called()

    def test_settings_list_hidden_and_visible_cameras_and_disable_selection_when_blocked(self):
        app = self.app
        app.excluded_camera_ids = {"front"}
        settings = MagicMock(app=app)
        for busy, errors in ((False, {}), (True, {}), (False, {"preferences": "Invalid preferences"})):
            with self.subTest(busy=busy, errors=errors):
                app.preferences_busy, app.errors = busy, errors
                actions = []

                def make_button(text, callback, *args):
                    widget = MagicMock()
                    actions.append((text, callback, widget))
                    return widget

                with (patch("lookout.app.Gtk"), patch("lookout.app.clear"), patch("lookout.app.box"),
                      patch("lookout.app.label") as text, patch("lookout.app.button", side_effect=make_button)):
                    Settings.render(settings)
                for camera in self.catalog:
                    text.assert_any_call(camera.name, "camera-name")
                choices = [(text, callback, widget) for text, callback, widget in actions if text.endswith(" camera")]
                self.assertEqual([text for text, _, _ in choices], ["Add camera", "Remove camera"])
                for _, _, widget in choices:
                    widget.set_sensitive.assert_called_once_with(not busy and not errors)
                if errors:
                    settings.selection_info.set_label.assert_called_with(errors["preferences"])
                elif busy:
                    settings.selection_info.set_label.assert_called_with("Saving camera selection...")
        # Invoke the captured callbacks while blocked, checking their camera IDs without doing I/O.
        app.set_camera_selected.reset_mock()
        for _, callback, _ in choices:
            callback()
        self.assertEqual(app.set_camera_selected.call_args_list, [call("front", True), call("back", False)])

    def test_all_hidden_and_invalid_preferences_have_explicit_empty_panels(self):
        app = self.app
        app.excluded_camera_ids = {camera.id for camera in self.catalog}
        app.unconfigured_devices.return_value = [DiscoveredDevice("New NVR", "http://192.0.2.2")]
        for errors, title in (({}, "No cameras selected"), ({"preferences": "Invalid preferences"}, "Camera selection unavailable")):
            with self.subTest(title=title):
                app.errors = errors
                app.cameras = Application.cameras.fget(app)
                panel = MagicMock(app=app, tiles={camera.id: MagicMock() for camera in self.catalog})
                old_tiles = list(panel.tiles.values())
                with (patch("lookout.app.clear"), patch("lookout.app.box"),
                      patch("lookout.app.label") as text, patch("lookout.app.button") as action):
                    Panel.render(panel)
                text.assert_any_call(title, "heading", wrap=True)
                action.assert_called_once_with("Manage cameras", app.show_settings, "primary")
                self.assertFalse(panel.tiles)
                for tile in old_tiles:
                    tile.stop.assert_called_once()
                app.device_row.assert_not_called()
                if errors:
                    text.assert_any_call(errors["preferences"], "error", wrap=True)

    def test_single_row_render_stops_only_removed_tile_and_reuses_other_tiles(self):
        app = self.app
        app.excluded_camera_ids = {"front"}
        app.cameras = Application.cameras.fget(app)
        removed, retained = (MagicMock(camera=camera) for camera in self.catalog)
        panel = MagicMock(app=app, width=1200, tiles={"front": removed, "back": retained})
        with (patch("lookout.app.Gtk") as gtk, patch("lookout.app.clear"),
              patch("lookout.app.CameraTile") as factory, patch("lookout.app.feed_layout", return_value=(1, 1182, 144)) as layout):
            Panel.render(panel)
            removed.stop.assert_called_once()
            retained.stop.assert_not_called()
            retained.update_camera.assert_not_called()
            factory.assert_not_called()
            layout.assert_called_once_with(1200, 1)
            gtk.Grid.return_value.attach.assert_called_once_with(retained, 0, 0, 1, 1)
            app.excluded_camera_ids.clear()
            app.cameras = Application.cameras.fget(app)
            gtk.Grid.return_value.attach.reset_mock()
            Panel.render(panel)
            factory.assert_called_once_with(self.catalog[0], app)
            self.assertEqual(gtk.Grid.return_value.attach.call_args_list,
                             [call(factory.return_value, 0, 0, 1, 1), call(retained, 1, 0, 1, 1)])
            gtk.ScrolledWindow.assert_not_called()
            retained.stop.assert_not_called()

    def test_stopping_removed_camera_saves_last_snapshot_without_deleting_it(self):
        tile = MagicMock(wait_timer=0)
        last_frame = tile.last_frame
        CameraTile.stop(tile)
        self.assertTrue(tile.closed)
        tile.stream.stop.assert_called_once()
        tile.queue_snapshot.assert_called_once_with(force=True)
        self.assertIs(tile.last_frame, last_frame)
        tile.app.snapshots.remove_nvr.assert_not_called()

    def test_no_layout_customization_remains_in_app(self):
        source = inspect.getsource(inspect.getmodule(Application))
        for obsolete in ("feeds_per_row", "save_layout", "SpinButton", "Apply layout", "max_height"):
            self.assertNotIn(obsolete, source)
        self.assertIn('title="Camera Panel | Settings"', source)
        self.assertIn('Layer.set_namespace(self, "lookout")', source)
        self.assertIn('"io.github.lookout.Cameras"', source)


class AppTests(unittest.TestCase):

    def test_timeout_retains_snapshot_and_saves_last_frame(self):
        tile = MagicMock(closed=False)
        CameraTile.show_status(tile, "Timed out")
        self.assertEqual(tile.state, "Timed out")
        tile.queue_snapshot.assert_called_once_with(force=True)
        tile.picture.set_paintable.assert_not_called()

    def test_live_status_alone_cannot_mark_a_snapshot_live(self):
        tile = MagicMock(closed=False, state="Connecting")
        CameraTile.show_status(tile, "Live")
        self.assertEqual(tile.state, "Connecting")
        tile.update_badges.assert_not_called()

    @patch("lookout.app.time.monotonic", return_value=100)
    def test_resolved_stream_gets_full_budget_after_metadata_loading(self, _clock):
        tile = MagicMock(stream=None, state="Loading", deadline=101)
        tile.camera = Camera("cam", "Front", "", "nvr", "Home")
        camera = Camera("cam", "Front", "rtsp://recorder/sub", "nvr", "Home")
        CameraTile.update_camera(tile, camera)
        self.assertEqual(tile.deadline, 110)
        self.assertEqual(tile.state, "Connecting")
        tile.start_stream.assert_called_once()

    def test_every_open_scans_even_with_existing_cameras(self):
        for cameras in ([], [Camera("1", "Camera", "rtsp://nvr/live", "nvr", "NVR")]):
            app = MagicMock()
            app.cameras = cameras
            app.panel = MagicMock()
            Application.show_panel(app)
            Application.show_panel(app)
            self.assertEqual(app.scan.call_count, 2)
            self.assertEqual(app.refresh_cameras.call_count, 2)
            app.panel.set_visible.assert_called_with(True)

    def test_scan_is_async_coalesced_and_never_saves_devices(self):
        app = MagicMock()
        app.scanning = False
        app.demo = False
        device = DiscoveredDevice("Recorder", "http://192.0.2.2/onvif/device_service")
        app.unconfigured_devices.return_value = [device]
        Application.scan(app)
        self.assertTrue(app.scanning)
        Application.scan(app)
        app.run_async.assert_called_once()
        complete = app.run_async.call_args.args[1]
        complete([device], None)
        self.assertFalse(app.scanning)
        self.assertEqual(app.devices, [device])
        self.assertEqual(app.scan_message, "1 devices found / 1 new")
        app.store.save.assert_not_called()
        app.vault.set.assert_not_called()
        app.review_firewall.assert_not_called()

    def test_empty_scan_checks_firewall_without_changing_it(self):
        app = MagicMock(scanning=False, demo=False)
        app.unconfigured_devices.return_value = []
        Application.scan(app)
        app.run_async.call_args.args[1]([], None)
        app.review_firewall.assert_called_once()
        self.assertGreater(app.review_firewall.call_args.kwargs["since"], 0)
        app.firewall.allow.assert_not_called()

    def test_firewall_change_requires_explicit_dialog_consent(self):
        rule = DiscoveryPermission("wlp2s0", "192.168.1.0/24", True)
        for choice in (0, 1):
            with self.subTest(choice=choice), patch("lookout.app.Gtk.AlertDialog") as factory:
                app = MagicMock()
                dialog = factory.return_value
                dialog.choose_finish.return_value = choice
                Application.prompt_firewall(app, rule)
                app.firewall.allow.assert_not_called()
                app.firewall.remember.assert_not_called()
                dialog.choose.call_args.args[2](dialog, None)
                work, finished = app.run_async.call_args.args
                work()
                app.firewall.remember.assert_called_once_with(rule)
                if choice:
                    app.firewall.allow.assert_called_once_with(rule)
                else:
                    app.firewall.allow.assert_not_called()
                finished(None, None)
                self.assertFalse(app.firewall_busy)
                self.assertEqual(app.scan.call_count, choice)

    def test_firewall_authorization_failure_does_not_remember_approval(self):
        rule = DiscoveryPermission("wlp2s0", "192.168.1.0/24")
        app = MagicMock()
        app.firewall.allow.side_effect = FirewallError("Authorization cancelled")
        with patch("lookout.app.Gtk.AlertDialog") as factory:
            dialog = factory.return_value
            dialog.choose_finish.return_value = 1
            Application.prompt_firewall(app, rule)
            dialog.choose.call_args.args[2](dialog, None)
            work, finished = app.run_async.call_args.args
            with self.assertRaises(FirewallError):
                work()
            app.firewall.remember.assert_not_called()
            finished(None, "Authorization cancelled")
        self.assertFalse(app.firewall_busy)
        app.scan.assert_not_called()

    def test_firewall_review_can_retry_a_remembered_network(self):
        app = MagicMock(demo=False, firewall_busy=False)
        rule = DiscoveryPermission("wlp2s0", "192.168.1.0/24")
        app.firewall_prompted = {rule.key}
        app.firewall.check.return_value = [rule]
        Application.review_firewall(app, since=100, force=True)
        work, finished = app.run_async.call_args.args
        finished(work(), None)
        app.firewall.check.assert_called_once_with(100, force=True)
        self.assertEqual(app.pending_firewall, rule)
        app.offer_firewall_permission.assert_called_once()

    def test_hidden_panel_defers_firewall_dialog(self):
        app = MagicMock(firewall_busy=False, editor=None, saving=False, settings=None)
        app.pending_firewall = DiscoveryPermission("wlp2s0", "192.168.1.0/24")
        app.panel.get_visible.return_value = False
        Application.offer_firewall_permission(app)
        app.prompt_firewall.assert_not_called()
        self.assertIsNotNone(app.pending_firewall)

    def test_configured_devices_are_not_prompted_again(self):
        known = DiscoveredDevice("Known", "http://192.0.2.1/onvif/device_service")
        new = DiscoveredDevice("New", "http://192.0.2.2/onvif/device_service")
        app = SimpleNamespace(nvrs=[NVR("one", "Known", "192.0.2.1")], devices=[known, new],
                              loading=set(), device_addresses={})
        self.assertEqual(Application.unconfigured_devices(app), [new])

    def test_advertised_device_address_matches_manual_web_address(self):
        known = DiscoveredDevice("IPC-BO", "http://192.0.2.1:8000/onvif/device_service", "RLN8-410")
        new = DiscoveredDevice("IPC-BO", "http://192.0.2.2:8000/onvif/device_service", "RLN8-410")
        separate = DiscoveredDevice("Other", "http://192.0.2.1:9000/onvif/device_service")
        app = SimpleNamespace(nvrs=[NVR("one", "Known", "192.0.2.1")], devices=[known, new, separate],
                              loading=set(), device_addresses={"one": {known.address}})
        self.assertEqual(Application.unconfigured_devices(app), [new, separate])

    def test_scan_waits_for_identity_and_recounts_after_lookup(self):
        known = DiscoveredDevice("Known", "http://192.0.2.1:8000/onvif/device_service")
        new = DiscoveredDevice("New", "http://192.0.2.2:8000/onvif/device_service")
        app = MagicMock(scanning=False, panel=None, settings=None)
        app.nvrs = [NVR("one", "Known", "192.0.2.1")]
        app.devices = [known, new]
        app.loading = {"one"}
        app.device_addresses = {}
        app.unconfigured_devices.side_effect = lambda: Application.unconfigured_devices(app)
        self.assertEqual(app.unconfigured_devices(), [])
        Application.update_views(app)
        self.assertEqual(app.scan_message, "2 devices found / checking configured NVRs...")
        app.device_addresses["one"] = {known.address}
        app.loading.clear()
        Application.update_views(app)
        self.assertEqual(app.scan_message, "2 devices found / 1 new")
        self.assertEqual(app.unconfigured_devices(), [new])

    @patch("lookout.app.ONVIFClient")
    def test_camera_lookup_keeps_advertised_identity(self, factory):
        app = MagicMock(demo=False, generation=0)
        app.nvrs = [NVR("one", "Known", "192.0.2.1")]
        app.loading = set()
        app.device_addresses = {}
        app.camera_sets = {}
        client = factory.return_value
        client.cameras.return_value = [Camera("cam", "Front", "rtsp://192.0.2.1/live", "one", "Known")]
        client.device_addresses = {"http://192.0.2.1/onvif/device_service", "http://192.0.2.1:8000/onvif/device_service"}
        Application.refresh_cameras(app)
        work, finished = app.run_async.call_args.args
        finished(work(), None)
        self.assertEqual(app.device_addresses["one"], client.device_addresses)
        self.assertEqual(app.camera_sets["one"], client.cameras.return_value)
        self.assertFalse(app.loading)

    def test_hide_always_stops_streams(self):
        app = MagicMock()
        Application.hide_panel(app)
        app.panel.set_visible.assert_called_once_with(False)
        app.panel.stop_streams.assert_called_once()

    def test_demo_never_discovers(self):
        app = MagicMock(scanning=False, demo=True)
        Application.scan(app)
        app.run_async.assert_not_called()

    def test_stopped_tile_discards_queued_frames_and_status(self):
        tile = MagicMock(closed=True)
        CameraTile.show_status(tile, "Live")
        tile.status.set_label.assert_not_called()
        tile.picture.set_paintable.assert_not_called()

    def test_remove_restores_password_if_config_save_fails(self):
        app = MagicMock(saving=False, demo=False)
        nvr = NVR("one", "NVR", "192.0.2.1")
        app.nvrs = [nvr]
        app.vault.get.return_value = "secret"
        app.store.save.side_effect = StorageError("disk full")
        with patch("lookout.app.Gtk.AlertDialog") as factory:
            dialog = factory.return_value
            dialog.choose_finish.return_value = 1
            Application.remove_nvr(app, nvr)
            dialog.choose.call_args.args[2](dialog, None)
            work = app.run_async.call_args.args[0]
            with self.assertRaises(StorageError):
                work()
        app.vault.set.assert_called_once_with("one", "secret")

    def test_remove_reloads_retained_recorders(self):
        app = MagicMock(saving=False, demo=False, generation=0)
        nvr = NVR("one", "NVR", "192.0.2.1")
        retained = NVR("two", "NVR 2", "192.0.2.2")
        app.nvrs = [nvr, retained]
        with patch("lookout.app.Gtk.AlertDialog") as factory:
            dialog = factory.return_value
            dialog.choose_finish.return_value = 1
            Application.remove_nvr(app, nvr)
            dialog.choose.call_args.args[2](dialog, None)
            complete = app.run_async.call_args.args[1]
            complete(None, None)
        self.assertEqual(app.nvrs, [retained])
        app.refresh_cameras.assert_called_once()

    def test_duplicate_address_is_not_saved(self):
        advertised = "http://192.0.2.1:8000/onvif/device_service"
        for address in ("http://192.0.2.1:80", advertised):
            with self.subTest(address=address):
                editor = MagicMock(nvr=None)
                editor.app.saving = False
                editor.app.nvrs = [NVR("one", "Existing", "192.0.2.1")]
                editor.app.device_addresses = {"one": {advertised}}
                editor.fields = {name: MagicMock() for name in ("Name", "ONVIF address", "Username")}
                for key, value in (("Name", "New"), ("ONVIF address", address), ("Username", "admin")):
                    editor.fields[key].get_text.return_value = value
                Editor.save(editor)
                editor.app.run_async.assert_not_called()
                self.assertIn("already added", editor.message.set_label.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
