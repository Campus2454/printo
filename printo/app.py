"""printo GUI: GTK4 + libadwaita.

Two pages: Print and Queue. All CUPS calls run in a worker thread so the
window never freezes.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .backend import (  # noqa: E402
    PAPER_SIZES,
    SIDES,
    Backend,
    Job,
    JobState,
    Printer,
    PrintoError,
    build_options,
)

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


def run_async(
    work: Callable[[], object],
    done: Callable[[object, Optional[Exception]], None],
) -> None:
    """Run work() in a thread, then call done(result, error) on the UI thread."""

    def target() -> None:
        try:
            result, error = work(), None
        except Exception as exc:  # noqa: BLE001 - shown to user
            result, error = None, exc
        GLib.idle_add(lambda: done(result, error) or False)

    threading.Thread(target=target, daemon=True).start()


class PrintoWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend: Backend) -> None:
        super().__init__(application=app, title="printo")
        self.set_default_size(560, 680)
        self.backend = backend
        self.files: list[Path] = []
        self.printers: list[Printer] = []
        self._queue_rows: list[Gtk.Widget] = []

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(
            self._build_print_page(), "print", "Print", "printer-symbolic"
        )
        self.queue_page = self.stack.add_titled_with_icon(
            self._build_queue_page(), "queue", "Queue", "view-list-symbolic"
        )

        switcher = Adw.ViewSwitcher(
            stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE
        )
        header = Adw.HeaderBar(title_widget=switcher)

        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # drag files onto the window
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.refresh_printers()
        self.refresh_queue()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    # ---------------------------------------------------------------- print

    def _build_print_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

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
        self.printer_row = Adw.ComboRow(title="Printer")
        self.printer_row.set_model(Gtk.StringList.new([]))
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

        # print button
        group = Adw.PreferencesGroup()
        self.print_btn = Gtk.Button(
            label="Print", css_classes=["suggested-action", "pill"],
            halign=Gtk.Align.CENTER, sensitive=False,
        )
        self.print_btn.connect("clicked", self._on_print)
        group.add(self.print_btn)
        page.add(group)
        return page

    def _set_files(self, files: list[Path]) -> None:
        self.files = files
        if not files:
            self.file_row.set_title("No file chosen")
            self.file_row.set_subtitle("Drop files here, or choose")
        elif len(files) == 1:
            self.file_row.set_title(files[0].name)
            self.file_row.set_subtitle(str(files[0].parent))
        else:
            self.file_row.set_title(f"{len(files)} files")
            self.file_row.set_subtitle(", ".join(f.name for f in files[:3]))
        self._update_print_btn()

    def _update_print_btn(self) -> None:
        ok = bool(self.files) and bool(self.printers)
        self.print_btn.set_sensitive(ok)

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

        def done(result: object, error: Optional[Exception]) -> None:
            self._update_print_btn()
            if error:
                self.toast(str(error))
                return
            n = len(files)
            self.toast(f"Sent {n} file{'s' if n != 1 else ''} to {printer.label}")
            self._set_files([])
            self.refresh_queue()
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
        self.refresh_queue()
        return True  # keep timer

    def refresh_printers(self) -> None:
        def done(result: object, error: Optional[Exception]) -> None:
            if error:
                self.toast(str(error))
                return
            self.printers = result  # type: ignore[assignment]
            self.printer_row.set_model(
                Gtk.StringList.new([p.label for p in self.printers])
            )
            self.printer_row.set_selected(0)
            self._on_printer_changed()
            self._update_print_btn()
            if not self.printers:
                self.toast("No printers found. Add one in CUPS first.")

        run_async(self.backend.printers, done)

    def refresh_queue(self) -> None:
        def done(result: object, error: Optional[Exception]) -> None:
            if error:
                return  # quiet on poll; print page already showed errors
            self._show_jobs(result)  # type: ignore[arg-type]

        run_async(self.backend.jobs, done)

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
            self.refresh_queue()

        run_async(lambda: fn(job_id), done)

    def _cancel(self, job_id: int) -> None:
        self._job_action(self.backend.cancel, job_id)

    def _hold(self, job_id: int) -> None:
        self._job_action(self.backend.hold, job_id)

    def _release(self, job_id: int) -> None:
        self._job_action(self.backend.release, job_id)

    # ---------------------------------------------------------------- misc

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))


class PrintoApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN
        )
        self.backend = Backend()
        self.win: Optional[PrintoWindow] = None

    def _window(self) -> PrintoWindow:
        if self.win is None:
            self.win = PrintoWindow(self, self.backend)
        return self.win

    def do_activate(self) -> None:
        self._window().present()

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
