"""CUPS backend. No GUI code here, so it is easy to test.

Talks to CUPS through pycups. The connection is injectable, so tests can
pass a fake one.
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class Device:
    """Something CUPS found that a printer could be added on."""

    uri: str
    info: str
    make_model: str
    device_class: str  # direct, network, serial, file

    @property
    def label(self) -> str:
        return self.make_model if self.make_model not in ("", "Unknown") else self.info or self.uri

    @property
    def scheme(self) -> str:
        return urlparse(self.uri).scheme


@dataclass(frozen=True)
class Driver:
    name: str  # ppd name, passed to CUPS
    make_model: str
    language: str


EVERYWHERE = "everywhere"  # driverless IPP Everywhere
_DRIVERLESS_SCHEMES = ("ipp", "ipps", "dnssd")
_PRINTER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,127}$")


def suggest_printer_name(text: str) -> str:
    """Make a valid CUPS queue name out of free text ('HP LaserJet 400')."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")
    return name[:127] or "printer"


def supports_driverless(device_uri: str) -> bool:
    return urlparse(device_uri).scheme in _DRIVERLESS_SCHEMES


def _text(value: Any) -> str:
    """pycups returns some attributes as a str, others as a list of str."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value) if value is not None else ""


def _texts(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)] if value else []


_AUTH_WORDS = ("forbidden", "not-authorized", "not-authenticated", "unauthorized")
_AUTH_CODES = {1282, 1284, 1285, 401, 403}


def _friendly(exc: Exception) -> str:
    text = str(exc)
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    if code in _AUTH_CODES or any(w in text.lower() for w in _AUTH_WORDS):
        return (
            "CUPS says you are not allowed to do this. Add your user to the "
            "'lpadmin' group (or 'sys'/'wheel' on some distros), then log in again."
        )
    return f"CUPS error: {text}"


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
            # Never let pycups ask for a password on the terminal: fail fast.
            cups.setPasswordCB2(lambda *_args: "")
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
                raise PrintoError(_friendly(exc)) from exc

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

    def job_final_state(self, job_id: int) -> Optional[JobState]:
        """State of a job that left the queue (done, canceled, failed)."""
        try:
            attrs = self._call(
                "getJobAttributes", job_id, requested_attributes=["job-state"]
            )
        except PrintoError:
            return None
        state = attrs.get("job-state")
        return JobState(state) if state is not None else None

    # --- managing printers (needs lpadmin rights) ------------------------

    def devices(self, timeout: int = 10) -> list[Device]:
        raw = self._call("getDevices", timeout=timeout)
        out = []
        for uri, attrs in raw.items():
            if _text(attrs.get("device-class")) == "file":
                continue
            if "://" not in uri:
                continue  # bare backend names like "socket" or "ipp", not real devices
            out.append(
                Device(
                    uri=uri,
                    info=_text(attrs.get("device-info", "")),
                    make_model=_text(attrs.get("device-make-and-model", "")),
                    device_class=_text(attrs.get("device-class", "")),
                )
            )
        # network printers first, they are what most people want
        out.sort(key=lambda d: (d.device_class != "network", d.label.lower()))
        return out

    def drivers(self, query: str, limit: int = 60) -> list[Driver]:
        """Find drivers (PPDs) whose make/model contains every word of query."""
        words = query.lower().split()
        if not words:
            return []
        raw = self._call("getPPDs2")
        out = []
        for name, attrs in raw.items():
            model = _text(attrs.get("ppd-make-and-model", ""))
            low = model.lower()
            if all(w in low for w in words):
                langs = _texts(attrs.get("ppd-natural-language")) or ["en"]
                out.append(Driver(name, model, ",".join(langs)))
        out.sort(key=lambda d: d.make_model.lower())
        return out[:limit]

    def add_printer(
        self,
        name: str,
        device_uri: str,
        driver: str = EVERYWHERE,
        info: str = "",
        location: str = "",
    ) -> None:
        if not _PRINTER_NAME.match(name):
            raise PrintoError(
                "Printer name may only use letters, numbers, - and _ (max 127)."
            )
        if not device_uri:
            raise PrintoError("Choose a device or enter an address first.")
        self._call(
            "addPrinter",
            name,
            ppdname=driver or EVERYWHERE,
            device=device_uri,
            info=info or name,
            location=location,
        )
        self._call("enablePrinter", name)
        self._call("acceptJobs", name)

    def delete_printer(self, name: str) -> None:
        self._call("deletePrinter", name)

    def set_default(self, name: str) -> None:
        self._call("setDefault", name)

    def print_test_page(self, name: str) -> int:
        return int(self._call("printTestPage", name))
