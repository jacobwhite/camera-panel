"""GTK-independent StatusNotifierItem and dbusmenu, driven by the GLib main loop."""

from collections.abc import Callable
import logging
import os

from gi.repository import Gio, GLib


_ITEM = "org.kde.StatusNotifierItem"
_MENU = "com.canonical.dbusmenu"
_WATCHER = "org.kde.StatusNotifierWatcher"
_INTROSPECTION = Gio.DBusNodeInfo.new_for_xml("""
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated"><arg name="updatedProps" type="a(ia{sv})"/><arg name="removedProps" type="a(ias)"/></signal>
    <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
    <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
  </interface>
</node>
""")


class Tray:
    """Create and close on the GTK/GLib main thread; callbacks take no arguments.

    Requires a session bus but not a running watcher. Registration is retried
    whenever the watcher appears. No GTK namespace is imported or initialized.
    """

    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        icon_path: str,
    ) -> None:
        self._actions = {1: on_toggle, 2: on_settings, 3: on_quit}
        self._closed = False
        self._registrations = []
        self._watch = 0
        self._cancellable = Gio.Cancellable()
        icon_path = os.path.abspath(icon_path)
        self._properties = {
            _ITEM: {
                "Category": GLib.Variant("s", "Hardware"),
                "Id": GLib.Variant("s", "lookout"),
                "Title": GLib.Variant("s", "Camera Panel"),
                "Status": GLib.Variant("s", "Active"),
                "WindowId": GLib.Variant("u", 0),
                "IconName": GLib.Variant("s", icon_path),
                "IconThemePath": GLib.Variant("s", os.path.dirname(icon_path)),
                "IconPixmap": GLib.Variant("a(iiay)", []),
                "OverlayIconName": GLib.Variant("s", ""),
                "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
                "AttentionIconName": GLib.Variant("s", ""),
                "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
                "AttentionMovieName": GLib.Variant("s", ""),
                "ToolTip": GLib.Variant("(sa(iiay)ss)", (
                    icon_path, [], "Camera Panel",
                    "Show or hide cameras; manage NVRs from the menu.",
                )),
                "ItemIsMenu": GLib.Variant("b", False),
                "Menu": GLib.Variant("o", "/Menu"),
            },
            _MENU: {
                "Version": GLib.Variant("u", 3),
                "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", []),
            },
        }
        self._items = {0: {"children-display": GLib.Variant("s", "submenu")}}
        for item_id, label in enumerate(("Show / hide cameras", "Manage NVRs", "Quit"), 1):
            self._items[item_id] = {
                "label": GLib.Variant("s", label),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }

        # A private connection lets close() remove the watcher's entry without
        # disconnecting GTK's shared session bus. SNI has no unregister method.
        self._bus = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        self._bus.set_exit_on_close(False)
        try:
            for path, interface in (("/StatusNotifierItem", _ITEM), ("/Menu", _MENU)):
                self._registrations.append(self._bus.register_object(
                    path, _INTROSPECTION.lookup_interface(interface),
                    self._method_call, self._get_property, None,
                ))
            self._watch = Gio.bus_watch_name_on_connection(
                self._bus, _WATCHER, Gio.BusNameWatcherFlags.NONE,
                self._watcher_appeared, None,
            )
        except Exception:
            self.close()
            raise

    def _watcher_appeared(self, connection, name, owner):
        if not self._closed:
            connection.call(
                owner, "/StatusNotifierWatcher", _WATCHER,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (connection.get_unique_name(),)),
                None, Gio.DBusCallFlags.NONE, 5000, self._cancellable,
                self._registered, None,
            )

    def _registered(self, connection, result, user_data):
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            if not self._closed:
                logging.getLogger(__name__).warning("Tray registration failed: %s", error)

    def _get_property(self, connection, sender, path, interface, name):
        return self._properties[interface][name]

    def _item_properties(self, item_id, names):
        return {
            name: value for name, value in self._items[item_id].items()
            if not names or name in names
        }

    def _layout(self, item_id, depth, names):
        properties = self._item_properties(item_id, names)
        children = []
        if item_id == 0 and depth != 0:
            children = [
                GLib.Variant("(ia{sv}av)", self._layout(child, 0, names))
                for child in self._actions
            ]
        return item_id, properties, children

    def _method_call(self, connection, sender, path, interface, method, parameters, invocation):
        args = parameters.unpack()
        actions = []
        reply = None
        try:
            if interface == _ITEM:
                if method == "Activate":
                    actions.append(self._actions[1])
                elif method in ("SecondaryActivate", "ContextMenu"):
                    # Hosts with dbusmenu support render /Menu themselves.
                    actions.append(self._actions[2])
                elif method != "Scroll":
                    raise ValueError("Unknown method")
            elif method == "GetLayout":
                if args[1] < -1:
                    raise ValueError("recursionDepth must be -1 or nonnegative")
                reply = GLib.Variant("(u(ia{sv}av))", (1, self._layout(*args)))
            elif method == "GetGroupProperties":
                ids, names = args
                reply = GLib.Variant("(a(ia{sv}))", ([
                    (item_id, self._item_properties(item_id, names))
                    for item_id in ids if item_id in self._items
                ],))
            elif method == "GetProperty":
                reply = GLib.Variant("(v)", (self._items[args[0]][args[1]],))
            elif method in ("Event", "EventGroup"):
                events = [args] if method == "Event" else args[0]
                errors = []
                for item_id, event, data, timestamp in events:
                    if item_id not in self._items:
                        errors.append(item_id)
                    elif event == "clicked" and item_id in self._actions:
                        actions.append(self._actions[item_id])
                if method == "EventGroup":
                    reply = GLib.Variant("(ai)", (errors,))
                elif errors:
                    raise KeyError(errors[0])
            elif method == "AboutToShow":
                if args[0] not in self._items:
                    raise KeyError(args[0])
                reply = GLib.Variant("(b)", (False,))
            elif method == "AboutToShowGroup":
                reply = GLib.Variant("(aiai)", (
                    [], [item_id for item_id in args[0] if item_id not in self._items],
                ))
            else:
                raise ValueError("Unknown method")
        except (KeyError, ValueError) as error:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", str(error))
            return

        # Reply before callbacks: on_quit may close the connection immediately.
        invocation.return_value(reply)
        for action in actions:
            if self._closed:
                break
            try:
                action()
            except Exception:
                logging.getLogger(__name__).exception("Tray callback failed")

    def close(self) -> None:
        """Remove exports and watcher registration. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._cancellable.cancel()
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = 0
        for registration in self._registrations:
            self._bus.unregister_object(registration)
        self._registrations.clear()
        if not self._bus.is_closed():
            try:
                try:
                    self._bus.flush_sync(None)
                finally:
                    self._bus.close_sync(None)
            except GLib.Error:
                logging.getLogger(__name__).debug("Tray bus already disconnected", exc_info=True)
