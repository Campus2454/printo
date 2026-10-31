"""Small GUI helpers shared by the windows."""

from __future__ import annotations

import threading
from typing import Callable, Optional

from gi.repository import GLib


def run_async(
    work: Callable[[], object],
    done: Callable[[object, Optional[Exception]], None],
) -> None:
    """Run work() in a thread, then call done(result, error) on the UI thread."""

    def target() -> None:
        try:
            result, error = work(), None
        except Exception as exc:  # noqa: BLE001 - shown to the user
            result, error = None, exc
        GLib.idle_add(lambda: done(result, error) or False)

    threading.Thread(target=target, daemon=True).start()
