"""Print presets: saved option sets like "A4, both sides".

Stored as JSON in $XDG_CONFIG_HOME/printo/presets.json. No GUI code.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

from .backend import ORIENTATION, PAPER_SIZES, SIDES


@dataclass(frozen=True)
class Preset:
    name: str
    copies: int = 1
    sides: str = "one-sided"
    paper: str = ""
    orientation: str = ""
    printer: str = ""  # empty = keep whatever printer is selected


BUILTIN = (
    Preset("Default"),
    Preset("A4, both sides", sides="two-sided-long-edge", paper="A4"),
)


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "printo" / "presets.json"


def _clean(raw: object) -> Optional[Preset]:
    """Turn one JSON object into a Preset, or None if it is junk."""
    if not isinstance(raw, dict):
        return None
    known = {f.name for f in fields(Preset)}
    data = {k: v for k, v in raw.items() if k in known}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        copies = int(data.get("copies", 1))
    except (TypeError, ValueError):
        copies = 1
    sides = data.get("sides", "one-sided")
    paper = data.get("paper", "")
    orient = data.get("orientation", "")
    printer = data.get("printer", "")
    return Preset(
        name=name.strip(),
        copies=min(max(copies, 1), 999),
        sides=sides if sides in SIDES else "one-sided",
        paper=paper if paper in PAPER_SIZES else "",
        orientation=orient if orient in ORIENTATION else "",
        printer=printer if isinstance(printer, str) else "",
    )


class PresetStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_path()
        self._user: list[Preset] = self._load()

    def _load(self) -> list[Preset]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # missing or corrupt: start fresh, never crash the app
        if not isinstance(data, list):
            return []
        reserved = {p.name.lower() for p in BUILTIN}
        seen: set[str] = set()
        out = []
        for raw in data:
            p = _clean(raw)
            if p and p.name.lower() not in reserved | seen:
                seen.add(p.name.lower())
                out.append(p)
        return out

    def all(self) -> list[Preset]:
        return [*BUILTIN, *self._user]

    def is_builtin(self, name: str) -> bool:
        return any(p.name == name for p in BUILTIN)

    def save(self, preset: Preset) -> None:
        """Add a preset, or replace the user preset with the same name."""
        clean = _clean(asdict(preset))
        if clean is None:
            raise ValueError("Preset needs a name")
        if self.is_builtin(clean.name) or clean.name.lower() in {
            p.name.lower() for p in BUILTIN
        }:
            raise ValueError(f"'{clean.name}' is a built-in preset name")
        self._user = [p for p in self._user if p.name.lower() != clean.name.lower()]
        self._user.append(clean)
        self._write()

    def delete(self, name: str) -> None:
        if self.is_builtin(name):
            raise ValueError("Built-in presets cannot be deleted")
        self._user = [p for p in self._user if p.name != name]
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp file, then rename: never leaves a half-written file
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([asdict(p) for p in self._user], fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
