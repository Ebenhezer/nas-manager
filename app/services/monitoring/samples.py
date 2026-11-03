"""Metric history: the worker's periodic samples and downsampled reads."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models.monitoring import MetricSample
from app.schemas.monitoring import History, HistoryPoint
from app.schemas.system import SystemSummary
from app.services.monitoring.host import collect_system_summary, load_per_core

# Range name -> (span, bucket). Buckets keep every chart at <= ~360 points.
RANGES: dict[str, tuple[timedelta, timedelta]] = {
    "1h": (timedelta(hours=1), timedelta(minutes=1)),
    "6h": (timedelta(hours=6), timedelta(minutes=2)),
    "24h": (timedelta(hours=24), timedelta(minutes=5)),
    "7d": (timedelta(days=7), timedelta(minutes=30)),
}


def sample_paths(settings: Settings) -> list[Path]:
    """Filesystems worth watching: the data dir and every managed file root."""
    return [settings.data_dir, *settings.resolved_file_roots]


def record_sample(
    session: Session, settings: Settings, now: datetime | None = None
) -> tuple[MetricSample, SystemSummary]:
    summary = collect_system_summary(sample_paths(settings))
    row = MetricSample(
        sampled_at=now or utcnow_naive(),
        cpu_percent=summary.cpu.percent,
        load1=summary.cpu.load_average[0] if summary.cpu.load_average else None,
        load_per_core=load_per_core(summary.cpu.load_average, summary.cpu.logical_cores),
        memory_percent=summary.memory.percent,
        swap_percent=summary.memory.swap_percent,
        net_bytes_sent=summary.network.bytes_sent,
        net_bytes_recv=summary.network.bytes_received,
    )
    row.disks = [
        {"path": d.path, "percent": d.percent, "used": d.used, "total": d.total}
        for d in summary.disks
    ]
    session.add(row)
    session.commit()
    return row, summary


def latest_sample(session: Session) -> MetricSample | None:
    return session.execute(
        select(MetricSample).order_by(MetricSample.sampled_at.desc()).limit(1)
    ).scalar_one_or_none()


def prune(session: Session, settings: Settings, now: datetime | None = None) -> int:
    cutoff = (now or utcnow_naive()) - timedelta(days=settings.monitor_history_days)
    result = session.execute(delete(MetricSample).where(MetricSample.sampled_at < cutoff))
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def _rate(later: MetricSample, earlier: MetricSample, attr: str) -> float | None:
    seconds = (later.sampled_at - earlier.sampled_at).total_seconds()
    delta = getattr(later, attr) - getattr(earlier, attr)
    # Counters reset on reboot: a negative delta is not a rate.
    if seconds <= 0 or delta < 0:
        return None
    return float(delta) / seconds


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def history(
    session: Session, settings: Settings, range_name: str, now: datetime | None = None
) -> History:
    span, bucket = RANGES[range_name]
    end = now or utcnow_naive()
    rows = list(
        session.execute(
            select(MetricSample)
            .where(MetricSample.sampled_at >= end - span)
            .order_by(MetricSample.sampled_at)
        ).scalars()
    )
    buckets: dict[int, list[tuple[MetricSample, MetricSample | None]]] = {}
    previous: MetricSample | None = None
    for row in rows:
        index = int((row.sampled_at - (end - span)).total_seconds() // bucket.total_seconds())
        buckets.setdefault(index, []).append((row, previous))
        previous = row

    points: list[HistoryPoint] = []
    for index in sorted(buckets):
        group = buckets[index]
        samples = [row for row, _ in group]
        sent: list[float] = []
        recv: list[float] = []
        for row, prev in group:
            if prev is None:
                continue
            if (rate := _rate(row, prev, "net_bytes_sent")) is not None:
                sent.append(rate)
            if (rate := _rate(row, prev, "net_bytes_recv")) is not None:
                recv.append(rate)
        loads = [s.load_per_core for s in samples if s.load_per_core is not None]
        disk_max: dict[str, float] = {}
        for sample in samples:
            for disk in sample.disks:
                path, percent = str(disk.get("path")), disk.get("percent")
                if percent is not None:
                    disk_max[path] = max(disk_max.get(path, 0.0), float(percent))
        points.append(
            HistoryPoint(
                at=samples[-1].sampled_at,
                cpu_percent=_avg([s.cpu_percent for s in samples]),
                cpu_percent_max=max(s.cpu_percent for s in samples),
                memory_percent=_avg([s.memory_percent for s in samples]),
                swap_percent=_avg([s.swap_percent for s in samples]),
                load_per_core=_avg(loads) if loads else None,
                net_sent_bps=_avg(sent) if sent else None,
                net_recv_bps=_avg(recv) if recv else None,
                disks=disk_max,
            )
        )
    return History(
        range=range_name,
        bucket_seconds=int(bucket.total_seconds()),
        sample_seconds=settings.monitor_sample_seconds,
        points=points,
    )
