"""Printers page and the "Add printer" window."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .backend import (  # noqa: E402
    EVERYWHERE,
    Backend,
    Device,
    Driver,
    Printer,
    PrinterState,
    suggest_printer_name,
    supports_driverless,
)
from .gui_util import run_async  # noqa: E402

_STATE_TEXT = {
    PrinterState.IDLE: "Ready",
    PrinterState.PROCESSING: "Printing",
    PrinterState.STOPPED: "Stopped",
}


def printer_signature(printers: list[Printer]) -> tuple:
    """Cheap fingerprint, so the UI only rebuilds when something changed."""
    return tuple(
        (p.name, p.label, int(p.state), p.state_message, p.is_default) for p in printers
    )


def _icon_button(icon: str, tip: str, fn: Callable[[], None]) -> Gtk.Button:
    btn = Gtk.Button(
        icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER, css_classes=["flat"]
    )
    btn.connect("clicked", lambda _b: fn())
    return btn


class PrintersPage(Adw.PreferencesPage):
    """List of printers with: set default, test page, remove, add."""

    def __init__(self, backend: Backend, window: Gtk.Window) -> None:
        super().__init__()
        self.backend = backend
        self.window = window  # needs .toast(text) and .refresh_all()
        self._rows: list[Gtk.Widget] = []
        self._printers: list[Printer] = []

        self.group = Adw.PreferencesGroup(
            title="Printers",
            description="Adding or removing printers needs admin rights (lpadmin group).",
        )
        add = Gtk.Button(
            label="Add…", valign=Gtk.Align.CENTER, css_classes=["suggested-action"]
        )
        add.connect("clicked", lambda _b: self.open_add_window())
        self.group.set_header_suffix(add)
        self.add(self.group)

        self.empty_row = Adw.ActionRow(
            title="No printers yet", subtitle="Press Add… to set one up"
        )
        self.group.add(self.empty_row)

    # --- list -----------------------------------------------------------

    def set_printers(self, printers: list[Printer]) -> None:
        self._printers = printers
        for row in self._rows:
            self.group.remove(row)
        self._rows = []
        self.empty_row.set_visible(not printers)

        for p in printers:
            sub = _STATE_TEXT.get(p.state, "?")
            if p.state_message:
                sub += f" · {p.state_message}"
            if p.is_default:
                sub += " · default"
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(p.label),
                subtitle=GLib.markup_escape_text(sub),
            )
            row.add_prefix(Gtk.Image(icon_name="printer-symbolic"))
            if p.is_default:
                star = Gtk.Image(icon_name="starred-symbolic", valign=Gtk.Align.CENTER)
                star.set_tooltip_text("Default printer")
                row.add_suffix(star)
            else:
                row.add_suffix(
                    _icon_button(
                        "non-starred-symbolic", "Make default",
                        lambda n=p.name: self._set_default(n),
                    )
                )
            row.add_suffix(
                _icon_button(
                    "document-print-symbolic", "Print test page",
                    lambda n=p.name, label=p.label: self._test_page(n, label),
                )
            )
            row.add_suffix(
                _icon_button(
                    "user-trash-symbolic", "Remove printer",
                    lambda n=p.name, label=p.label: self._confirm_remove(n, label),
                )
            )
            self.group.add(row)
            self._rows.append(row)

    # --- actions --------------------------------------------------------

    def _admin_action(
        self, work: Callable[[], object], ok_text: str
    ) -> None:
        def done(_r: object, error: Optional[Exception]) -> None:
            self.window.toast(str(error) if error else ok_text)
            self.window.refresh_all()

        run_async(work, done)

    def _set_default(self, name: str) -> None:
        self._admin_action(lambda: self.backend.set_default(name), "Default printer changed")

    def _test_page(self, name: str, label: str) -> None:
        self._admin_action(
            lambda: self.backend.print_test_page(name), f"Test page sent to {label}"
        )

    def _confirm_remove(self, name: str, label: str) -> None:
        dialog = Adw.MessageDialog.new(
            self.window, f"Remove {label}?",
            "The printer and its waiting jobs will be removed from this computer.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.MessageDialog, response: str) -> None:
            if response == "remove":
                self._admin_action(
                    lambda: self.backend.delete_printer(name), f"Removed {label}"
                )

        dialog.connect("response", on_response)
        dialog.present()

    def open_add_window(self) -> None:
        AddPrinterWindow(
            self.window, self.backend,
            existing={p.name for p in self._printers},
        ).present()


class AddPrinterWindow(Adw.Window):
    def __init__(self, parent: Gtk.Window, backend: Backend, existing: set[str]) -> None:
        super().__init__(
            transient_for=parent, modal=True, title="Add printer",
            default_width=480, default_height=700,
        )
        self.parent_win = parent
        self.backend = backend
        self.existing = existing
        self.devices: list[Device] = []
        self.drivers: list[Driver] = []
        self._name_touched = False
        self._setting_name = False

        page = Adw.PreferencesPage()

        # --- where is the printer?
        group = Adw.PreferencesGroup(title="Printer")
        scan_row = Adw.ActionRow(
            title="Search network and USB",
            subtitle="Takes about 10 seconds",
        )
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.scan_btn = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_btn.connect("clicked", lambda _b: self._scan())
        scan_row.add_suffix(self.spinner)
        scan_row.add_suffix(self.scan_btn)
        group.add(scan_row)

        self.device_row = Adw.ComboRow(
            title="Found printers", model=Gtk.StringList.new([]), sensitive=False
        )
        self.device_row.connect("notify::selected", self._on_device_changed)
        group.add(self.device_row)

        self.address_row = Adw.EntryRow(title="Or type an address, e.g. ipp://192.168.1.20/ipp/print")
        self.address_row.connect("changed", self._on_address_changed)
        group.add(self.address_row)
        page.add(group)

        # --- name
        group = Adw.PreferencesGroup(title="Details")
        self.name_row = Adw.EntryRow(title="Name (letters, numbers, - and _)")
        self.name_row.connect("changed", self._on_name_changed)
        self.location_row = Adw.EntryRow(title="Location (optional)")
        group.add(self.name_row)
        group.add(self.location_row)
        page.add(group)

        # --- driver
        group = Adw.PreferencesGroup(title="Driver")
        self.mode_row = Adw.ComboRow(
            title="Driver",
            model=Gtk.StringList.new(["Driverless (recommended)", "Choose a driver…"]),
        )
        self.mode_row.connect("notify::selected", self._on_mode_changed)
        group.add(self.mode_row)
        self.search_row = Adw.EntryRow(title="Search drivers, e.g. HP LaserJet")
        self.search_row.set_show_apply_button(True)
        self.search_row.connect("apply", lambda _r: self._search_drivers())
        self.driver_row = Adw.ComboRow(
            title="Matching drivers", model=Gtk.StringList.new([])
        )
        group.add(self.search_row)
        group.add(self.driver_row)
        self.search_row.set_visible(False)
        self.driver_row.set_visible(False)
        page.add(group)

        # --- go
        group = Adw.PreferencesGroup()
        self.add_btn = Gtk.Button(
            label="Add printer", halign=Gtk.Align.CENTER,
            css_classes=["suggested-action", "pill"],
        )
        self.add_btn.connect("clicked", lambda _b: self._add())
        group.add(self.add_btn)
        page.add(group)

        self.toasts = Adw.ToastOverlay(child=page)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(Adw.HeaderBar())
        self.set_content(view)

    # --- helpers --------------------------------------------------------

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    def _device_uri(self) -> str:
        typed = self.address_row.get_text().strip()
        if typed:
            return typed
        i = self.device_row.get_selected()
        if self.devices and 0 <= i < len(self.devices):
            return self.devices[i].uri
        return ""

    def _suggest(self, label: str) -> None:
        if self._name_touched:
            return
        self._setting_name = True
        self.name_row.set_text(suggest_printer_name(label) if label else "")
        self._setting_name = False

    def _auto_mode(self) -> None:
        """Driverless when the address looks like an IPP printer."""
        uri = self._device_uri()
        if uri:
            self.mode_row.set_selected(0 if supports_driverless(uri) else 1)

    # --- signals --------------------------------------------------------

    def _on_name_changed(self, *_a: object) -> None:
        if not self._setting_name:
            self._name_touched = True

    def _on_device_changed(self, *_a: object) -> None:
        i = self.device_row.get_selected()
        if self.devices and 0 <= i < len(self.devices) and not self.address_row.get_text().strip():
            self._suggest(self.devices[i].label)
            self._auto_mode()

    def _on_address_changed(self, *_a: object) -> None:
        self._auto_mode()

    def _on_mode_changed(self, *_a: object) -> None:
        manual = self.mode_row.get_selected() == 1
        self.search_row.set_visible(manual)
        self.driver_row.set_visible(manual)

    # --- scan / search --------------------------------------------------

    def _scan(self) -> None:
        self.scan_btn.set_sensitive(False)
        self.spinner.start()

        def done(result: object, error: Optional[Exception]) -> None:
            self.spinner.stop()
            self.scan_btn.set_sensitive(True)
            if error:
                self._toast(str(error))
                return
            self.devices = result  # type: ignore[assignment]
            self.device_row.set_model(
                Gtk.StringList.new([f"{d.label}  ({d.uri})" for d in self.devices])
            )
            self.device_row.set_sensitive(bool(self.devices))
            if self.devices:
                self.device_row.set_selected(0)
                self._on_device_changed()
            else:
                self._toast("Nothing found. Type the address by hand.")

        run_async(self.backend.devices, done)

    def _search_drivers(self) -> None:
        query = self.search_row.get_text().strip()
        if not query:
            return

        def done(result: object, error: Optional[Exception]) -> None:
            if error:
                self._toast(str(error))
                return
            self.drivers = result  # type: ignore[assignment]
            self.driver_row.set_model(
                Gtk.StringList.new([d.make_model for d in self.drivers])
            )
            if not self.drivers:
                self._toast("No driver matches. Try fewer words.")

        run_async(lambda: self.backend.drivers(query), done)

    # --- add ------------------------------------------------------------

    def _add(self) -> None:
        uri = self._device_uri()
        name = self.name_row.get_text().strip()
        if not uri:
            self._toast("Scan and pick a printer, or type its address.")
            return
        if not name:
            self._toast("Give the printer a name.")
            return
        if name in self.existing:
            self._toast(f"A printer called '{name}' already exists.")
            return

        driver = EVERYWHERE
        if self.mode_row.get_selected() == 1:
            i = self.driver_row.get_selected()
            if not self.drivers or not 0 <= i < len(self.drivers):
                self._toast("Search for a driver and pick one first.")
                return
            driver = self.drivers[i].name

        location = self.location_row.get_text().strip()
        self.add_btn.set_sensitive(False)

        def done(_r: object, error: Optional[Exception]) -> None:
            self.add_btn.set_sensitive(True)
            if error:
                self._toast(str(error))
                return
            self.parent_win.toast(f"Added {name}")  # type: ignore[attr-defined]
            self.parent_win.refresh_all()  # type: ignore[attr-defined]
            self.close()

        run_async(
            lambda: self.backend.add_printer(name, uri, driver, info=name, location=location),
            done,
        )
