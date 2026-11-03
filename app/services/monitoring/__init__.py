"""System monitoring and alerting (Phase 1 host summary, Phase 10 the rest).

- ``host``: live CPU/memory/disk/network summary
- ``processes``: top processes (never command lines)
- ``services``: systemd unit state via unprivileged ``systemctl show``
- ``raid`` / ``smart``: array health from ``/proc/mdstat``, SMART via the helper
- ``checks`` / ``samples``: the worker's stored snapshots and history
- ``rules``: the pure alert evaluation engine
- ``alerts`` / ``channels`` / ``notify``: alert state, channels and delivery
"""

from app.services.monitoring.host import collect_system_summary, prime_cpu_counter

__all__ = ["collect_system_summary", "prime_cpu_counter"]
