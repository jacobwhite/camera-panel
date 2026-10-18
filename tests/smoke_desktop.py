"""Optional live Wayland smoke test. Uses test patterns and a fake discovery result.

Run: LD_PRELOAD=libgtk4-layer-shell.so python -m tests.smoke_desktop
It briefly opens native windows, creates no config, and accesses no NVR or keyring.
"""

import os
import subprocess
import tempfile
import time
from unittest.mock import Mock

from lookout.app import Application, Editor, GLib, Gtk
from lookout.onvif import DiscoveredDevice
from lookout.firewall import DiscoveryPermission


def all_labels(widget):
    result = [widget.get_label()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        result.extend(all_labels(child))
        child = child.get_next_sibling()
    return result


app = Application(demo=True)
failures = []
deadline = time.monotonic() + 12


def permission_finished():
    try:
        if app.firewall_busy:
            assert time.monotonic() < deadline, "Firewall decision did not finish"
            return True
        app.firewall.allow.assert_not_called()
        app.firewall.remember.assert_called_once()
        print("Desktop smoke passed: 4 live decoders, pause-on-hide, discovery prompt, NVR editor, firewall consent/decline.")
    except Exception as error:
        failures.append(error)
    app.quit_cleanly()
    return False


def decline_permission():
    def visit(widget):
        if isinstance(widget, Gtk.Button) and widget.get_label() == "Not now":
            return widget
        child = widget.get_first_child()
        while child:
            if found := visit(child):
                return found
            child = child.get_next_sibling()
        return None
    try:
        windows = Gtk.Window.get_toplevels()
        for index in range(windows.get_n_items()):
            if decline := visit(windows.get_item(index)):
                app.firewall.allow.assert_not_called()
                decline.emit("clicked")
                GLib.timeout_add(50, permission_finished)
                return False
        assert time.monotonic() < deadline, "Firewall consent dialog did not render"
        return True
    except Exception as error:
        failures.append(error)
        app.quit_cleanly()
        return False


def finish_editor():
    try:
        assert app.editor.get_mapped(), "NVR editor did not map"
        app.editor.close()
        app.firewall = Mock()
        app.prompt_firewall(DiscoveryPermission("wlp2s0", "192.168.1.0/24", True))
        GLib.timeout_add(100, decline_permission)
    except Exception as error:
        failures.append(error)
        app.quit_cleanly()
    return False


def check():
    try:
        assert app.panel and app.panel.get_visible(), "Panel did not map"
        tiles = list(app.panel.tiles.values())
        if len(tiles) != 4 or not all(tile.picture.get_paintable() for tile in tiles):
            assert time.monotonic() < deadline, "Demo feeds did not render"
            return True
        assert app.panel.get_width() > 1000, "Panel did not span the output"
        assert app.panel.get_height() < 230, "Panel was not a compact strip"
        if os.environ.get("LOOKOUT_SMOKE_CAPTURE"):
            descriptor, capture = tempfile.mkstemp(prefix="camera-panel-demo-", suffix=".png")
            os.close(descriptor)
            subprocess.run(["grim", "-s", "1", "-g", f"0,0 {app.panel.width + 20}x420",
                            capture], check=True, timeout=5)
            print(f"Demo desktop capture: {capture}")
        app.hide_panel()
        assert not app.panel.tiles, "Decoders remained attached after hiding"
        assert all(tile.closed and tile.stream._stopped.is_set() for tile in tiles)
        app.nvrs = []
        app.camera_sets.clear()
        app.devices = [DiscoveredDevice("Test recorder", "http://192.0.2.1/onvif/device_service")]
        app.show_panel()
        text = all_labels(app.panel)
        assert "NVR found on your network." in text, text
        assert "Add NVR" in text, text
        assert not app.nvrs, "Discovery silently added an NVR"
        app.show_settings()
        assert not app.panel.get_visible()
        editor = Editor(app, device=app.devices[0])
        app.editor = editor
        editor.present()
        assert editor.fields["ONVIF address"].get_text() == app.devices[0].address
        assert editor.password.get_text() == ""
        # Let Wayland configure the editor before testing a user's close action.
        GLib.timeout_add(300, finish_editor)
        return False
    except Exception as error:
        failures.append(error)
    app.quit_cleanly()
    return False


GLib.timeout_add(300, check)
app.run(["lookout-smoke"])
if failures:
    raise failures[0]
