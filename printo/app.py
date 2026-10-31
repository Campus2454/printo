"""printo GUI: GTK4 + libadwaita.

Three pages: Print, Queue, Printers. All CUPS calls run in worker threads
so the window never freezes. A tray icon and desktop notifications tell you
when a job is done or a printer stops.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .backend import (  # noqa: E402
    PAPER_SIZES,
    SIDES,
    Backend,
    Job,
    JobState,
    Printer,
    build_options,
)
from .gui_util import run_async  # noqa: E402
from .presets import Preset, PresetStore  # noqa: E402
from .printers_ui import PrintersPage, printer_signature  # noqa: E402
from .tray import Tray  # noqa: E402
from .watch import JobWatcher, PrinterWatcher  # noqa: E402

REFRESH_SECONDS = 3

_STATE_TEXT = {
    JobState.PENDING: "Waiting",
    JobState.HELD: "On hold",
    JobState.PROCESSING: "Printing",
    JobState.STOPPED: "Stopped",
    JobState.CANCELED: "Canceled",
    JobState.ABORTED: "Failed",
    JobState.COMPLETED: "Done",
}


def _select(row: Adw.ComboRow, keys: list[str], value: str) -> None:
    """Select the combo entry whose key equals value (if there is one)."""
    if value in keys:
        row.set_selected(keys.index(value))


class PrintoWindow(Adw.ApplicationWindow):
    def __init__(self, app: "PrintoApp") -> None:
        super().__init__(application=app, title="printo")
        self.set_default_size(580, 720)
        self.app = app
        self.backend = app.backend
        self.presets = app.presets
        self.files: list[Path] = []
        self.printers: list[Printer] = []
        self._printer_sig: tuple = ()
        self._queue_rows: list[Gtk.Widget] = []
        self._preset_list: list[Preset] = []
        self._loading_presets = False
        self._polling = False
        self._cups_down = False
        self._failed_unseen = False

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(
            self._build_print_page(), "print", "Print", "printer-symbolic"
        )
        self.queue_page = self.stack.add_titled_with_icon(
            self._build_queue_page(), "queue", "Queue", "view-list-symbolic"
        )
        self.printers_page = PrintersPage(self.backend, self)
        self.stack.add_titled_with_icon(
            self.printers_page, "printers", "Printers", "preferences-system-symbolic"
        )

        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=switcher)
        menu = Gio.Menu()
        menu.append("About printo", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.connect("close-request", self._on_close)
        self.connect("notify::is-active", self._on_active_changed)

        self._reload_presets()
        self.refresh_all()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    # ------------------------------------------------------------ lifecycle

    def _on_close(self, _w: Gtk.Window) -> bool:
        if self.app.tray.active:
            self.set_visible(False)  # keep running in the tray
            return True
        return False

    def _on_active_changed(self, *_a: object) -> None:
        if self.is_active():
            self._failed_unseen = False
            self._update_tray()

    def _update_tray(self) -> None:
        self.app.tray.set_attention(self._failed_unseen or self.app.printwatch.any_stopped)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    # ---------------------------------------------------------------- print

    def _build_print_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # presets
        group = Adw.PreferencesGroup(title="Preset")
        self.preset_row = Adw.ComboRow(title="Preset", model=Gtk.StringList.new([]))
        self.preset_row.connect("notify::selected", self._on_preset_selected)
        self.preset_delete = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text="Delete this preset",
            valign=Gtk.Align.CENTER, css_classes=["flat"],
        )
        self.preset_delete.connect("clicked", lambda _b: self._delete_preset())
        self.preset_row.add_suffix(self.preset_delete)
        group.add(self.preset_row)
        self.preset_save = Adw.EntryRow(title="Save these settings as a preset…")
        self.preset_save.set_show_apply_button(True)
        self.preset_save.connect("apply", self._on_save_preset)
        group.add(self.preset_save)
        page.add(group)

        # file
        group = Adw.PreferencesGroup(title="File")
        self.file_row = Adw.ActionRow(
            title="No file chosen", subtitle="Drop files here, or choose"
        )
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._on_choose)
        self.file_row.add_suffix(choose)
        group.add(self.file_row)
        page.add(group)

        # printer
        group = Adw.PreferencesGroup(title="Printer")
        self.printer_row = Adw.ComboRow(title="Printer", model=Gtk.StringList.new([]))
        self.printer_status = Gtk.Label(css_classes=["dim-label"])
        group.add(self.printer_row)
        group.add(self.printer_status)
        self.printer_row.connect("notify::selected", self._on_printer_changed)
        page.add(group)

        # options
        group = Adw.PreferencesGroup(title="Options")
        self.copies_row = Adw.SpinRow.new_with_range(1, 999, 1)
        self.copies_row.set_title("Copies")
        group.add(self.copies_row)

        self._side_keys = list(SIDES)
        self.sides_row = Adw.ComboRow(
            title="Sides", model=Gtk.StringList.new(list(SIDES.values()))
        )
        group.add(self.sides_row)

        self._paper_keys = ["", *PAPER_SIZES]
        self.paper_row = Adw.ComboRow(
            title="Paper",
            model=Gtk.StringList.new(["Printer default", *PAPER_SIZES]),
        )
        group.add(self.paper_row)

        self._orient_keys = ["", "portrait", "landscape"]
        self.orient_row = Adw.ComboRow(
            title="Orientation",
            model=Gtk.StringList.new(["Automatic", "Portrait", "Landscape"]),
        )
        group.add(self.orient_row)

        self.pages_row = Adw.EntryRow(title="Pages (e.g. 1-3,5) — empty = all")
        group.add(self.pages_row)
        page.add(group)

        group = Adw.PreferencesGroup()
        self.print_btn = Gtk.Button(
            label="Print", css_classes=["suggested-action", "pill"],
            halign=Gtk.Align.CENTER, sensitive=False,
        )
        self.print_btn.connect("clicked", self._on_print)
        group.add(self.print_btn)
        page.add(group)
        return page

    # presets -------------------------------------------------------------

    def _reload_presets(self, select: str = "") -> None:
        self._loading_presets = True
        self._preset_list = self.presets.all()
        self.preset_row.set_model(Gtk.StringList.new([p.name for p in self._preset_list]))
        names = [p.name for p in self._preset_list]
        self.preset_row.set_selected(names.index(select) if select in names else 0)
        self._loading_presets = False
        self._update_preset_delete()

    def _current_preset(self) -> Optional[Preset]:
        i = self.preset_row.get_selected()
        return self._preset_list[i] if 0 <= i < len(self._preset_list) else None

    def _update_preset_delete(self) -> None:
        p = self._current_preset()
        self.preset_delete.set_sensitive(bool(p) and not self.presets.is_builtin(p.name))

    def _on_preset_selected(self, *_a: object) -> None:
        self._update_preset_delete()
        if self._loading_presets:
            return
        p = self._current_preset()
        if p is None:
            return
        self.copies_row.set_value(p.copies)
        _select(self.sides_row, self._side_keys, p.sides)
        _select(self.paper_row, self._paper_keys, p.paper)
        _select(self.orient_row, self._orient_keys, p.orientation)
        if p.printer:
            _select(self.printer_row, [pr.name for pr in self.printers], p.printer)

    def _on_save_preset(self, row: Adw.EntryRow) -> None:
        name = row.get_text().strip()
        if not name:
            return
        printer = self._selected_printer()
        try:
            self.presets.save(
                Preset(
                    name=name,
                    copies=int(self.copies_row.get_value()),
                    sides=self._side_keys[self.sides_row.get_selected()],
                    paper=self._paper_keys[self.paper_row.get_selected()],
                    orientation=self._orient_keys[self.orient_row.get_selected()],
                    printer=printer.name if printer else "",
                )
            )
        except (ValueError, OSError) as exc:
            self.toast(str(exc))
            return
        row.set_text("")
        self._reload_presets(select=name)
        self.toast(f"Saved preset '{name}'")

    def _delete_preset(self) -> None:
        p = self._current_preset()
        if p is None:
            return
        try:
            self.presets.delete(p.name)
        except (ValueError, OSError) as exc:
            self.toast(str(exc))
            return
        self._reload_presets()
        self.toast(f"Deleted preset '{p.name}'")

    # files ---------------------------------------------------------------

    def _set_files(self, files: list[Path]) -> None:
        self.files = files
        if not files:
            self.file_row.set_title("No file chosen")
            self.file_row.set_subtitle("Drop files here, or choose")
        elif len(files) == 1:
            self.file_row.set_title(GLib.markup_escape_text(files[0].name))
            self.file_row.set_subtitle(GLib.markup_escape_text(str(files[0].parent)))
        else:
            self.file_row.set_title(f"{len(files)} files")
            self.file_row.set_subtitle(
                GLib.markup_escape_text(", ".join(f.name for f in files[:3]))
            )
        self._update_print_btn()

    def _update_print_btn(self) -> None:
        self.print_btn.set_sensitive(bool(self.files) and bool(self.printers))

    def _on_choose(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choose files to print")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                picked = dlg.open_multiple_finish(result)
            except GLib.Error:
                return  # cancelled
            self._set_files(
                [Path(picked.get_item(i).get_path()) for i in range(picked.get_n_items())]
            )

        dialog.open_multiple(self, None, done)

    def _on_drop(self, _t: Gtk.DropTarget, value: Gdk.FileList, _x: float, _y: float) -> bool:
        paths = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
        if not paths:
            return False
        self._set_files(paths)
        return True

    def open_files(self, paths: list[Path]) -> None:
        self._set_files(paths)
        self.stack.set_visible_child_name("print")

    # printing ------------------------------------------------------------

    def _selected_printer(self) -> Optional[Printer]:
        i = self.printer_row.get_selected()
        if 0 <= i < len(self.printers):
            return self.printers[i]
        return None

    def _on_printer_changed(self, *_a: object) -> None:
        p = self._selected_printer()
        if p is None:
            self.printer_status.set_label("")
        elif p.is_ready:
            self.printer_status.set_label(p.state_message or "Ready")
        else:
            self.printer_status.set_label(f"Stopped: {p.state_message or 'check printer'}")

    def _on_print(self, _btn: Gtk.Button) -> None:
        printer = self._selected_printer()
        if printer is None or not self.files:
            return
        try:
            opts = build_options(
                copies=int(self.copies_row.get_value()),
                sides=self._side_keys[self.sides_row.get_selected()],
                page_ranges=self.pages_row.get_text(),
                paper=self._paper_keys[self.paper_row.get_selected()],
                orientation=self._orient_keys[self.orient_row.get_selected()],
            )
        except ValueError as exc:
            self.toast(str(exc))
            return

        files = list(self.files)
        self.print_btn.set_sensitive(False)

        def work() -> list[int]:
            return [self.backend.print_file(printer.name, f, opts) for f in files]

        def done(_result: object, error: Optional[Exception]) -> None:
            self._update_print_btn()
            if error:
                self.toast(str(error))
                return
            n = len(files)
            self.toast(f"Sent {n} file{'s' if n != 1 else ''} to {printer.label}")
            self._set_files([])
            self.refresh_all()
            self.stack.set_visible_child_name("queue")

        run_async(work, done)

    # ---------------------------------------------------------------- queue

    def _build_queue_page(self) -> Gtk.Widget:
        self.queue_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
            valign=Gtk.Align.START,
        )
        self.queue_empty = Adw.StatusPage(
            icon_name="emblem-ok-symbolic",
            title="Nothing in the queue",
            description="Print jobs show up here.",
        )
        self.queue_stack = Gtk.Stack()
        scroll = Gtk.ScrolledWindow(child=self.queue_list, vexpand=True)
        self.queue_stack.add_named(scroll, "list")
        self.queue_stack.add_named(self.queue_empty, "empty")
        return self.queue_stack

    def _tick(self) -> bool:
        self.refresh_all(quiet=True)
        return True  # keep the timer

    def refresh_all(self, quiet: bool = False) -> None:
        """Poll CUPS once: printers + jobs + what changed since last time."""
        if self._polling:
            return
        self._polling = True

        def work() -> tuple:
            printers = self.backend.printers()
            jobs = self.backend.jobs()
            events = self.app.jobwatch.update(jobs, self.backend.job_final_state)
            stopped = self.app.printwatch.update(printers)
            return printers, jobs, events, stopped

        def done(result: object, error: Optional[Exception]) -> None:
            self._polling = False
            if error:
                if not self._cups_down:  # say it once, not every 3 seconds
                    self.toast(str(error))
                self._cups_down = True
                self.queue_empty.set_title("Cannot reach CUPS")
                self.queue_empty.set_description(str(error))
                self.queue_stack.set_visible_child_name("empty")
                return
            if self._cups_down:
                self._cups_down = False
                self.queue_empty.set_title("Nothing in the queue")
                self.queue_empty.set_description("Print jobs show up here.")
            printers, jobs, events, stopped = result  # type: ignore[misc]
            self._apply_printers(printers)
            self._show_jobs(jobs)
            self._notify(events, stopped)

        run_async(work, done)

    def _apply_printers(self, printers: list[Printer]) -> None:
        sig = printer_signature(printers)
        if sig == self._printer_sig:
            return
        self._printer_sig = sig
        keep = self._selected_printer()
        self.printers = printers
        self.printer_row.set_model(Gtk.StringList.new([p.label for p in printers]))
        names = [p.name for p in printers]
        self.printer_row.set_selected(
            names.index(keep.name) if keep and keep.name in names else 0
        )
        self._on_printer_changed()
        self._update_print_btn()
        self.printers_page.set_printers(printers)

    def _show_jobs(self, jobs: list[Job]) -> None:
        for row in self._queue_rows:
            self.queue_list.remove(row)
        self._queue_rows = []

        for job in jobs:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(job.title),
                subtitle=f"{_STATE_TEXT.get(job.state, '?')} · {job.printer} · {job.size_kb} KB",
            )
            if job.state in (JobState.PENDING, JobState.PROCESSING):
                row.add_suffix(self._job_button("media-playback-pause-symbolic", "Hold", self._hold, job.id))
            elif job.is_held:
                row.add_suffix(self._job_button("media-playback-start-symbolic", "Resume", self._release, job.id))
            row.add_suffix(self._job_button("process-stop-symbolic", "Cancel", self._cancel, job.id))
            self.queue_list.append(row)
            self._queue_rows.append(row)

        self.queue_stack.set_visible_child_name("list" if jobs else "empty")
        self.queue_page.set_badge_number(len(jobs))

    def _notify(self, events: list, stopped: list[Printer]) -> None:
        for ev in events:
            if ev.kind == "done":
                self.app.notify(f"job-{ev.job.id}", f"Printed: {ev.job.title}", f"on {ev.job.printer}")
            else:
                self._failed_unseen = not self.is_active()
                self.app.notify(f"job-{ev.job.id}", f"Print failed: {ev.job.title}", f"on {ev.job.printer}")
        for p in stopped:
            self.app.notify(f"printer-{p.name}", f"Printer stopped: {p.label}", p.state_message or "Check the printer")
        self._update_tray()

    def _job_button(self, icon: str, tip: str, fn: Callable[[int], None], job_id: int) -> Gtk.Button:
        btn = Gtk.Button(
            icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        btn.connect("clicked", lambda _b: fn(job_id))
        return btn

    def _job_action(self, fn: Callable[[int], None], job_id: int) -> None:
        def done(_r: object, error: Optional[Exception]) -> None:
            if error:
                self.toast(str(error))
            self.refresh_all()

        run_async(lambda: fn(job_id), done)

    def _cancel(self, job_id: int) -> None:
        self._job_action(self.backend.cancel, job_id)

    def _hold(self, job_id: int) -> None:
        self._job_action(self.backend.hold, job_id)

    def _release(self, job_id: int) -> None:
        self._job_action(self.backend.release, job_id)


class PrintoApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN
        )
        self.add_main_option(
            "background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Start hidden in the tray", None,
        )
        self.backend = Backend()
        self.presets = PresetStore()
        self.jobwatch = JobWatcher()
        self.printwatch = PrinterWatcher()
        self.tray = Tray(self.show_window)
        self.win: Optional[PrintoWindow] = None
        self.background = False
        self.connect("handle-local-options", self._on_local_options)

    def _on_local_options(self, _app: Gio.Application, options: GLib.VariantDict) -> int:
        self.background = options.contains("background")
        return -1  # carry on and start normally

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        for name, fn in (("quit", self._quit), ("about", self._about), ("show", lambda *_a: self.show_window())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", fn)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        if self.tray.start():
            self.hold()  # stay alive with the window hidden

    def _quit(self, *_a: object) -> None:
        self.tray.stop()
        self.quit()

    def _about(self, *_a: object) -> None:
        Adw.AboutWindow(
            transient_for=self.win, application_name="printo",
            application_icon=APP_ID, version=__version__,
            developer_name="Campus2454", license_type=Gtk.License.MIT_X11,
            website="https://github.com/Campus2454/printo",
            comments="A small, fast GUI for CUPS",
        ).present()

    def _window(self) -> PrintoWindow:
        if self.win is None:
            self.win = PrintoWindow(self)
        return self.win

    def show_window(self) -> None:
        self._window().present()

    def notify(self, notif_id: str, title: str, body: str) -> None:
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_default_action("app.show")
        self.send_notification(notif_id, n)

    def do_activate(self) -> None:
        win = self._window()
        if self.background and self.tray.active:
            self.background = False  # only the first start is silent
            return
        win.present()

    def do_open(self, files: list[Gio.File], _n: int, _hint: str) -> None:
        win = self._window()
        win.open_files([Path(f.get_path()) for f in files if f.get_path()])
        win.present()


def main() -> int:
    if "--version" in sys.argv:
        print(f"printo {__version__}")
        return 0
    return PrintoApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
