"""CUPS backend. No GUI code here, so it is easy to test.

Talks to CUPS through pycups. The connection is injectable, so tests can
pass a fake one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse


class PrinterState(IntEnum):
    IDLE = 3
    PROCESSING = 4
    STOPPED = 5


class JobState(IntEnum):
    PENDING = 3
    HELD = 4
    PROCESSING = 5
    STOPPED = 6
    CANCELED = 7
    ABORTED = 8
    COMPLETED = 9


# Sides values CUPS understands.
SIDES = {
    "one-sided": "One side",
    "two-sided-long-edge": "Both sides (flip long edge)",
    "two-sided-short-edge": "Both sides (flip short edge)",
}

PAPER_SIZES = ("A4", "A3", "A5", "Letter", "Legal")

ORIENTATION = {"portrait": "3", "landscape": "4"}

_JOB_ATTRS = [
    "job-id",
    "job-name",
    "job-originating-user-name",
    "job-printer-uri",
    "job-state",
    "job-k-octets",
    "time-at-creation",
]


class PrintoError(Exception):
    """Any CUPS failure, with a message fit to show the user."""


@dataclass(frozen=True)
class Printer:
    name: str
    info: str
    state: PrinterState
    state_message: str
    is_default: bool
    uri: str

    @property
    def label(self) -> str:
        return self.info or self.name

    @property
    def is_ready(self) -> bool:
        return self.state != PrinterState.STOPPED


@dataclass(frozen=True)
class Job:
    id: int
    title: str
    printer: str
    user: str
    state: JobState
    size_kb: int
    created: int  # unix time

    @property
    def is_active(self) -> bool:
        return self.state in (
            JobState.PENDING,
            JobState.HELD,
            JobState.PROCESSING,
            JobState.STOPPED,
        )

    @property
    def is_held(self) -> bool:
        return self.state == JobState.HELD


def build_options(
    copies: int = 1,
    sides: str = "one-sided",
    page_ranges: str = "",
    paper: str = "",
    orientation: str = "",
) -> dict[str, str]:
    """Turn friendly values into a CUPS option dict.

    Raises ValueError on bad input, so the GUI can show a message.
    """
    if copies < 1 or copies > 999:
        raise ValueError("Copies must be between 1 and 999")
    if sides not in SIDES:
        raise ValueError(f"Unknown sides value: {sides}")

    opts = {"copies": str(copies), "sides": sides}

    ranges = page_ranges.replace(" ", "")
    if ranges:
        _check_page_ranges(ranges)
        opts["page-ranges"] = ranges
    if paper:
        opts["media"] = paper
    if orientation:
        if orientation not in ORIENTATION:
            raise ValueError(f"Unknown orientation: {orientation}")
        opts["orientation-requested"] = ORIENTATION[orientation]
    return opts


def _check_page_ranges(text: str) -> None:
    """Accept things like '1-3,5,7-9'."""
    for part in text.split(","):
        bits = part.split("-")
        if len(bits) > 2 or not all(b.isdigit() and int(b) > 0 for b in bits):
            raise ValueError(f"Bad page range: '{part}'")
        if len(bits) == 2 and int(bits[0]) > int(bits[1]):
            raise ValueError(f"Bad page range: '{part}' (start is after end)")


def _printer_from_uri(uri: str) -> str:
    return Path(urlparse(uri).path).name


class Backend:
    """Thin wrapper over a pycups Connection."""

    def __init__(self, conn: Optional[Any] = None) -> None:
        self._conn = conn
        # pycups connections are not thread-safe; the GUI calls us from
        # worker threads, so every call goes through this lock.
        self._lock = threading.RLock()

    @property
    def conn(self) -> Any:
        if self._conn is None:
            try:
                import cups
            except ImportError as exc:  # pragma: no cover
                raise PrintoError(
                    "python-cups is missing. Install pycups."
                ) from exc
            try:
                self._conn = cups.Connection()
            except RuntimeError as exc:
                raise PrintoError(
                    "Cannot reach CUPS. Is the cups service running?"
                ) from exc
        return self._conn

    def _call(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            try:
                return getattr(self.conn, fn_name)(*args, **kwargs)
            except PrintoError:
                raise
            except Exception as exc:  # pycups raises cups.IPPError, RuntimeError
                self._conn = None  # drop it, try fresh next time
                raise PrintoError(f"CUPS error: {exc}") from exc

    # --- printers -------------------------------------------------------

    def printers(self) -> list[Printer]:
        raw: Mapping[str, Mapping[str, Any]] = self._call("getPrinters")
        default = self._call("getDefault")
        out = []
        for name, attrs in raw.items():
            out.append(
                Printer(
                    name=name,
                    info=attrs.get("printer-info", ""),
                    state=PrinterState(attrs.get("printer-state", 3)),
                    state_message=attrs.get("printer-state-message", ""),
                    is_default=(name == default),
                    uri=attrs.get("device-uri", ""),
                )
            )
        # default first, then by name
        out.sort(key=lambda p: (not p.is_default, p.label.lower()))
        return out

    # --- jobs -----------------------------------------------------------

    def jobs(self, which: str = "not-completed") -> list[Job]:
        # CUPS hides job names/users of other people's jobs, and only
        # reveals our own when asked with my_jobs=True. So ask twice:
        # everyone's jobs (limited info), then ours (full info), and merge.
        everyone = self._call(
            "getJobs",
            which_jobs=which,
            my_jobs=False,
            requested_attributes=_JOB_ATTRS,
        )
        mine = self._call(
            "getJobs",
            which_jobs=which,
            my_jobs=True,
            requested_attributes=_JOB_ATTRS,
        )
        raw = {jid: {**attrs, **mine.get(jid, {})} for jid, attrs in everyone.items()}
        out = []
        for job_id, attrs in raw.items():
            out.append(
                Job(
                    id=int(job_id),
                    title=attrs.get("job-name", "(untitled)"),
                    printer=_printer_from_uri(attrs.get("job-printer-uri", "")),
                    user=attrs.get("job-originating-user-name", ""),
                    state=JobState(attrs.get("job-state", 3)),
                    size_kb=int(attrs.get("job-k-octets", 0)),
                    created=int(attrs.get("time-at-creation", 0)),
                )
            )
        out.sort(key=lambda j: j.id)
        return out

    def print_file(
        self,
        printer: str,
        path: str | Path,
        options: Optional[Mapping[str, str]] = None,
    ) -> int:
        p = Path(path)
        if not p.is_file():
            raise PrintoError(f"File not found: {p}")
        return int(self._call("printFile", printer, str(p), p.name, dict(options or {})))

    def cancel(self, job_id: int) -> None:
        self._call("cancelJob", job_id)

    def hold(self, job_id: int) -> None:
        self._call("setJobHoldUntil", job_id, "indefinite")

    def release(self, job_id: int) -> None:
        self._call("setJobHoldUntil", job_id, "no-hold")
