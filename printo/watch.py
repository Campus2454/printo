"""Work out what changed between two polls of CUPS. No GUI code.

JobWatcher says which jobs finished or failed. PrinterWatcher says which
printers just stopped. The GUI turns these into notifications.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .backend import Job, JobState, Printer, PrinterState


@dataclass(frozen=True)
class JobEvent:
    kind: str  # "done" | "failed"
    job: Job


class JobWatcher:
    def __init__(self) -> None:
        self._known: dict[int, Job] = {}

    def update(
        self,
        active: list[Job],
        final_state: Callable[[int], Optional[JobState]],
    ) -> list[JobEvent]:
        """Call with the current active jobs after every poll.

        final_state(job_id) is only called for a job that was active last
        time and is gone now.
        """
        events = []
        now = {j.id: j for j in active}
        for job_id, job in self._known.items():
            if job_id in now:
                continue
            state = final_state(job_id)
            if state == JobState.COMPLETED:
                events.append(JobEvent("done", job))
            elif state in (JobState.ABORTED,):
                events.append(JobEvent("failed", job))
            # CANCELED: the user did that, no need to tell them
        # keep the richest copy (own jobs carry names)
        self._known = {
            jid: (j if j.title != "(untitled)" else self._known.get(jid, j))
            for jid, j in now.items()
        }
        return events


class PrinterWatcher:
    def __init__(self) -> None:
        self._stopped: Optional[set[str]] = None  # None = not primed yet

    def update(self, printers: list[Printer]) -> list[Printer]:
        """Return printers that have just become stopped."""
        stopped = {p.name for p in printers if p.state == PrinterState.STOPPED}
        newly = []
        if self._stopped is not None:
            newly = [p for p in printers if p.name in stopped - self._stopped]
        self._stopped = stopped
        return newly

    @property
    def any_stopped(self) -> bool:
        return bool(self._stopped)
