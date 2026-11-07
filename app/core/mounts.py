"""Reading the kernel's mount table (``/proc/self/mounts``).

Shared by the root helper (which mounts USB drives) and the web process
(which lists them for the Files page). Fields are space separated, and the
kernel escapes spaces, tabs, newlines and backslashes in paths as octal
(``\\040`` and so on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROC_MOUNTS = Path("/proc/self/mounts")

_OCTAL = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class MountEntry:
    source: str
    target: str
    fstype: str
    options: tuple[str, ...]

    @property
    def read_only(self) -> bool:
        return "ro" in self.options


def _unescape(value: str) -> str:
    return _OCTAL.sub(lambda match: chr(int(match.group(1), 8)), value)


def parse_mounts(text: str) -> list[MountEntry]:
    entries: list[MountEntry] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        entries.append(
            MountEntry(
                source=_unescape(fields[0]),
                target=_unescape(fields[1]),
                fstype=fields[2],
                options=tuple(fields[3].split(",")),
            )
        )
    return entries


def read_mounts(path: Path = PROC_MOUNTS) -> list[MountEntry]:
    try:
        return parse_mounts(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def mounts_under(base: Path, entries: list[MountEntry]) -> list[MountEntry]:
    """Mounts whose target is directly inside ``base`` (one level deep).

    When a target was mounted more than once only the last (visible) entry
    is kept.
    """
    found: dict[str, MountEntry] = {}
    for entry in entries:
        target = Path(entry.target)
        if target.parent == base:
            found[entry.target] = entry
    return list(found.values())
