"""Tray icon using the StatusNotifierItem D-Bus protocol.

Works on KDE, XFCE, Cinnamon, MATE and on GNOME with the AppIndicator
extension. GTK4 has no tray API of its own, so we speak D-Bus directly.

Click the icon = show the window. If no tray host is running,
start() returns False and the app behaves like a normal window app.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from gi.repository import Gio, GLib

from . import APP_ID

_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>
"""

_PATH = "/StatusNotifierItem"
_WATCHER = "org.kde.StatusNotifierWatcher"


class Tray:
    def __init__(self, on_activate: Callable[[], None]) -> None:
        self._on_activate = on_activate
        self._conn: Optional[Gio.DBusConnection] = None
        self._owner_id = 0
        self._reg_id = 0
        self._status = "Active"
        self.active = False  # True once a tray host accepted us

    # --- public ---------------------------------------------------------

    def start(self) -> bool:
        """Try to put the icon in the tray. Returns False if no tray exists."""
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            node = Gio.DBusNodeInfo.new_for_xml(_XML)
            self._reg_id = self._conn.register_object(
                _PATH, node.interfaces[0], self._on_method, self._on_get_property, None
            )
            name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            self._owner_id = Gio.bus_own_name_on_connection(
                self._conn, name, Gio.BusNameOwnerFlags.NONE, None, None
            )
            reply = self._conn.call_sync(
                _WATCHER, "/StatusNotifierWatcher", _WATCHER,
                "RegisterStatusNotifierItem", GLib.Variant("(s)", (name,)),
                None, Gio.DBusCallFlags.NONE, 1500, None,
            )
            del reply
            self.active = True
        except GLib.Error:
            self.stop()
        return self.active

    def stop(self) -> None:
        if self._conn is not None:
            if self._reg_id:
                self._conn.unregister_object(self._reg_id)
            if self._owner_id:
                Gio.bus_unown_name(self._owner_id)
        self._reg_id = self._owner_id = 0
        self._conn = None
        self.active = False

    def set_attention(self, needs: bool) -> None:
        """Highlight the icon (printer stopped / job failed)."""
        status = "NeedsAttention" if needs else "Active"
        if status == self._status or self._conn is None:
            return
        self._status = status
        self._conn.emit_signal(
            None, _PATH, "org.kde.StatusNotifierItem", "NewStatus",
            GLib.Variant("(s)", (status,)),
        )

    # --- D-Bus callbacks ------------------------------------------------

    def _on_method(self, _conn, _sender, _path, _iface, method, _params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            GLib.idle_add(lambda: self._on_activate() or False)
        invocation.return_value(None)

    def _on_get_property(self, _conn, _sender, _path, _iface, prop):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", "printo"),
            "Status": GLib.Variant("s", self._status),
            "WindowId": GLib.Variant("u", 0),
            "IconName": GLib.Variant("s", APP_ID),
            "AttentionIconName": GLib.Variant("s", "printer-error-symbolic"),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/NO_DBUSMENU"),
        }
        return values.get(prop)
