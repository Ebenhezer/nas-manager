"""Privileged helper subprocess for NAS Manager (Phase 5).

The web process never runs as root. This package provides a small, tightly
restricted root-serving process that exposes a fixed allowlist of validated
operations over a Unix socket. It never executes commands supplied by
clients: every operation maps to a handler that builds its own ``argv``
from parameters that are strictly validated (device names, mount points,
filesystem types, option tokens) and runs them with ``shell=False`` and a
minimal environment.

Run it with ``python -m app.helper`` (see ``app/helper/__main__.py``).
"""

from __future__ import annotations

__version__ = "1"
