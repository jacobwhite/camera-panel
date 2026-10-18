"""Run with python -m unittest discover -s tests -p test_tray.py -v."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

try:
    from gi.repository import Gio, GLib
except ImportError:
    Gio = None
else:
    from lookout.tray import Tray, _INTROSPECTION, _ITEM, _MENU, _WATCHER


@unittest.skipIf(Gio is None, "PyGObject Gio/GLib is unavailable")
class TrayTests(unittest.TestCase):
    def test_introspection(self):
        item = _INTROSPECTION.lookup_interface(_ITEM)
        menu = _INTROSPECTION.lookup_interface(_MENU)
        self.assertEqual(item.lookup_property("ToolTip").signature, "(sa(iiay)ss)")
        self.assertEqual(item.lookup_property("Menu").signature, "o")
        self.assertEqual(menu.lookup_property("Version").signature, "u")
        self.assertEqual(
            {method.name for method in menu.methods},
            {"GetLayout", "GetGroupProperties", "GetProperty", "Event",
             "EventGroup", "AboutToShow", "AboutToShowGroup"},
        )
        self.assertEqual(
            [arg.signature for arg in menu.lookup_method("GetLayout").out_args],
            ["u", "(ia{sv}av)"],
        )
        self.assertEqual(
            {signal.name for signal in menu.signals},
            {"ItemsPropertiesUpdated", "LayoutUpdated", "ItemActivationRequested"},
        )

    @unittest.skipUnless(shutil.which("dbus-run-session"), "dbus-run-session is unavailable")
    def test_isolated_session(self):
        result = subprocess.run(
            ["dbus-run-session", "--", sys.executable, "-B", "-c",
             "import runpy; runpy.run_path('tests/test_tray.py', run_name='__main__')",
             "--isolated-tray-tests"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# Only the explicit subprocess entry point loads these tests. No tray is ever
# exported on the user's session bus by normal unittest discovery.
@unittest.skipUnless(sys.argv[1:] == ["--isolated-tray-tests"], "requires isolated D-Bus subprocess")
class _IsolatedTrayTests(unittest.TestCase):
    def setUp(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.events = []
        self.tray = Tray(
            lambda: self.events.append("toggle"),
            lambda: self.events.append("settings"),
            lambda: self.events.append("quit"),
            "icons/lookout.svg",
        )
        self.addCleanup(self.tray.close)
        self.destination = self.tray._bus.get_unique_name()

    def wait_for(self, predicate):
        loop = GLib.MainLoop()
        timed_out = []

        def check():
            if predicate():
                loop.quit()
            return GLib.SOURCE_CONTINUE

        def expire():
            timed_out.append(True)
            loop.quit()
            return GLib.SOURCE_CONTINUE

        poll = GLib.timeout_add(5, check)
        timeout = GLib.timeout_add(2000, expire)
        try:
            loop.run()
        finally:
            GLib.source_remove(poll)
            GLib.source_remove(timeout)
        self.assertFalse(timed_out, "Timed out waiting for D-Bus activity")

    def call(self, interface, method, signature="()", args=(), path=None):
        results = []
        errors = []

        def finished(connection, result, data):
            try:
                results.append(connection.call_finish(result))
            except GLib.Error as error:
                errors.append(error)

        path = path or ("/Menu" if interface == _MENU else "/StatusNotifierItem")
        self.bus.call(
            self.destination, path, interface, method, GLib.Variant(signature, args),
            None, Gio.DBusCallFlags.NONE, 1500, None, finished, None,
        )
        self.wait_for(lambda: results or errors)
        if errors:
            raise errors[0]
        return results[0]

    def test_properties_and_exported_introspection(self):
        xml, = self.call("org.freedesktop.DBus.Introspectable", "Introspect").unpack()
        node = Gio.DBusNodeInfo.new_for_xml(xml)
        self.assertIsNotNone(node.lookup_interface(_ITEM))
        properties, = self.call(
            "org.freedesktop.DBus.Properties", "GetAll", "(s)", (_ITEM,),
        ).unpack()
        self.assertEqual(properties["Category"], "Hardware")
        self.assertEqual(properties["Id"], "lookout")
        self.assertEqual(properties["Title"], "Camera Panel")
        self.assertEqual(properties["Status"], "Active")
        self.assertFalse(properties["ItemIsMenu"])
        self.assertEqual(properties["Menu"], "/Menu")
        self.assertEqual(properties["IconName"], os.path.abspath("icons/lookout.svg"))
        self.assertEqual(properties["IconThemePath"], os.path.abspath("icons"))
        self.assertEqual(properties["ToolTip"][2], "Camera Panel")
        for interface, path in ((_ITEM, "/StatusNotifierItem"), (_MENU, "/Menu")):
            values = self.call(
                "org.freedesktop.DBus.Properties", "GetAll", "(s)", (interface,), path,
            ).get_child_value(0)
            for prop in _INTROSPECTION.lookup_interface(interface).properties:
                self.assertEqual(values.lookup_value(prop.name, None).get_type_string(), prop.signature)
        menu_properties, = self.call(
            "org.freedesktop.DBus.Properties", "GetAll", "(s)", (_MENU,), "/Menu",
        ).unpack()
        self.assertEqual(menu_properties, {
            "Version": 3, "TextDirection": "ltr", "Status": "normal", "IconThemePath": [],
        })
        self.assertFalse(any(
            name in sys.modules for name in
            ("gi.repository.Gtk", "gi.repository.AppIndicator3", "gi.repository.AyatanaAppIndicator3")
        ))

    def test_layout_and_property_filtering(self):
        revision, root = self.call(_MENU, "GetLayout", "(iias)", (0, -1, [])).unpack()
        self.assertEqual(revision, 1)
        self.assertEqual(root[:2], (0, {"children-display": "submenu"}))
        self.assertEqual([child[0] for child in root[2]], [1, 2, 3])
        self.assertEqual(
            [child[1]["label"] for child in root[2]],
            ["Show / hide cameras", "Manage NVRs", "Quit"],
        )
        for child in root[2]:
            self.assertTrue(child[1]["enabled"])
            self.assertTrue(child[1]["visible"])
            self.assertEqual(child[2], [])
        _, shallow = self.call(_MENU, "GetLayout", "(iias)", (0, 0, [])).unpack()
        self.assertEqual(shallow[2], [])
        _, filtered = self.call(_MENU, "GetLayout", "(iias)", (0, 1, ["label"])).unpack()
        self.assertEqual(filtered[1], {})
        self.assertEqual(filtered[2][0][1], {"label": "Show / hide cameras"})
        _, leaf = self.call(_MENU, "GetLayout", "(iias)", (2, -1, [])).unpack()
        self.assertEqual(leaf[0], 2)
        self.assertEqual(leaf[2], [])
        group, = self.call(
            _MENU, "GetGroupProperties", "(aias)", ([1, 2, 99], ["label", "missing"]),
        ).unpack()
        self.assertEqual(group, [
            (1, {"label": "Show / hide cameras"}), (2, {"label": "Manage NVRs"}),
        ])
        self.assertEqual(
            self.call(_MENU, "GetProperty", "(is)", (3, "label")).unpack(), ("Quit",),
        )

    def test_actions_and_menu_events(self):
        for method in ("Activate", "SecondaryActivate", "ContextMenu"):
            self.call(_ITEM, method, "(ii)", (10, 20))
        self.call(_ITEM, "Scroll", "(is)", (1, "vertical"))
        self.assertEqual(self.events, ["toggle", "settings", "settings"])
        self.events.clear()
        for item_id in (1, 2, 3):
            self.call(_MENU, "Event", "(isvu)", (item_id, "clicked", GLib.Variant("i", 0), 0))
        self.assertEqual(self.events, ["toggle", "settings", "quit"])
        self.events.clear()
        errors, = self.call(_MENU, "EventGroup", "(a(isvu))", ([
            (0, "opened", GLib.Variant("i", 0), 0),
            (1, "hovered", GLib.Variant("i", 0), 0),
            (2, "clicked", GLib.Variant("i", 0), 0),
            (99, "clicked", GLib.Variant("i", 0), 0),
        ],)).unpack()
        self.assertEqual(errors, [99])
        self.assertEqual(self.events, ["settings"])

    def test_about_to_show_and_errors(self):
        self.assertEqual(self.call(_MENU, "AboutToShow", "(i)", (0,)).unpack(), (False,))
        self.assertEqual(
            self.call(_MENU, "AboutToShowGroup", "(ai)", ([0, 1, 99],)).unpack(),
            ([], [99]),
        )
        for method, signature, args in (
            ("GetLayout", "(iias)", (99, -1, [])),
            ("GetLayout", "(iias)", (0, -2, [])),
            ("GetProperty", "(is)", (1, "missing")),
            ("AboutToShow", "(i)", (99,)),
            ("Event", "(isvu)", (99, "clicked", GLib.Variant("i", 0), 0)),
        ):
            with self.subTest(method=method, args=args), self.assertRaises(GLib.Error):
                self.call(_MENU, method, signature, args)

    def test_watcher_reappearance_and_close(self):
        registrations = []
        watcher_info = Gio.DBusNodeInfo.new_for_xml("""
            <node><interface name="org.kde.StatusNotifierWatcher">
              <method name="RegisterStatusNotifierItem">
                <arg name="service" type="s" direction="in"/>
              </method>
            </interface></node>
        """)

        def register(connection, sender, path, interface, method, parameters, invocation):
            registrations.append((sender, parameters.unpack()[0]))
            invocation.return_value(None)

        export = self.bus.register_object(
            "/StatusNotifierWatcher", watcher_info.interfaces[0], register, None, None,
        )
        self.addCleanup(self.bus.unregister_object, export)

        def bus_call(method, signature, args):
            return self.bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                method, GLib.Variant(signature, args), None, Gio.DBusCallFlags.NONE, 1500, None,
            ).unpack()[0]

        self.addCleanup(bus_call, "ReleaseName", "(s)", (_WATCHER,))
        self.assertEqual(bus_call("RequestName", "(su)", (_WATCHER, 0)), 1)
        self.wait_for(lambda: len(registrations) == 1)
        self.assertEqual(registrations[0], (self.destination, self.destination))
        bus_call("ReleaseName", "(s)", (_WATCHER,))
        self.assertEqual(bus_call("RequestName", "(su)", (_WATCHER, 0)), 1)
        self.wait_for(lambda: len(registrations) == 2)
        self.assertEqual(registrations[1], registrations[0])
        self.tray.close()
        self.tray.close()
        self.assertTrue(self.tray._bus.is_closed())
        self.assertEqual(self.tray._registrations, [])
        self.assertEqual(self.tray._watch, 0)
        self.assertFalse(bus_call("NameHasOwner", "(s)", (self.destination,)))
        self.assertFalse(self.bus.is_closed())
        replacement = Tray(lambda: None, lambda: None, lambda: None, "icons/lookout.svg")
        self.addCleanup(replacement.close)
        self.wait_for(lambda: len(registrations) == 3)
        name = replacement._bus.get_unique_name()
        self.assertEqual(registrations[2], (name, name))

    def test_quit_can_close_tray_before_client_receives_reply(self):
        self.tray._actions[3] = self.tray.close
        self.call(_MENU, "Event", "(isvu)", (3, "clicked", GLib.Variant("i", 0), 0))
        self.assertTrue(self.tray._bus.is_closed())


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(TrayTests)


if __name__ == "__main__":
    if sys.argv[1:] == ["--isolated-tray-tests"]:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_IsolatedTrayTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(not result.wasSuccessful())
    unittest.main()
