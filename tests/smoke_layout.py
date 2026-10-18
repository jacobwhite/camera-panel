"""Optional Wayland selection/snapshot test, with local patterns and temporary files only."""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from lookout.app import Application, GLib, Gtk, Settings
from lookout.playback import Stream
from lookout.preferences import PreferencesStore
from lookout.snapshots import SnapshotStore


class PatternStream(Stream):
    def __init__(self, *args, **kwargs):
        # Exercise real decoders and persistence without ever opening a network stream.
        kwargs["demo"] = True
        super().__init__(*args, **kwargs)


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from widgets(child)
        child = child.get_next_sibling()


def selection_buttons(text=None):
    return [widget for widget in widgets(app.settings)
            if isinstance(widget, Gtk.Button)
            and widget.get_label() in ("Add camera", "Remove camera")
            and (text is None or widget.get_label() == text)]


app = Application(demo=True)
app.set_application_id("io.github.lookout.LayoutSmoke")
temporary = tempfile.TemporaryDirectory(prefix="camera-panel-layout-")
app.snapshots = SnapshotStore(Path(temporary.name) / "snapshots")
app.preferences = PreferencesStore(Path(temporary.name) / "preferences" / "preferences.json")
# Keep discovery and metadata lookup disabled even when testing non-demo persistence.
app.scan = lambda: None
app.refresh_cameras = lambda: None
phase = 0
failures = []
streams = {}
removed = None
deadline = time.monotonic() + 30


def check():
    global phase, streams, removed
    try:
        assert time.monotonic() < deadline, "Selection test timed out"
        if not app.panel:
            return True
        if phase == 0:
            initial = app.camera_sets["demo"]
            app.camera_sets["demo"] += [replace(camera, id=f"extra-{camera.id}") for camera in initial]
            app.panel.render()
            phase = 1
            return True
        tiles = list(app.panel.tiles.values())
        if app.preferences_busy:
            assert all(not widget.get_sensitive() for widget in selection_buttons())
            return True
        assert not app.errors, app.errors
        if phase == 1:
            if len(tiles) != 8 or not all(tile.state == "Live" for tile in tiles):
                return True
            grid = tiles[0].get_parent()
            assert grid.get_child_at(7, 0) is not None and grid.get_child_at(0, 1) is None
            assert sum(tile.get_width() for tile in tiles) + 7 * 4 <= app.panel.get_width()
            assert app.panel.get_height() < 220, app.panel.get_height()
            streams = {tile.camera.id: tile.stream for tile in tiles}
            removed = tiles[0]
            app.demo = False
            app.settings = Settings(app)
            app.settings.present()
            assert app.settings.get_title() == "Camera Panel | Settings"
            assert not any(isinstance(widget, Gtk.SpinButton) for widget in widgets(app.settings))
            assert len(selection_buttons("Remove camera")) == 8
            selection_buttons("Remove camera")[0].emit("clicked")
            assert app.preferences_busy and len(app.panel.tiles) == 8, "Selection applied before save"
            assert all(not widget.get_sensitive() for widget in selection_buttons())
            phase = 2
            return True
        if phase == 2:
            assert len(tiles) == 7
            grid = tiles[0].get_parent()
            assert grid.get_child_at(6, 0) is not None and grid.get_child_at(7, 0) is None
            assert grid.get_child_at(0, 1) is None
            assert all(tile.stream is streams[tile.camera.id] for tile in tiles), "Removal restarted other decoders"
            assert all(not tile.stream._stopped.is_set() for tile in tiles)
            assert removed.closed and removed.stream._stopped.is_set()
            assert app.preferences.load() == {removed.camera.id}
            assert len(app.camera_sets["demo"]) == 8, "Removal discarded the camera catalog"
            app.snapshot_executor.submit(lambda: None).result(timeout=5)
            assert app.snapshots.load(removed.camera.id) is not None, "Removal discarded the saved preview"
            assert len(selection_buttons("Remove camera")) == 7
            assert len(selection_buttons("Add camera")) == 1
            selection_buttons("Add camera")[0].emit("clicked")
            assert app.preferences_busy and len(app.panel.tiles) == 7
            phase = 3
            return True
        if phase == 3:
            if len(tiles) != 8 or not all(tile.state == "Live" for tile in tiles):
                return True
            grid = tiles[0].get_parent()
            assert grid.get_child_at(7, 0) is not None and grid.get_child_at(0, 1) is None
            assert app.panel.get_height() < 220, app.panel.get_height()
            assert all(tile.stream is streams[tile.camera.id] for tile in tiles if tile.camera.id != removed.camera.id)
            assert app.panel.tiles[removed.camera.id].stream is not removed.stream
            assert app.preferences.load() == set()
            assert len(selection_buttons("Remove camera")) == 8
            app.set_camera_selected(removed.camera.id, False)
            phase = 4
            return True
        if phase == 4:
            assert len(tiles) == 7
            app.hide_panel()
            app.snapshot_executor.submit(lambda: None).result(timeout=5)
            cached = app.snapshots.cached_cameras(app.nvrs)
            assert len(cached) == 8
            assert all(not camera.uri and not camera.password for camera in cached)
            app.camera_sets["demo"] = cached
            app.preferences = PreferencesStore(app.preferences.path)
            app.excluded_camera_ids = app.preferences.load()
            assert app.excluded_camera_ids == {removed.camera.id}
            app.show_panel()
            assert len(app.panel.tiles) == 7 and removed.camera.id not in app.panel.tiles
            phase = 5
            return True
        if phase == 5:
            if not all(tile.picture.get_paintable() for tile in tiles):
                return True
            assert all(tile.stream is None and tile.state == "Loading" for tile in tiles)
            assert all(tile.snapshot_label.get_visible() for tile in tiles)
            tile = tiles[0]
            picture = tile.picture.get_paintable()
            tile.deadline = time.monotonic() - 1
            tile.start_stream()
            assert tile.state == "Timed out" and tile.retry_button.get_visible()
            assert tile.picture.get_paintable() == picture, "Timeout discarded saved preview"
            with patch("lookout.app.Stream") as factory:
                tile.camera.uri = "rtsp://never-contact.invalid/live"
                tile.retry()
                factory.return_value.start.assert_called_once()
                assert 9 < factory.call_args.kwargs["connect_timeout"] <= 10
            assert tile.picture.get_paintable() == picture and tile.snapshot_label.get_visible()
            print("Layout smoke passed: fixed row 8 -> 7 -> 8, Settings add/remove, unchanged other decoders, "
                  "persisted selection and previews, cached reopen, timeout and explicit retry.")
            app.quit_cleanly()
            return False
    except Exception as error:
        failures.append(error)
        app.quit_cleanly()
        return False


GLib.timeout_add(150, check)
try:
    with (patch("lookout.app.Stream", PatternStream),
          patch.dict(os.environ, {"XDG_CACHE_HOME": temporary.name, "XDG_CONFIG_HOME": temporary.name})):
        app.run(["lookout-layout-smoke"])
finally:
    temporary.cleanup()
if failures:
    raise failures[0]
