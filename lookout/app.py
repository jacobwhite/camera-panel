"""Native tray, layer-shell strip, and recorder setup. All network work is off-thread."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell as Layer, Pango

from .onvif import Camera, ONVIFClient, OnvifError, discover, normalize_address
from .firewall import FirewallSetup
from .playback import Stream
from .preferences import PreferencesStore, feed_layout
from .snapshots import SnapshotStore
from .storage import ConfigStore, NVR, PasswordVault, StorageError
from .theme import Theme
from .tray import Tray


def label(text, style=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if style:
        widget.add_css_class(style)
    if not wrap:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    return widget


def button(text, callback, style=None):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda _: callback())
    if style:
        widget.add_css_class(style)
    return widget


def box(vertical=False, spacing=8, margin=0):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                     spacing=spacing)
    for edge in ("top", "bottom", "start", "end"):
        getattr(widget, "set_margin_" + edge)(margin)
    return widget


def clear(widget):
    while child := widget.get_first_child():
        widget.remove(child)


class PreviewPicture(Gtk.Picture):
    def do_measure(self, orientation, for_size):
        # Decoded texture dimensions must not force a 640x360 minimum tile.
        size = max(8, self.get_size_request()[1]) if orientation == Gtk.Orientation.VERTICAL else 0
        return size, size, -1, -1


class CameraTile(Gtk.Box):
    def __init__(self, camera, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
        self.app = app
        self.add_css_class("camera")
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.camera = camera
        self.closed = False
        self.frame = None
        self.lock = threading.Lock()
        self.pending = False
        self.generation = 0
        self.stream = None
        self.wait_timer = 0
        self.deadline = time.monotonic() + 10
        self.state = "Loading" if not camera.uri and not app.demo else "Connecting"
        self.last_frame = None
        self.snapshot_timestamp = None
        self.last_saved_at = 0
        self.snapshot_error = ""
        self.picture = PreviewPicture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        overlay = Gtk.Overlay(child=self.picture)
        self.append(overlay)
        self.status = label(self.state.upper(), "camera-status")
        self.status.set_halign(Gtk.Align.END)
        self.status.set_valign(Gtk.Align.START)
        self.status.set_margin_top(3)
        self.status.set_margin_end(3)
        self.status.set_can_target(False)
        overlay.add_overlay(self.status)
        self.snapshot_label = label("SNAPSHOT", "snapshot-status")
        self.snapshot_label.set_halign(Gtk.Align.START)
        self.snapshot_label.set_valign(Gtk.Align.END)
        self.snapshot_label.set_margin_start(3)
        self.snapshot_label.set_margin_bottom(3)
        self.snapshot_label.set_can_target(False)
        self.snapshot_label.set_visible(False)
        overlay.add_overlay(self.snapshot_label)
        self.retry_button = button("Retry", self.retry)
        self.retry_button.set_halign(Gtk.Align.CENTER)
        self.retry_button.set_valign(Gtk.Align.CENTER)
        self.retry_button.set_visible(False)
        overlay.add_overlay(self.retry_button)
        self.name = label(camera.name, "camera-name")
        self.name.set_margin_start(5)
        self.name.set_margin_end(5)
        self.name.set_margin_top(3)
        self.name.set_margin_bottom(3)
        self.append(self.name)
        if not app.demo:
            app.run_snapshot(lambda: app.snapshots.load(camera.id), self.loaded_snapshot)
        self.start_stream()

    def loaded_snapshot(self, snapshot, error):
        if self.closed or snapshot is None or self.last_frame is not None:
            return
        try:
            self.picture.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(snapshot.png)))
        except GLib.Error:
            return
        self.snapshot_timestamp = snapshot.captured_at
        self.update_badges()

    def update_badges(self):
        live = self.state == "Live"
        self.status.set_label(self.state.upper())
        self.status.remove_css_class("success")
        if live:
            self.status.add_css_class("success")
            self.picture.remove_css_class("stale")
        else:
            self.picture.add_css_class("stale")
        self.retry_button.set_visible(self.state in ("Timed out", "Stream unavailable"))
        self.snapshot_label.set_visible(not live and self.snapshot_timestamp is not None)
        saved = ""
        if self.snapshot_timestamp is not None:
            saved = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.snapshot_timestamp))
            self.snapshot_label.set_label("SNAPSHOT " + time.strftime("%H:%M:%S", time.localtime(self.snapshot_timestamp)))
        self.set_tooltip_text(f"{self.camera.name}\n{self.camera.nvr_name}\n{self.state}"
                              + (f"\nLast received: {saved}" if saved else "")
                              + (f"\n{self.snapshot_error}" if self.snapshot_error else ""))

    def queue_snapshot(self, force=False):
        if self.app.demo or self.last_frame is None:
            return
        now = time.monotonic()
        if not force and now - self.last_saved_at < 5:
            return
        self.last_saved_at = now
        frame, captured_at = self.last_frame
        camera = self.camera

        def saved(_result, error):
            if not self.closed:
                self.snapshot_error = "Could not save this preview." if error else ""
                self.update_badges()
        self.app.run_snapshot(lambda: self.app.snapshots.save(camera, frame, captured_at), saved)

    def start_stream(self):
        if self.wait_timer:
            GLib.source_remove(self.wait_timer)
            self.wait_timer = 0
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.show_status("Timed out")
            return
        if not self.camera.uri and not self.app.demo:
            self.state = "Loading"
            self.wait_timer = GLib.timeout_add(max(1, int(remaining * 1000)), self.wait_expired)
            self.update_badges()
            return
        self.generation += 1
        generation = self.generation
        self.stream = Stream(self.camera,
                             lambda frame: self.receive_frame(frame, generation),
                             lambda status: self.receive_status(status, generation),
                             demo=self.app.demo, connect_timeout=remaining)
        self.stream.start()

    def wait_expired(self):
        self.wait_timer = 0
        self.show_status("Timed out")
        return False

    def update_camera(self, camera):
        previous = self.camera
        self.camera = camera
        self.name.set_label(camera.name)
        changed = (previous.uri, previous.username, previous.password) != (camera.uri, camera.username, camera.password)
        if changed and self.state not in ("Timed out", "Stream unavailable"):
            if self.stream is None:
                # Metadata loading is not an RTSP attempt. Give the newly
                # resolved stream its full connection budget.
                self.deadline = time.monotonic() + 10
                self.state = "Connecting"
                self.start_stream()
            else:
                self.retry()
        self.update_badges()

    def retry(self):
        if self.closed:
            return
        if self.stream:
            self.stream.stop()
        self.generation += 1
        self.deadline = time.monotonic() + 10
        self.show_status("Connecting")
        self.start_stream()
        if not self.camera.uri:
            self.app.refresh_cameras()

    def receive_frame(self, frame, generation):
        with self.lock:
            if self.closed or generation != self.generation:
                return
            self.frame = (frame, time.time(), generation)
            if self.pending:
                return
            self.pending = True
        GLib.idle_add(self.paint_frame)

    def paint_frame(self):
        with self.lock:
            frame, self.frame = self.frame, None
            self.pending = False
        if not self.closed and frame and frame[2] == self.generation:
            self.picture.set_paintable(Gdk.MemoryTexture.new(
                Stream.WIDTH, Stream.HEIGHT, Gdk.MemoryFormat.R8G8B8,
                GLib.Bytes.new(frame[0]), Stream.WIDTH * 3))
            self.last_frame = frame[:2]
            self.snapshot_timestamp = frame[1]
            self.state = "Live"
            self.update_badges()
            self.queue_snapshot()
        return False

    def receive_status(self, status, generation):
        GLib.idle_add(self.show_status, status, generation)

    def show_status(self, status, generation=None):
        if not self.closed and (generation is None or generation == self.generation) and status != "Live":
            self.state = status
            self.update_badges()
            if status in ("Timed out", "Stream unavailable"):
                self.queue_snapshot(force=True)
        return False

    def stop(self):
        with self.lock:
            self.closed = True
            self.frame = None
        if self.wait_timer:
            GLib.source_remove(self.wait_timer)
            self.wait_timer = 0
        if self.stream:
            self.stream.stop()
        self.queue_snapshot(force=True)
        self.picture.set_paintable(None)


class Panel(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Camera Panel", decorated=False)
        self.app = app
        self.tiles = {}
        self.width = 1200
        self.add_css_class("lookout")
        self.add_css_class("panel")
        if not Layer.is_supported():
            raise RuntimeError("Camera Panel requires Wayland layer-shell. Launch using lookout-run on Hyprland.")
        Layer.init_for_window(self)
        Layer.set_namespace(self, "lookout")
        Layer.set_layer(self, Layer.Layer.OVERLAY)
        Layer.set_keyboard_mode(self, Layer.KeyboardMode.ON_DEMAND)
        Layer.set_exclusive_zone(self, -1)
        for edge in (Layer.Edge.TOP, Layer.Edge.LEFT, Layer.Edge.RIGHT):
            Layer.set_anchor(self, edge, True)
        Layer.set_margin(self, Layer.Edge.LEFT, 10)
        Layer.set_margin(self, Layer.Edge.RIGHT, 10)
        root = box(vertical=True, spacing=6, margin=8)
        header = box(spacing=10)
        header.append(label("CAMERA PANEL", "brand"))
        self.summary = label("LOCAL CAMERAS", "eyebrow")
        header.append(self.summary)
        spacer = Gtk.Box(hexpand=True)
        header.append(spacer)
        self.scan_label = label("", "muted")
        header.append(self.scan_label)
        self.scan_button = button("Scan", app.scan)
        header.append(self.scan_button)
        header.append(button("Settings", app.show_settings))
        close = button("Close", app.hide_panel, "flat")
        close.set_tooltip_text("Hide cameras (Escape). Streams stop while hidden.")
        header.append(close)
        root.append(header)
        self.content = box(vertical=True, spacing=4)
        root.append(self.content)
        self.set_child(root)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key_pressed)
        self.add_controller(keys)
        self.connect("close-request", lambda _: app.hide_panel() or True)

    def key_pressed(self, _controller, key, _code, _modifiers):
        if key == Gdk.KEY_Escape:
            self.app.hide_panel()
            return True
        return False

    def position(self):
        display = Gdk.Display.get_default()
        monitors = display.get_monitors()
        monitor = monitors.get_item(0)
        margin = 32
        try:
            result = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True,
                                    text=True, timeout=1, check=True)
            outputs = json.loads(result.stdout)
            focused = next(item for item in outputs if item.get("focused"))
            for i in range(monitors.get_n_items()):
                candidate = monitors.get_item(i)
                if candidate.get_connector() == focused["name"]:
                    monitor = candidate
                    break
            margin = int(focused.get("reserved", [0, 26, 0, 0])[1]) + 6
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration, TypeError):
            pass
        if monitor:
            Layer.set_monitor(self, monitor)
            self.width = monitor.get_geometry().width - 20
        Layer.set_margin(self, Layer.Edge.TOP, margin)

    def stop_streams(self):
        for tile in self.tiles.values():
            tile.stop()
        self.tiles.clear()

    def render(self):
        cameras = self.app.cameras
        self.scan_label.set_label("Scanning LAN..." if self.app.scanning else self.app.scan_message)
        self.scan_button.set_sensitive(not self.app.scanning and not self.app.demo)
        desired = {camera.id: camera for camera in cameras}
        for key, tile in list(self.tiles.items()):
            if key not in desired:
                tile.stop()
                del self.tiles[key]
        # Preserve working streams when discovery updates the header or another NVR responds.
        for tile in self.tiles.values():
            parent = tile.get_parent()
            if parent:
                parent.remove(tile)
        clear(self.content)
        if cameras:
            grid = Gtk.Grid(column_homogeneous=True, column_spacing=4, hexpand=True)
            _, tile_width, height = feed_layout(self.width, len(cameras))
            for index, camera in enumerate(cameras):
                tile = self.tiles.get(camera.id)
                if tile is None:
                    tile = self.tiles[camera.id] = CameraTile(camera, self.app)
                elif tile.camera != camera:
                    tile.update_camera(camera)
                tile.picture.set_size_request(-1, height)
                tile.name.set_visible(tile_width >= 60)
                grid.attach(tile, index, 0, 1, 1)
            self.content.append(grid)
            counts = f"{len(cameras)} CAMERAS  /  {len({c.nvr_id for c in cameras})} NVRs"
            if self.app.demo:
                counts = "DEMO / LOCAL TEST PATTERNS / NO CAMERAS CONNECTED"
            self.summary.set_label(counts)
        else:
            empty = box(vertical=True, spacing=10, margin=16)
            found = self.app.unconfigured_devices()
            selection_blocked = "preferences" in self.app.errors
            all_hidden = any(camera.nvr_id == nvr.id for nvr in self.app.nvrs
                             for camera in self.app.camera_sets.get(nvr.id, []))
            title = "Your cameras, one glance away."
            detail = "Scanning your local network for ONVIF recorders..."
            if selection_blocked:
                title = "Camera selection unavailable"
                detail = "Fix the camera preferences and restart Camera Panel before showing cameras."
            elif all_hidden:
                title = "No cameras selected"
                detail = "Add cameras back in Settings. Your recorders and recordings are unchanged."
            elif found:
                title = "NVR found on your network."
                detail = "Add a recorder below to bring its cameras into view. Your credentials stay in the desktop keyring."
            elif self.app.nvrs:
                title = "Connecting to your recorders..." if self.app.loading else "No camera feeds available."
                detail = "Looking up each NVR's camera streams." if self.app.loading else "Open Settings to check connection details or retry."
            elif not self.app.scanning:
                detail = "No ONVIF devices found yet. Enable ONVIF on the NVR, check the network, or add its address manually."
            empty.append(label(title, "heading", wrap=True))
            empty.append(label(detail, "muted", wrap=True))
            if selection_blocked or all_hidden:
                empty.append(button("Manage cameras", self.app.show_settings, "primary"))
            elif found:
                for device in found[:3]:
                    empty.append(self.app.device_row(device))
                if len(found) > 3:
                    empty.append(button(f"View all {len(found)} discovered devices", self.app.show_settings))
            else:
                actions = box()
                actions.append(button("Add NVR manually", lambda: self.app.show_editor(), "primary"))
                empty.append(actions)
            self.content.append(empty)
            self.summary.set_label("CAMERA SELECTION" if selection_blocked or all_hidden
                                   else "ONVIF DISCOVERY  /  LOCAL NETWORK ONLY  /  NO CLOUD")
        if self.app.errors:
            first = self.app.errors.get("preferences") or next(iter(self.app.errors.values()))
            warning = label(first, "error", wrap=True)
            self.content.append(warning)


class Settings(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Camera Panel | Settings", default_width=620, default_height=640)
        self.app = app
        self.add_css_class("lookout")
        root = box(vertical=True, spacing=14, margin=22)
        root.append(label("CAMERA PANEL / SETTINGS", "eyebrow"))
        root.append(label("All your NVRs. One view.", "heading"))
        root.append(label("Choose cameras from your ONVIF recorders to show in one row. Removing a camera only hides its preview; it does not change the NVR or recordings.", "muted", True))
        self.selection_info = label("", "muted", True)
        root.append(self.selection_info)
        actions = box()
        actions.append(button("Add NVR", lambda: app.show_editor(), "primary"))
        self.scan_button = button("Scan network", app.scan)
        actions.append(self.scan_button)
        actions.append(button("Show cameras", self.show_cameras))
        root.append(actions)
        self.info = label("", "muted", True)
        root.append(self.info)
        permissions = button("Discovery firewall settings", lambda: app.review_firewall(force=True))
        permissions.set_sensitive(not app.demo)
        root.append(permissions)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.records = box(vertical=True, spacing=10)
        scroll.set_child(self.records)
        root.append(scroll)
        root.append(label("Passwords are kept in Secret Service, not in the configuration file.\nDiscovery works on the local subnet; remote NVRs can be added by address.", "muted", True))
        self.set_child(root)
        self.connect("close-request", self.close_window)
        self.render()

    def close_window(self, _window):
        self.app.settings = None
        return False

    def show_cameras(self):
        self.close()
        self.app.show_panel()

    def render(self):
        self.scan_button.set_sensitive(not self.app.scanning and not self.app.demo)
        self.info.set_label("Scanning your local network..." if self.app.scanning else self.app.scan_message)
        selection_error = self.app.errors.get("preferences") or self.app.errors.get("selection")
        self.selection_info.set_label(selection_error or (
            "Saving camera selection..." if self.app.preferences_busy else "Add or remove cameras below."))
        self.selection_info.remove_css_class("error")
        if selection_error:
            self.selection_info.add_css_class("error")
        clear(self.records)
        self.records.append(label("CONFIGURED RECORDERS", "eyebrow"))
        if not self.app.nvrs:
            self.records.append(label("No NVRs added yet.", "muted"))
        for nvr in self.app.nvrs:
            row = box(vertical=True)
            row.add_css_class("device")
            top = box()
            text = label(nvr.name, "camera-name")
            text.set_hexpand(True)
            top.append(text)
            top.append(button("Edit", lambda n=nvr: self.app.show_editor(nvr=n)))
            top.append(button("Remove", lambda n=nvr: self.app.remove_nvr(n), "danger"))
            row.append(top)
            row.append(label(nvr.address, "muted"))
            cameras = [camera for camera in self.app.camera_sets.get(nvr.id, []) if camera.nvr_id == nvr.id]
            count = len(cameras)
            status = self.app.errors.get(nvr.id) or ("Connecting..." if nvr.id in self.app.loading else f"{count} cameras available")
            row.append(label(status, "error" if nvr.id in self.app.errors else "muted", True))
            for camera in cameras:
                camera_row = box()
                name = label(camera.name, "camera-name")
                name.set_hexpand(True)
                camera_row.append(name)
                selected = camera.id not in self.app.excluded_camera_ids
                action = button("Remove camera" if selected else "Add camera",
                                lambda key=camera.id, value=not selected: self.app.set_camera_selected(key, value))
                action.set_sensitive(not self.app.preferences_busy and "preferences" not in self.app.errors)
                camera_row.append(action)
                row.append(camera_row)
            self.records.append(row)
        self.records.append(Gtk.Separator(margin_top=10, margin_bottom=10))
        self.records.append(label("DISCOVERED ON YOUR NETWORK", "eyebrow"))
        devices = self.app.unconfigured_devices()
        if not devices:
            status = "Checking configured NVRs..." if self.app.loading else "No new devices. You can always add an address manually."
            self.records.append(label("Scanning..." if self.app.scanning else status, "muted", True))
        for device in devices:
            self.records.append(self.app.device_row(device))


class Editor(Gtk.ApplicationWindow):
    def __init__(self, app, nvr=None, device=None):
        super().__init__(application=app, title="Camera Panel | " + ("Edit NVR" if nvr else "Add NVR"),
                         default_width=520, resizable=False)
        self.app, self.nvr = app, nvr
        self.add_css_class("lookout")
        self.set_transient_for(app.settings)
        self.set_modal(True)
        root = box(vertical=True, spacing=12, margin=24)
        root.append(label("CONNECT A RECORDER", "eyebrow"))
        root.append(label("Edit NVR" if nvr else "Bring your cameras into view.", "heading", True))
        self.fields = {}
        values = {"Name": nvr.name if nvr else (device.name if device else ""),
                  "ONVIF address": nvr.address if nvr else (device.address if device else ""),
                  "Username": nvr.username if nvr else ""}
        for name, value in values.items():
            group = box(vertical=True, spacing=4)
            group.append(label(name))
            field = Gtk.Entry(text=value, hexpand=True)
            field.set_placeholder_text("192.168.1.100:80 or full ONVIF URL" if name == "ONVIF address" else name)
            field.set_max_length(2048 if name == "ONVIF address" else 256)
            self.fields[name] = field
            group.append(field)
            root.append(group)
        root.append(label("Password" + (" (leave blank to keep saved password)" if nvr else ""), wrap=True))
        self.password = Gtk.PasswordEntry(show_peek_icon=True)
        root.append(self.password)
        self.verify = Gtk.CheckButton(label="Verify ONVIF connection before saving", active=True)
        root.append(self.verify)
        root.append(label("Use an ONVIF-enabled account from the recorder's settings. Some NVRs require a separate ONVIF user and port.", "muted", True))
        self.message = label("", "error", True)
        root.append(self.message)
        actions = box()
        self.cancel_button = button("Cancel", self.close)
        self.save_button = button("Save & connect" if nvr else "Connect & add NVR", self.save, "primary")
        actions.append(self.cancel_button)
        actions.append(self.save_button)
        root.append(actions)
        self.set_child(root)
        self.connect("close-request", self.close_window)

    def close_window(self, _window):
        if self.app.saving:
            return True
        self.app.editor = None
        return False

    def save(self):
        if self.app.saving:
            return
        try:
            address = normalize_address(self.fields["ONVIF address"].get_text())
            name = self.fields["Name"].get_text().strip() or urlsplit(address).hostname
            nvr = NVR(self.nvr.id if self.nvr else str(uuid.uuid4()), name, address,
                      self.fields["Username"].get_text().strip())
            if any(item.id != nvr.id and address in (
                    {normalize_address(item.address)} | self.app.device_addresses.get(item.id, set()))
                   for item in self.app.nvrs):
                raise OnvifError("This NVR is already added. Edit its existing entry instead.")
        except OnvifError as error:
            self.message.set_label(str(error))
            return
        entered_password = self.password.get_text()
        verify = self.verify.get_active()
        records = [nvr if item.id == nvr.id else item for item in self.app.nvrs]
        if not self.nvr:
            records.append(nvr)
        self.app.saving = True
        self.save_button.set_sensitive(False)
        self.cancel_button.set_sensitive(False)
        self.message.remove_css_class("error")
        self.message.set_label("Connecting to the NVR..." if verify else "Saving securely...")

        def save():
            old = self.app.vault.get(nvr.id) if self.nvr else ""
            password = entered_password if entered_password or not self.nvr else old
            client = ONVIFClient(nvr.address, nvr.username, password)
            cameras = client.cameras(nvr.id, nvr.name) if verify else []
            self.app.vault.set(nvr.id, password)
            try:
                self.app.store.save(records)
            except StorageError:
                if self.nvr:
                    self.app.vault.set(nvr.id, old)
                else:
                    self.app.vault.delete(nvr.id)
                raise
            return cameras, client.device_addresses

        def saved(result, error):
            self.app.saving = False
            self.save_button.set_sensitive(True)
            self.cancel_button.set_sensitive(True)
            if error:
                self.message.add_css_class("error")
                self.message.set_label(error)
                return
            cameras, addresses = result
            self.app.nvrs = records
            self.app.generation += 1
            self.app.loading.clear()
            self.app.camera_sets[nvr.id] = cameras
            self.app.device_addresses[nvr.id] = addresses
            self.app.errors.pop(nvr.id, None)
            self.close()
            if self.app.settings:
                self.app.settings.close()
            self.app.show_panel()

        self.app.run_async(save, saved)


class Application(Gtk.Application):
    def __init__(self, demo=False):
        super().__init__(application_id="io.github.lookout.Demo" if demo else "io.github.lookout.Cameras",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.demo = demo
        self.store, self.vault = ConfigStore(), PasswordVault()
        self.preferences = PreferencesStore()
        self.excluded_camera_ids = set()
        self.preferences_busy = False
        self.snapshots = SnapshotStore()
        self.snapshot_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lookout-snapshots")
        self.firewall = FirewallSetup()
        self.firewall_busy = False
        self.pending_firewall = None
        self.firewall_prompted = set()
        self.nvrs = []
        self.camera_sets = {}
        self.device_addresses = {}
        self.errors = {}
        self.loading = set()
        self.generation = 0
        self.devices = []
        self.scanning = False
        self.scan_message = ""
        self.saving = False
        self.stopping = False
        self.panel = self.settings = self.editor = self.tray = None
        self.network_slots = threading.Semaphore(4)

    @property
    def cameras(self):
        if "preferences" in self.errors:
            return []
        return [camera for nvr in self.nvrs for camera in self.camera_sets.get(nvr.id, [])
                if camera.nvr_id == nvr.id and camera.id not in self.excluded_camera_ids]

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        self.theme = Theme()
        GLib.timeout_add_seconds(2, self.theme.refresh)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self.quit_cleanly)
        if self.demo:
            self.nvrs = [NVR("demo", "Demo recorder", "http://demo.invalid")]
            self.camera_sets["demo"] = [Camera(str(i), name, "demo", "demo", "DEMO / TEST PATTERN")
                                         for i, name in enumerate(("Front entrance", "Driveway", "Garden", "Side gate"))]
            self.scan_message = "Demo mode"
        else:
            try:
                self.nvrs = self.store.load()
            except StorageError as error:
                self.errors["config"] = str(error)
            try:
                self.excluded_camera_ids = self.preferences.load()
            except StorageError as error:
                self.errors["preferences"] = f"{error} Fix the preferences file and restart Camera Panel."
            for camera in self.snapshots.cached_cameras(self.nvrs):
                self.camera_sets.setdefault(camera.nvr_id, []).append(camera)
        self.tray = Tray(self.toggle_panel, self.show_settings, self.quit_cleanly,
                         str(Path(__file__).parent / "lookout.svg"))
        # Warm metadata in the background; video still starts only when opened.
        self.refresh_cameras()

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        if "--settings" in args:
            self.show_settings()
        elif "--background" not in args:
            self.toggle_panel()
        return 0

    def do_activate(self):
        self.toggle_panel()

    def run_async(self, work, finished):
        def run():
            try:
                result, error = work(), None
            except (OnvifError, StorageError) as exc:
                result, error = None, str(exc)
            except Exception:
                result, error = None, "Operation failed. Check the network and desktop keyring, then retry."

            def deliver():
                if not self.stopping:
                    finished(result, error)
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=run, daemon=True).start()

    def run_snapshot(self, work, finished=None):
        def done(future):
            try:
                result, error = future.result(), None
            except Exception:
                result, error = None, "Could not access the saved preview."
            if finished:
                def deliver():
                    if not self.stopping:
                        finished(result, error)
                    return False
                GLib.idle_add(deliver)
        self.snapshot_executor.submit(work).add_done_callback(done)

    def set_camera_selected(self, camera_id, selected):
        if self.preferences_busy or "preferences" in self.errors:
            return
        if not any(camera.id == camera_id and camera.nvr_id == nvr.id
                   for nvr in self.nvrs for camera in self.camera_sets.get(nvr.id, [])):
            self.errors["selection"] = "This camera is no longer available. Refresh Settings and try again."
            self.update_views()
            return
        excluded = self.excluded_camera_ids.copy()
        if selected:
            excluded.discard(camera_id)
        else:
            excluded.add(camera_id)
        if excluded == self.excluded_camera_ids:
            return
        self.errors.pop("selection", None)
        if self.demo:
            self.excluded_camera_ids = excluded
            self.update_views()
            return
        self.preferences_busy = True
        self.update_views()

        def saved(_result, error):
            self.preferences_busy = False
            if error is None:
                # Keep the saved set intact even if discovery refreshed the catalog meanwhile.
                self.excluded_camera_ids = excluded
                self.errors.pop("selection", None)
            else:
                self.errors["selection"] = error
            self.update_views()
        self.run_async(lambda: self.preferences.save(excluded), saved)

    def update_views(self):
        if self.devices and not self.scanning:
            detail = "checking configured NVRs..." if self.loading else f"{len(self.unconfigured_devices())} new"
            self.scan_message = f"{len(self.devices)} devices found / {detail}"
        if self.panel and self.panel.get_visible():
            self.panel.render()
        if self.settings:
            self.settings.render()

    def toggle_panel(self):
        if self.panel and self.panel.get_visible():
            self.hide_panel()
        else:
            self.show_panel()

    def show_panel(self):
        if self.panel is None:
            self.panel = Panel(self)
        self.panel.position()
        self.panel.set_visible(True)
        self.panel.render()
        # Every opening discovers devices, even when configured cameras already exist.
        self.scan()
        self.refresh_cameras()
        self.offer_firewall_permission()

    def hide_panel(self):
        if self.panel:
            self.panel.set_visible(False)
            self.panel.stop_streams()

    def scan(self):
        if self.scanning or self.demo:
            return
        self.scanning = True
        started = time.time()
        self.update_views()

        def scanned(devices, error):
            self.scanning = False
            self.devices = devices or []
            new_count = len(self.unconfigured_devices())
            self.scan_message = error or f"{len(self.devices)} devices found / {new_count} new"
            self.update_views()
            if not error and not self.devices:
                self.review_firewall(since=started)
        self.run_async(discover, scanned)

    def review_firewall(self, since=None, force=False):
        if self.demo or self.firewall_busy:
            return
        self.firewall_busy = True
        since = time.time() if since is None else since

        def checked(rules, error):
            self.firewall_busy = False
            if error:
                self.errors["discovery"] = error
                self.update_views()
                return
            rules = [rule for rule in rules if force or rule.key not in self.firewall_prompted]
            if rules:
                self.pending_firewall = rules[0]
                self.offer_firewall_permission()
            elif force:
                Gtk.AlertDialog(message="No blocked UFW discovery rule detected",
                                detail="UFW may already allow discovery, or may not be enabled. Camera Panel does not change other firewalls. Check that ONVIF is enabled and the NVR is on this local subnet.").show(self.settings)
        self.run_async(lambda: self.firewall.check(since, force=force), checked)

    def offer_firewall_permission(self):
        if (self.pending_firewall is None or self.firewall_busy or self.editor or self.saving
                or not (self.settings or self.panel and self.panel.get_visible())):
            return
        rule, self.pending_firewall = self.pending_firewall, None
        self.prompt_firewall(rule)

    def prompt_firewall(self, rule):
        self.firewall_busy = True
        self.firewall_prompted.add(rule.key)
        # Use a regular window as the modal parent, not the layer-shell overlay.
        self.hide_panel()
        if self.settings is None:
            self.settings = Settings(self)
        self.settings.present()
        diagnosis = ("UFW blocked a discovery reply from your local network." if rule.confirmed
                     else "UFW is enabled and may be blocking local discovery replies.")
        dialog = Gtk.AlertDialog(
            message="Allow local NVR discovery?",
            detail=f"{diagnosis}\n\nAllow only UDP replies from {rule.subnet}, source port 3702, "
                   f"to Camera Panel port 37020 on {rule.interface}. The firewall stays enabled; all other rules remain unchanged.\n\n"
                   "Administrator approval is required. Your choice is remembered for this network. "
                   "You can review it later in Discovery firewall settings. Manual NVR setup works without this permission.",
            buttons=["Not now", "Allow discovery"], cancel_button=0, default_button=0)

        def chosen(dialog, result):
            try:
                allow = dialog.choose_finish(result) == 1
            except GLib.Error:
                allow = False
            self.scan_message = "Waiting for administrator approval..." if allow else "Saving discovery preference..."
            self.update_views()

            def apply():
                if allow:
                    self.firewall.allow(rule)
                self.firewall.remember(rule)

            def finished(_result, error):
                self.firewall_busy = False
                if error:
                    self.errors["discovery"] = error
                    self.scan_message = error
                else:
                    self.errors.pop("discovery", None)
                    self.scan_message = "Discovery allowed on this network." if allow else "Firewall unchanged. You can add NVRs manually."
                    if allow:
                        self.scan()
                self.update_views()
            self.run_async(apply, finished)
        dialog.choose(self.settings, None, chosen)

    def unconfigured_devices(self):
        # A web URL can differ from the recorder's advertised ONVIF device URL.
        # Wait for capabilities rather than briefly suggesting the saved NVR again.
        if self.loading:
            return []
        known = {normalize_address(nvr.address) for nvr in self.nvrs}
        for nvr in self.nvrs:
            known.update(self.device_addresses.get(nvr.id, set()))
        return [device for device in self.devices if device.address not in known]

    def refresh_cameras(self):
        if self.demo or self.loading:
            return
        generation = self.generation
        self.loading = {nvr.id for nvr in self.nvrs}
        for nvr in self.nvrs:
            def progressive(camera, nvr=nvr):
                def deliver():
                    if self.stopping or generation != self.generation or nvr.id not in self.loading:
                        return False
                    cameras = self.camera_sets.setdefault(nvr.id, [])
                    for index, previous in enumerate(cameras):
                        if previous.id == camera.id:
                            # Preserve cached friendly labels until the optional name lookup finishes.
                            camera.name = previous.name
                            cameras[index] = camera
                            break
                    else:
                        cameras.append(camera)
                    self.update_views()
                    return False
                GLib.idle_add(deliver)

            def load(nvr=nvr, progressive=progressive):
                with self.network_slots:
                    password = self.vault.get(nvr.id)
                    client = ONVIFClient(nvr.address, nvr.username, password)
                    cameras = client.cameras(nvr.id, nvr.name, on_camera=progressive)
                    return cameras, client.device_addresses

            def loaded(result, error, nvr=nvr):
                if generation != self.generation:
                    return
                self.loading.discard(nvr.id)
                if error:
                    self.errors[nvr.id] = f"{nvr.name}: {error}"
                else:
                    cameras, addresses = result
                    self.errors.pop(nvr.id, None)
                    self.camera_sets[nvr.id] = cameras
                    self.device_addresses[nvr.id] = addresses
                self.update_views()
            self.run_async(load, loaded)
        self.update_views()

    def device_row(self, device):
        row = box(spacing=12)
        row.add_css_class("device")
        details = box(vertical=True, spacing=3)
        details.set_hexpand(True)
        details.append(label(device.name, "camera-name"))
        details.append(label(device.address, "muted"))
        row.append(details)
        row.append(button("Add NVR", lambda: self.show_editor(device=device), "primary"))
        return row

    def show_settings(self):
        self.hide_panel()
        if self.settings is None:
            self.settings = Settings(self)
        self.settings.present()
        self.scan()
        self.refresh_cameras()
        self.offer_firewall_permission()

    def show_editor(self, nvr=None, device=None):
        if self.demo:
            return
        if "config" in self.errors:
            self.show_settings()
            self.settings.info.set_label(self.errors["config"] + " Fix the config before adding recorders.")
            return
        self.show_settings()
        if self.editor is None:
            self.editor = Editor(self, nvr, device)
        self.editor.present()

    def remove_nvr(self, nvr):
        if self.saving or self.demo:
            return
        dialog = Gtk.AlertDialog(message=f"Remove {nvr.name}?",
                                 detail="This forgets the recorder and its saved password. It does not change the NVR or its recordings.",
                                 buttons=["Cancel", "Remove"], cancel_button=0, default_button=0)

        def chosen(dialog, result):
            try:
                if dialog.choose_finish(result) != 1 or self.saving:
                    return
            except GLib.Error:
                return
            self.saving = True
            records = [item for item in self.nvrs if item.id != nvr.id]

            def remove():
                password = self.vault.get(nvr.id)
                self.vault.delete(nvr.id)
                try:
                    self.store.save(records)
                except StorageError:
                    self.vault.set(nvr.id, password)
                    raise

            def removed(_result, error):
                self.saving = False
                if error:
                    self.errors[nvr.id] = error
                else:
                    self.nvrs = records
                    self.camera_sets.pop(nvr.id, None)
                    self.device_addresses.pop(nvr.id, None)
                    self.errors.pop(nvr.id, None)
                    self.generation += 1
                    self.loading.clear()
                    self.refresh_cameras()
                self.update_views()
                if not error:
                    self.run_snapshot(lambda: self.snapshots.remove_nvr(nvr.id))
            self.run_async(remove, removed)
        dialog.choose(self.settings, None, chosen)

    def quit_cleanly(self):
        self.stopping = True
        self.hide_panel()
        if self.tray:
            self.tray.close()
        self.quit()
        return False

    def do_shutdown(self):
        self.stopping = True
        self.hide_panel()
        if self.tray:
            self.tray.close()
        self.snapshot_executor.shutdown(wait=True)
        self.snapshots.flush()
        Gtk.Application.do_shutdown(self)
