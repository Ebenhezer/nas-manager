"""Phase 9: video metadata, playback classification, streaming and transcodes.

ffprobe/ffmpeg are replaced by ``tests/video_fakes.py``; the real binaries
are exercised separately in ``test_ffmpeg_live.py``.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import utcnow_naive
from app.models import AuditEvent, MediaItem, MediaTranscode, User
from app.models.media import (
    PLAYBACK_DIRECT,
    PLAYBACK_TRANSCODE,
    PLAYBACK_UNSUPPORTED,
    TRANSCODE_FAILED,
    TRANSCODE_QUEUED,
    TRANSCODE_READY,
    TRANSCODE_RUNNING,
)
from app.services.media import ffmpeg, transcode, video
from app.services.media.ffmpeg import ProbeResult, Tools

from .media_helpers import create_user, upload, user_client
from .video_fakes import AUDIO_ONLY, MPEG4_AVI, FakeFFmpeg, fake_popen

API = "/api/v1/media"
FAKE_TOOLS = Tools(ffmpeg="/opt/fake/ffmpeg", ffprobe="/opt/fake/ffprobe")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFFmpeg:
    instance = FakeFFmpeg()
    monkeypatch.setattr(ffmpeg, "tools", lambda settings: FAKE_TOOLS)
    monkeypatch.setattr(ffmpeg, "_run", instance.run)
    return instance


@pytest.fixture
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "tools", lambda settings: Tools(ffmpeg=None, ffprobe=None))


@pytest.fixture
def admin_client(app: FastAPI, admin_user: User) -> Iterator[TestClient]:
    with user_client(app, "admin") as client:
        yield client


def _use_popen(monkeypatch: pytest.MonkeyPatch, popen: Any) -> None:
    monkeypatch.setattr(ffmpeg, "transcode", functools.partial(ffmpeg.transcode, popen=popen))


def _run_transcodes(app: FastAPI, stop: threading.Event | None = None) -> list[str]:
    states = []
    with app.state.db.session_factory() as session:
        while (row := transcode.claim(session, owner="t", lease_seconds=60)) is not None:
            states.append(transcode.run(session, app.state.settings, row, stop=stop))
    return states


def _transcode_row(app: FastAPI, item_id: int) -> MediaTranscode:
    with app.state.db.session_factory() as session:
        row = transcode.get_row(session, item_id)
        assert row is not None
        session.expunge(row)
        return row


# ---------------------------------------------------------------------------
# ffprobe parsing and classification (pure)
# ---------------------------------------------------------------------------


def test_parse_probe_reads_streams_and_rotation() -> None:
    probe = ffmpeg.parse_probe(MPEG4_AVI)
    assert probe.format_name == "avi" and probe.duration == 3.0
    assert (probe.video_codec, probe.audio_codec) == ("mpeg4", "mp3")
    assert (probe.width, probe.height, probe.rotation) == (640, 480, 270)
    assert probe.has_video


def test_parse_probe_ignores_cover_art_and_bad_numbers() -> None:
    probe = ffmpeg.parse_probe(AUDIO_ONLY)
    assert not probe.has_video and probe.audio_codec == "aac"
    odd = ffmpeg.parse_probe(
        {
            "format": {"duration": "NaN", "bit_rate": "x"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": "wide",
                    "duration": "4.5",
                    "tags": {"rotate": "90"},
                }
            ],
        }
    )
    assert odd.duration == 4.5 and odd.bitrate is None and odd.width is None
    assert odd.video_codec == "unknown" and odd.rotation == 90
    assert ffmpeg.parse_probe({}) == ProbeResult()
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.parse_probe([])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "fmt", "vcodec", "acodec", "expected"),
    [
        ("a.mp4", "mov,mp4,m4a", "h264", "aac", PLAYBACK_DIRECT),
        ("a.MOV", "mov,mp4,m4a", "h264", None, PLAYBACK_DIRECT),
        ("a.m4v", "mov,mp4,m4a", "h264", "mp3", PLAYBACK_DIRECT),
        ("a.mp4", "mov,mp4,m4a", "hevc", "aac", PLAYBACK_TRANSCODE),
        ("a.mp4", "mov,mp4,m4a", "h264", "ac3", PLAYBACK_TRANSCODE),
        ("a.webm", "matroska,webm", "vp9", "opus", PLAYBACK_DIRECT),
        ("a.webm", "matroska,webm", "av1", None, PLAYBACK_DIRECT),
        ("a.webm", "matroska,webm", "vp9", "aac", PLAYBACK_TRANSCODE),
        ("a.mkv", "matroska,webm", "vp9", "opus", PLAYBACK_TRANSCODE),
        ("a.mkv", "matroska,webm", "h264", "aac", PLAYBACK_TRANSCODE),
        ("a.avi", "avi", "mpeg4", "mp3", PLAYBACK_TRANSCODE),
        ("a.mp4", "mov,mp4,m4a", None, "aac", PLAYBACK_UNSUPPORTED),
    ],
)
def test_classify_playback(
    name: str, fmt: str, vcodec: str | None, acodec: str | None, expected: str
) -> None:
    probe = ProbeResult(format_name=fmt, video_codec=vcodec, audio_codec=acodec)
    assert video.classify_playback(probe, name) == expected


def test_poster_offset() -> None:
    assert video.poster_offset(None) == 0.0
    assert video.poster_offset(4.0) == pytest.approx(0.4)
    assert video.poster_offset(600.0) == 1.0


def test_parse_progress() -> None:
    assert ffmpeg.parse_progress("out_time_us=2500000\n") == 2.5
    assert ffmpeg.parse_progress("out_time_ms=1000000") == 1.0
    assert ffmpeg.parse_progress("out_time_us=N/A") is None
    assert ffmpeg.parse_progress("progress=continue") is None


# ---------------------------------------------------------------------------
# Command construction and process handling
# ---------------------------------------------------------------------------


def test_commands_are_fixed_argv_with_file_protocol_inputs(
    settings: Settings, fake: FakeFFmpeg, tmp_path: Path
) -> None:
    hostile = tmp_path / "-i http:evil.mp4"
    hostile.write_bytes(b"x")
    ffmpeg.probe(settings, hostile)
    ffmpeg.extract_poster(settings, hostile, at_seconds=1.0, output=tmp_path / "p.jpg")
    probe_argv, poster_argv = fake.calls
    assert probe_argv[0] == "/opt/fake/ffprobe"
    assert probe_argv[-1] == f"file:{hostile.resolve()}"
    assert "-nostdin" not in probe_argv  # ffprobe does not know the option
    assert "-nostdin" in poster_argv
    assert poster_argv[poster_argv.index("-i") + 1] == f"file:{hostile.resolve()}"
    assert poster_argv[-1] == f"file:{(tmp_path / 'p.jpg').resolve()}"
    argv = ffmpeg.transcode_argv(settings, "/usr/bin/ffmpeg", hostile, tmp_path / "o.mp4")
    assert argv[argv.index("-i") + 1].startswith("file:/")
    assert argv[argv.index("-preset") + 1] == "veryfast"
    assert argv[argv.index("-crf") + 1] == "23"
    assert "min(ih,1080)" in argv[argv.index("-vf") + 1]
    assert argv[-1] == f"file:{(tmp_path / 'o.mp4').resolve()}"


def test_run_reports_failures_and_timeouts() -> None:
    with pytest.raises(ffmpeg.FFmpegError, match="failed \\(3\\): oops"):
        ffmpeg._run(
            [sys.executable, "-c", "import sys; sys.stderr.write('oops'); sys.exit(3)"], timeout=10
        )
    with pytest.raises(ffmpeg.FFmpegError, match="timed out"):
        ffmpeg._run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    with pytest.raises(ffmpeg.FFmpegError, match="could not run"):
        ffmpeg._run(["/nonexistent/ffmpeg"], timeout=1)


def test_probe_rejects_malformed_json(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ffmpeg, "tools", lambda s: FAKE_TOOLS)
    monkeypatch.setattr(
        ffmpeg,
        "_run",
        lambda argv, timeout: subprocess.CompletedProcess(argv, 0, b"{not json", b""),
    )
    with pytest.raises(ffmpeg.FFmpegError, match="malformed"):
        ffmpeg.probe(settings, tmp_path / "x.mp4")


def test_tools_resolution(settings: Settings, tmp_path: Path) -> None:
    binary = tmp_path / "my-ffmpeg"
    binary.write_text("")
    configured = settings.model_copy(
        update={"ffmpeg_path": binary, "ffprobe_path": tmp_path / "missing"}
    )
    found = ffmpeg.tools(configured)
    assert found.ffmpeg == str(binary) and found.ffprobe is None and not found.available


def test_transcode_reports_progress_and_writes_output(
    settings: Settings, fake: FakeFFmpeg, tmp_path: Path
) -> None:
    progress: list[float] = []
    out = tmp_path / "out.mp4"
    popen = fake_popen("ok", duration=3.0)
    ffmpeg.transcode(
        settings, tmp_path / "in.avi", out, duration=3.0, on_progress=progress.append, popen=popen
    )
    assert out.stat().st_size > 0
    assert progress[-1] == 100.0
    assert popen.calls[0][0] == "/opt/fake/ffmpeg"


@pytest.mark.parametrize(
    ("mode", "message"),
    [("fail", "Invalid data"), ("empty", "produced no output")],
)
def test_transcode_failures(
    settings: Settings, fake: FakeFFmpeg, tmp_path: Path, mode: str, message: str
) -> None:
    with pytest.raises(ffmpeg.FFmpegError, match=message):
        ffmpeg.transcode(
            settings,
            tmp_path / "in.avi",
            tmp_path / "out.mp4",
            duration=3.0,
            on_progress=lambda p: None,
            popen=fake_popen(mode),
        )


def test_transcode_is_killed_on_stop_and_timeout(
    settings: Settings, fake: FakeFFmpeg, tmp_path: Path
) -> None:
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    with pytest.raises(ffmpeg.FFmpegStopped):
        ffmpeg.transcode(
            settings,
            tmp_path / "in.avi",
            tmp_path / "out.mp4",
            duration=3.0,
            on_progress=lambda p: None,
            stop=stop,
            popen=fake_popen("hang"),
        )
    impatient = settings.model_copy(update={"media_transcode_timeout_seconds": 0})
    with pytest.raises(ffmpeg.FFmpegError, match="was killed"):
        ffmpeg.transcode(
            impatient,
            tmp_path / "in.avi",
            tmp_path / "out.mp4",
            duration=3.0,
            on_progress=lambda p: None,
            popen=fake_popen("hang"),
        )


# ---------------------------------------------------------------------------
# Indexing and posters
# ---------------------------------------------------------------------------


def test_direct_video_is_probed_postered_and_streamed(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    data = os.urandom(4096)
    item = upload(admin_client, "clip.mp4", data)
    assert item["kind"] == "video" and item["playback"] == PLAYBACK_DIRECT
    assert item["duration_seconds"] == 12.5 and item["video_codec"] == "h264"
    assert (item["width"], item["height"]) == (1920, 1080)
    assert item["thumb_state"] == "pending"

    thumb = admin_client.get(f"{API}/items/{item['id']}/thumbnail")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    poster_call = fake.calls[-1]
    assert poster_call[poster_call.index("-ss") + 1] == "1.000"

    stream = admin_client.get(f"{API}/items/{item['id']}/stream")
    assert stream.status_code == 200 and stream.content == data
    assert stream.headers["content-type"] == "video/mp4"
    assert stream.headers["accept-ranges"] == "bytes"
    tail = admin_client.get(f"{API}/items/{item['id']}/stream", headers={"Range": "bytes=4000-"})
    assert tail.status_code == 206 and tail.content == data[4000:]
    assert tail.headers["content-range"] == "bytes 4000-4095/4096"

    no_need = admin_client.post(f"{API}/items/{item['id']}/transcode")
    assert no_need.status_code == 409


def test_rotated_video_reports_display_dimensions(
    admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    item = upload(admin_client, "phone.avi", b"avi bytes")
    assert item["rotation"] == 270 and (item["width"], item["height"]) == (480, 640)
    assert item["playback"] == PLAYBACK_TRANSCODE


def test_poster_failure_marks_the_thumbnail_failed(
    admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    fake.fail_posters = True
    item = upload(admin_client, "clip.mp4", b"x")
    assert admin_client.get(f"{API}/items/{item['id']}/thumbnail").status_code == 404
    detail = admin_client.get(f"{API}/items/{item['id']}").json()
    assert detail["thumb_state"] == "failed" and "Invalid data" in detail["thumb_error"]


def test_audio_only_files_are_unsupported(admin_client: TestClient, fake: FakeFFmpeg) -> None:
    item = upload(admin_client, "song.m4a.mp4", b"x")
    assert item["playback"] == PLAYBACK_UNSUPPORTED and item["thumb_state"] == "unsupported"
    assert admin_client.post(f"{API}/items/{item['id']}/transcode").status_code == 400
    assert admin_client.get(f"{API}/items/{item['id']}/stream").status_code == 409


def test_without_ffmpeg_videos_are_indexed_and_reprobed_later(
    app: FastAPI, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffmpeg, "tools", lambda settings: Tools(ffmpeg=None, ffprobe=None))
    item = upload(admin_client, "old.avi", b"x")
    assert item["playback"] == "unknown" and item["thumb_state"] == "unsupported"
    assert item["duration_seconds"] is None
    refused = admin_client.post(f"{API}/items/{item['id']}/transcode")
    assert refused.status_code == 400  # not probed yet
    status = admin_client.get(f"{API}/status").json()
    assert status["ffmpeg_available"] is False and status["transcoding_available"] is False
    assert "video_transcoding" not in admin_client.get("/api/v1/info").json()["features"]

    # ffmpeg is installed: the next scan probes the unchanged file once.
    instance = FakeFFmpeg()
    monkeypatch.setattr(ffmpeg, "tools", lambda settings: FAKE_TOOLS)
    monkeypatch.setattr(ffmpeg, "_run", instance.run)
    library_id = item["library_id"]
    admin_client.post(f"{API}/libraries/{library_id}/scan")
    from app.services.media import jobs

    with app.state.db.session_factory() as session:
        claimed = jobs.claim_scan(session, owner="t", lease_seconds=60)
        assert claimed is not None
        result = jobs.run_scan(session, app.state.settings, claimed)
        assert result is not None and result.updated == 1
    detail = admin_client.get(f"{API}/items/{item['id']}").json()
    assert detail["playback"] == PLAYBACK_TRANSCODE and detail["duration_seconds"] == 3.0
    info = admin_client.get("/api/v1/info").json()["features"]
    assert "video_streaming" in info and "video_transcoding" in info


def test_transcoding_unavailable_is_503(
    admin_client: TestClient, fake: FakeFFmpeg, app: FastAPI
) -> None:
    item = upload(admin_client, "old.avi", b"x")
    app.state.settings.media_transcode_enabled = False
    try:
        refused = admin_client.post(f"{API}/items/{item['id']}/transcode")
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "transcoding_unavailable"
        blocked = admin_client.get(f"{API}/items/{item['id']}/stream")
        assert blocked.json()["error"]["details"]["transcoding_available"] is False
    finally:
        app.state.settings.media_transcode_enabled = True


# ---------------------------------------------------------------------------
# Transcode lifecycle
# ---------------------------------------------------------------------------


def test_transcode_lifecycle(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_popen(monkeypatch, fake_popen("ok"))
    item = upload(admin_client, "old.avi", b"avi bytes")
    item_id = item["id"]
    blocked = admin_client.get(f"{API}/items/{item_id}/stream")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "transcode_required"
    assert blocked.json()["error"]["details"]["state"] is None

    queued = admin_client.post(f"{API}/items/{item_id}/transcode")
    assert queued.status_code == 202 and queued.json()["state"] == TRANSCODE_QUEUED
    again = admin_client.post(f"{API}/items/{item_id}/transcode").json()
    assert again["state"] == TRANSCODE_QUEUED
    with app.state.db.session_factory() as session:
        assert len(session.execute(select(MediaTranscode)).scalars().all()) == 1

    assert _run_transcodes(app) == [TRANSCODE_READY]
    status = admin_client.get(f"{API}/items/{item_id}/transcode").json()
    assert status["state"] == TRANSCODE_READY and status["progress_percent"] == 100.0
    assert status["output_size"] and status["output_size"] > 2000

    stream = admin_client.get(f"{API}/items/{item_id}/stream")
    assert stream.status_code == 200 and stream.headers["content-type"] == "video/mp4"
    assert stream.content.startswith(b"\x00\x00\x00\x18ftypisom")
    partial = admin_client.get(f"{API}/items/{item_id}/stream", headers={"Range": "bytes=4-11"})
    assert partial.status_code == 206 and partial.content == b"ftypisom"
    assert _transcode_row(app, item_id).last_accessed_at is not None
    # Asking again while a current transcode exists changes nothing.
    assert admin_client.post(f"{API}/items/{item_id}/transcode").json()["state"] == TRANSCODE_READY

    # The source changes: the old rendition is never served for it.
    source = app.state.settings.resolved_media_dir / "admin" / item["rel_path"]
    source.write_bytes(b"new avi bytes!")
    os.utime(source, ns=(1, 5_000_000_000))
    library_id = item["library_id"]
    from app.services.media import jobs

    admin_client.post(f"{API}/libraries/{library_id}/scan")
    with app.state.db.session_factory() as session:
        claimed = jobs.claim_scan(session, owner="t", lease_seconds=60)
        assert claimed is not None
        jobs.run_scan(session, app.state.settings, claimed)
    stale = admin_client.get(f"{API}/items/{item_id}/stream")
    assert stale.status_code == 409 and stale.json()["error"]["details"]["state"] is None
    assert admin_client.get(f"{API}/items/{item_id}/transcode").json()["state"] is None
    assert admin_client.post(f"{API}/items/{item_id}/transcode").json()["state"] == "queued"
    assert _run_transcodes(app) == [TRANSCODE_READY]
    files = list(transcode.cache_dir(app.state.settings).glob("*/*.mp4"))
    assert len(files) == 1  # the stale rendition was removed
    with app.state.db.session_factory() as session:
        events = [
            e.event
            for e in session.execute(
                select(AuditEvent).where(AuditEvent.event.like("media.transcode%"))
            ).scalars()
        ]
    assert events == ["media.transcode_requested", "media.transcode_requested"]


def test_failed_transcode_is_recorded_and_can_be_retried(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_popen(monkeypatch, fake_popen("fail"))
    item = upload(admin_client, "old.avi", b"x")
    admin_client.post(f"{API}/items/{item['id']}/transcode")
    assert _run_transcodes(app) == [TRANSCODE_FAILED]
    status = admin_client.get(f"{API}/items/{item['id']}/transcode").json()
    assert status["state"] == TRANSCODE_FAILED and "Invalid data" in status["error"]
    assert not list(transcode.cache_dir(app.state.settings).glob("*/*"))  # no temp leftovers
    with app.state.db.session_factory() as session:
        failure = session.execute(
            select(AuditEvent).where(AuditEvent.event == "media.transcode_failed")
        ).scalar_one()
        assert failure.outcome == "failure"
    retried = admin_client.post(f"{API}/items/{item['id']}/transcode").json()
    assert retried["state"] == TRANSCODE_QUEUED and retried["error"] is None


def test_stopped_transcode_is_requeued(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_popen(monkeypatch, fake_popen("hang"))
    item = upload(admin_client, "old.avi", b"x")
    admin_client.post(f"{API}/items/{item['id']}/transcode")
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    with app.state.db.session_factory() as session:
        row = transcode.claim(session, owner="t", lease_seconds=60)
        assert row is not None
        assert transcode.run(session, app.state.settings, row, stop=stop) == TRANSCODE_QUEUED
    row = _transcode_row(app, item["id"])
    assert row.state == TRANSCODE_QUEUED and row.lease_owner is None


def test_expired_transcode_lease_is_requeued(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    item = upload(admin_client, "old.avi", b"x")
    admin_client.post(f"{API}/items/{item['id']}/transcode")
    with app.state.db.session_factory() as session:
        row = transcode.claim(session, owner="dead", lease_seconds=60)
        assert row is not None and row.state == TRANSCODE_RUNNING
        assert transcode.claim(session, owner="other", lease_seconds=60) is None
        transcode.renew_leases(session, owner="dead", lease_seconds=600)
        assert transcode.reclaim(session, utcnow_naive()) == 0
        row.lease_expires_at = utcnow_naive() - timedelta(seconds=1)
        session.commit()
        assert transcode.reclaim(session, utcnow_naive()) == 1
        session.refresh(row)
        assert row.state == TRANSCODE_QUEUED


def test_cache_is_evicted_least_recently_streamed_first(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_popen(monkeypatch, fake_popen("ok"))
    first = upload(admin_client, "one.avi", b"1")
    second = upload(admin_client, "two.avi", b"22")
    for item in (first, second):
        admin_client.post(f"{API}/items/{item['id']}/transcode")
    _run_transcodes(app)
    assert admin_client.get(f"{API}/items/{first['id']}/stream").status_code == 200
    monkeypatch.setattr(app.state.settings, "media_transcode_cache_max_bytes", 3000)
    with app.state.db.session_factory() as session:
        assert transcode.enforce_cache_limit(session, app.state.settings) == 1
    assert admin_client.get(f"{API}/items/{first['id']}/stream").status_code == 200
    assert admin_client.get(f"{API}/items/{second['id']}/stream").status_code == 409


def test_deleting_a_video_removes_its_transcode(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_popen(monkeypatch, fake_popen("ok"))
    item = upload(admin_client, "old.avi", b"x")
    admin_client.post(f"{API}/items/{item['id']}/transcode")
    _run_transcodes(app)
    assert list(transcode.cache_dir(app.state.settings).glob("*/*.mp4"))
    assert admin_client.delete(f"{API}/items/{item['id']}").status_code == 204
    assert not list(transcode.cache_dir(app.state.settings).glob("*/*.mp4"))
    with app.state.db.session_factory() as session:
        assert session.execute(select(MediaTranscode)).first() is None


def test_prune_removes_orphaned_transcode_files(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_popen(monkeypatch, fake_popen("ok"))
    item = upload(admin_client, "old.avi", b"x")
    admin_client.post(f"{API}/items/{item['id']}/transcode")
    _run_transcodes(app)
    folder = transcode.cache_dir(app.state.settings) / "ff"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "255-deadbeef.mp4").write_bytes(b"orphan")
    old_tmp = folder / ".tmp-old.mp4"
    old_tmp.write_bytes(b"x")
    os.utime(old_tmp, (1, 1))
    (folder / ".tmp-new.mp4").write_bytes(b"x")
    with app.state.db.session_factory() as session:
        assert transcode.prune_orphans(session, app.state.settings) == 2
    assert (folder / ".tmp-new.mp4").exists()
    assert admin_client.get(f"{API}/items/{item['id']}/stream").status_code == 200


def test_library_transcode_queues_every_video_that_needs_it(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    upload(admin_client, "one.avi", b"1")
    upload(admin_client, "two.avi", b"2")
    upload(admin_client, "fine.mp4", b"3")
    library_id = admin_client.get(f"{API}/libraries").json()[0]["id"]
    result = admin_client.post(f"{API}/libraries/{library_id}/transcode")
    assert result.status_code == 202 and result.json() == {"queued": 2}
    assert admin_client.post(f"{API}/libraries/{library_id}/transcode").json() == {"queued": 0}
    status = admin_client.get(f"{API}/status").json()
    assert status["queued_transcodes"] == 2 and status["transcoding_available"] is True


def test_worker_runs_transcodes_on_its_own_pool(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.worker.runner import Worker

    _use_popen(monkeypatch, fake_popen("ok"))
    item = upload(admin_client, "old.avi", b"x")
    admin_client.post(f"{API}/items/{item['id']}/transcode")
    worker = Worker(app.state.settings, app.state.db, owner="w")
    try:
        worker.tick()
        worker.wait_idle()
    finally:
        worker.pool.shutdown()
        worker.media_pool.shutdown()
        worker.transcode_pool.shutdown()
    assert _transcode_row(app, item["id"]).state == TRANSCODE_READY


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_other_users_cannot_stream_or_transcode(
    app: FastAPI, admin_user: User, admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    clip = upload(admin_client, "clip.mp4", b"x")
    old = upload(admin_client, "old.avi", b"y")
    image = upload(admin_client, "p.png", b"z")
    create_user(app, admin_user, "carol")
    with user_client(app, "carol") as carol:
        for item in (clip, old):
            assert carol.get(f"{API}/items/{item['id']}/stream").status_code == 404
            assert carol.get(f"{API}/items/{item['id']}/transcode").status_code == 404
            assert carol.post(f"{API}/items/{item['id']}/transcode").status_code == 404
        library_id = clip["library_id"]
        assert carol.post(f"{API}/libraries/{library_id}/transcode").status_code == 403
    assert admin_client.get(f"{API}/items/{image['id']}/stream").status_code == 400
    assert admin_client.post(f"{API}/items/{image['id']}/transcode").status_code == 400


def test_videos_page_renders(admin_client: TestClient) -> None:
    page = admin_client.get("/videos")
    assert page.status_code == 200
    assert 'data-default-kind="video"' in page.text
    assert 'href="/videos"' in page.text


def test_media_item_rows_expose_playback(
    app: FastAPI, admin_client: TestClient, fake: FakeFFmpeg
) -> None:
    upload(admin_client, "clip.mp4", b"x")
    with app.state.db.session_factory() as session:
        row = session.execute(select(MediaItem)).scalar_one()
        assert row.playback == PLAYBACK_DIRECT and row.container == "mov,mp4,m4a,3gp,3g2,mj2"
