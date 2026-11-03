"""systemd unit state, read with unprivileged ``systemctl show``.

The unit list comes from configuration (``NAS_MONITOR_SERVICES``) and every
name is validated against ``UNIT_NAME_RE`` before it reaches the argument
list; ``--`` ends option parsing as a second guard. There is no shell, the
environment is minimal and the call has a timeout. Anything that prevents an
answer (no systemctl, no D-Bus, a timeout) is reported as ``unknown`` per
unit, never raised.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field

from app.core.config import UNIT_NAME_RE, Settings
from app.models.monitoring import CHECK_CRITICAL, CHECK_OK, CHECK_UNKNOWN, CHECK_WARNING

logger = logging.getLogger(__name__)

PROPERTIES = (
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "ActiveEnterTimestamp",
)
_TIMEOUT_SECONDS = 10


@dataclass
class UnitState:
    unit: str
    status: str
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    description: str | None = None
    active_since: str | None = None
    error: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if self.load_state == "not-found":
            return "unit not installed"
        return f"{self.active_state or '?'} ({self.sub_state or '?'})"

    @property
    def is_down(self) -> bool:
        """Installed but not running (``failed`` or ``inactive``)."""
        return self.status == CHECK_CRITICAL


def validate_unit(name: str) -> str:
    if not UNIT_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid systemd unit name: {name!r}")
    return name


def _systemctl(settings: Settings) -> str | None:
    if settings.systemctl_path is not None:
        return str(settings.systemctl_path)
    return shutil.which("systemctl", path="/usr/bin:/bin:/usr/sbin:/sbin")


def classify(props: dict[str, str]) -> str:
    load = props.get("LoadState", "")
    active = props.get("ActiveState", "")
    if load == "not-found":
        # A unit that is not installed (e.g. nmbd on a host without NetBIOS)
        # is reported, but is not an outage.
        return CHECK_UNKNOWN
    if active in {"active", "reloading"}:
        return CHECK_OK
    if active in {"activating", "deactivating"}:
        return CHECK_WARNING
    if active in {"failed", "inactive"}:
        return CHECK_CRITICAL
    return CHECK_UNKNOWN


def parse_show(stdout: str) -> list[dict[str, str]]:
    """Split ``systemctl show`` output (blank-line separated blocks)."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition("=")
        if sep:
            current[key.strip()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def _state(unit: str, props: dict[str, str]) -> UnitState:
    return UnitState(
        unit=unit,
        status=classify(props),
        load_state=props.get("LoadState") or None,
        active_state=props.get("ActiveState") or None,
        sub_state=props.get("SubState") or None,
        unit_file_state=props.get("UnitFileState") or None,
        description=props.get("Description") or None,
        active_since=props.get("ActiveEnterTimestamp") or None,
        raw=props,
    )


def _unknown(units: list[str], error: str) -> list[UnitState]:
    return [UnitState(unit=unit, status=CHECK_UNKNOWN, error=error) for unit in units]


def unit_states(settings: Settings, units: list[str] | None = None) -> list[UnitState]:
    names = [validate_unit(unit) for unit in (units or settings.monitor_service_list)]
    if not names:
        return []
    binary = _systemctl(settings)
    if binary is None:
        return _unknown(names, "systemctl is not available")
    argv = [binary, "show", "--no-pager", "-p", ",".join(PROPERTIES), "--", *names]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, validated unit names
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "SYSTEMD_PAGER": ""},
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return _unknown(names, "systemctl timed out")
    except OSError as exc:
        return _unknown(names, f"systemctl could not run: {exc.strerror or exc}")
    blocks = parse_show(result.stdout)
    if result.returncode != 0 and not blocks:
        message = (result.stderr.strip().splitlines() or ["systemctl failed"])[-1][:200]
        return _unknown(names, message)
    by_id = {block.get("Id", ""): block for block in blocks}
    states: list[UnitState] = []
    for index, unit in enumerate(names):
        # ``Id`` is the canonical name; fall back to output order for aliases.
        props = by_id.get(unit) or (blocks[index] if index < len(blocks) else None)
        if props is None:
            states.append(UnitState(unit=unit, status=CHECK_UNKNOWN, error="no answer"))
        else:
            states.append(_state(unit, props))
    return states
