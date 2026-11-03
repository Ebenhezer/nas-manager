"""Phase 9 against the real ffprobe/ffmpeg (skipped when they are not installed).

Generates two short synthetic clips with ffmpeg's ``testsrc`` source: an
H.264/AAC MP4 (plays directly) and an MPEG-4 Part 2 Matroska file (needs a
transcode), then runs the real probe, poster and transcode paths end to end.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models import User
from app.models.media import PLAYBACK_DIRECT, PLAYBACK_TRANSCODE, TRANSCODE_READY
from app.services.media import ffmpeg, transcode, video

from .media_helpers import user_client

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed",
)

API = "/api/v1/media"


def _make_clip(path: Path, *codec_args: str, audio: bool = True) -> Path:
    binary = shutil.which("ffmpeg")
    assert binary is not None
    argv = [
        binary,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=320x240:rate=25",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest"]
    subprocess.run([*argv, *codec_args, str(path)], check=True, timeout=120)  # noqa: S603
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("clips")
    return {
        "mp4": _make_clip(
            folder / "direct.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"
        ),
        "mkv": _make_clip(folder / "legacy.mkv", "-c:v", "mpeg4", audio=False),
    }


@pytest.fixture
def admin_client(app: FastAPI, admin_user: User) -> Iterator[TestClient]:
    with user_client(app, "admin") as client:
        yield client


def test_real_probe_and_classification(settings: Settings, clips: dict[str, Path]) -> None:
    direct = ffmpeg.probe(settings, clips["mp4"])
    assert direct.video_codec == "h264" and direct.audio_codec == "aac"
    assert (direct.width, direct.height) == (320, 240)
    assert direct.duration == pytest.approx(2.0, abs=0.2)
    assert video.classify_playback(direct, "direct.mp4") == PLAYBACK_DIRECT
    legacy = ffmpeg.probe(settings, clips["mkv"])
    assert legacy.video_codec == "mpeg4" and legacy.audio_codec is None
    assert video.classify_playback(legacy, "legacy.mkv") == PLAYBACK_TRANSCODE
    assert ffmpeg.version(settings) is not None


def test_real_poster_and_transcode(
    settings: Settings, clips: dict[str, Path], tmp_path: Path
) -> None:
    poster = tmp_path / "poster.jpg"
    ffmpeg.extract_poster(settings, clips["mkv"], at_seconds=0.2, output=poster)
    assert poster.read_bytes()[:2] == b"\xff\xd8"  # JPEG
    progress: list[float] = []
    output = tmp_path / "out.mp4"
    ffmpeg.transcode(settings, clips["mkv"], output, duration=2.0, on_progress=progress.append)
    result = ffmpeg.probe(settings, output)
    assert result.video_codec == "h264"
    assert video.classify_playback(result, "out.mp4") == PLAYBACK_DIRECT
    assert progress[-1] == 100.0


def test_real_corrupt_input_fails_cleanly(settings: Settings, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypisom not really a video")
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.probe(settings, broken)


def test_end_to_end_upload_transcode_and_stream(
    app: FastAPI, admin_client: TestClient, clips: dict[str, Path]
) -> None:
    def send(path: Path) -> dict[str, object]:
        with path.open("rb") as handle:
            response = admin_client.post(
                f"{API}/upload", files={"file": (path.name, handle, "application/octet-stream")}
            )
        assert response.status_code == 201, response.text
        body: dict[str, object] = response.json()
        return body

    direct = send(clips["mp4"])
    assert direct["playback"] == PLAYBACK_DIRECT
    thumb = admin_client.get(f"{API}/items/{direct['id']}/thumbnail")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    ranged = admin_client.get(f"{API}/items/{direct['id']}/stream", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100

    legacy = send(clips["mkv"])
    assert legacy["playback"] == PLAYBACK_TRANSCODE
    assert admin_client.get(f"{API}/items/{legacy['id']}/stream").status_code == 409
    assert admin_client.post(f"{API}/items/{legacy['id']}/transcode").status_code == 202
    with app.state.db.session_factory() as session:
        row = transcode.claim(session, owner="live", lease_seconds=60)
        assert row is not None
        assert transcode.run(session, app.state.settings, row) == TRANSCODE_READY
    streamed = admin_client.get(f"{API}/items/{legacy['id']}/stream")
    assert streamed.status_code == 200 and streamed.headers["content-type"] == "video/mp4"
    assert b"ftyp" in streamed.content[:16]
    seek = admin_client.get(
        f"{API}/items/{legacy['id']}/stream", headers={"Range": "bytes=100-199"}
    )
    assert seek.status_code == 206 and seek.content == streamed.content[100:200]
