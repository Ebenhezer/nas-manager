"""The only place that runs ffprobe/ffmpeg (Phase 9).

Hardening:

* Binaries come from ``NAS_FFMPEG_PATH`` / ``NAS_FFPROBE_PATH`` or ``PATH``
  lookup at the application's start-up environment; nothing a client sends
  ever becomes part of a command line except a file path the media services
  already resolved inside a library jail.
* Every argument list is fixed and built here; there is never a shell.
  Inputs and outputs are passed as ``file:<absolute path>`` so a file name
  can never be read as an option or as another protocol (``http:``,
  ``concat:``...).
* The child gets a minimal environment, no stdin, a timeout, and is killed
  when the worker is asked to stop.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import Settings

logger = logging.getLogger(__name__)

_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}
# ffprobe has no -nostdin; both get stdin=/dev/null regardless.
_PROBE_COMMON = ["-hide_banner", "-v", "error"]
_COMMON = ["-nostdin", *_PROBE_COMMON]
_PROGRESS_INTERVAL_SECONDS = 2.0
_STDERR_TAIL = 2000


class FFmpegError(Exception):
    """ffprobe/ffmpeg failed or produced unusable output."""


class FFmpegStopped(FFmpegError):
    """The run was cancelled because the worker is stopping."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: str | None
    ffprobe: str | None

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None


@dataclass
class ProbeResult:
    format_name: str | None = None
    duration: float | None = None
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    rotation: int | None = None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None


def _resolve(configured: Path | None, name: str) -> str | None:
    if configured is not None:
        return str(configured) if configured.is_file() else None
    return shutil.which(name, path=_ENV["PATH"])


def tools(settings: Settings) -> Tools:
    return Tools(
        ffmpeg=_resolve(settings.ffmpeg_path, "ffmpeg"),
        ffprobe=_resolve(settings.ffprobe_path, "ffprobe"),
    )


def available(settings: Settings) -> bool:
    return tools(settings).available


@lru_cache(maxsize=4)
def _version_of(binary: str) -> str | None:
    try:
        result = _run([binary, "-version"], timeout=10)
    except FFmpegError:
        return None
    first = result.stdout.decode("utf-8", "replace").splitlines()[:1]
    return first[0][:120] if first else None


def version(settings: Settings) -> str | None:
    binary = tools(settings).ffmpeg
    return _version_of(binary) if binary else None


def _input(path: Path) -> str:
    absolute = path.resolve()
    return f"file:{absolute}"


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed argument list (no shell); raise ``FFmpegError`` on failure."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv built in this module
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=_ENV,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{Path(argv[0]).name} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise FFmpegError(f"could not run {Path(argv[0]).name}: {exc}") from exc
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip()[-_STDERR_TAIL:]
        raise FFmpegError(f"{Path(argv[0]).name} failed ({result.returncode}): {tail}")
    return result


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None  # drop NaN


def _rotation(stream: dict[str, Any]) -> int | None:
    candidates: list[Any] = [stream.get("tags", {}).get("rotate")]
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict):
            candidates.append(side.get("rotation"))
    for value in candidates:
        try:
            degrees = int(float(value))
        except (TypeError, ValueError):
            continue
        return degrees % 360
    return None


def parse_probe(data: dict[str, Any]) -> ProbeResult:
    """Turn ffprobe's JSON into a ``ProbeResult`` (pure; unit tested)."""
    if not isinstance(data, dict):
        raise FFmpegError("unexpected ffprobe output")
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    assert isinstance(fmt, dict)  # noqa: S101 - narrowed above
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    result = ProbeResult(
        format_name=str(fmt["format_name"])[:64] if fmt.get("format_name") else None,
        duration=_number(fmt.get("duration")),
    )
    bitrate = _number(fmt.get("bit_rate"))
    result.bitrate = int(bitrate) if bitrate is not None else None
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is not None:
        result.video_codec = str(video.get("codec_name") or "unknown")[:32]
        width, height = video.get("width"), video.get("height")
        result.width = int(width) if isinstance(width, int) and width > 0 else None
        result.height = int(height) if isinstance(height, int) and height > 0 else None
        result.rotation = _rotation(video)
        if result.duration is None:
            result.duration = _number(video.get("duration"))
    if audio is not None:
        result.audio_codec = str(audio.get("codec_name") or "unknown")[:32]
    return result


def probe(settings: Settings, path: Path) -> ProbeResult:
    binary = tools(settings).ffprobe
    if binary is None:
        raise FFmpegError("ffprobe is not installed")
    result = _run(
        [
            binary,
            *_PROBE_COMMON,
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            _input(path),
        ],
        timeout=settings.media_probe_timeout_seconds,
    )
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise FFmpegError("ffprobe returned malformed JSON") from exc
    return parse_probe(data)


def extract_poster(settings: Settings, path: Path, *, at_seconds: float, output: Path) -> None:
    """Write one frame of ``path`` (at ``at_seconds``) to ``output`` as JPEG."""
    binary = tools(settings).ffmpeg
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    _run(
        [
            binary,
            *_COMMON,
            "-y",
            "-ss",
            f"{max(at_seconds, 0.0):.3f}",
            "-i",
            _input(path),
            "-frames:v",
            "1",
            "-an",
            "-f",
            "image2",
            "-c:v",
            "mjpeg",
            f"file:{output.resolve()}",
        ],
        timeout=settings.media_probe_timeout_seconds,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise FFmpegError("ffmpeg produced no poster frame")


def transcode_argv(settings: Settings, binary: str, source: Path, output: Path) -> list[str]:
    height = settings.media_transcode_max_height
    return [
        binary,
        *_COMMON,
        "-y",
        "-i",
        _input(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        settings.media_transcode_preset,
        "-crf",
        str(settings.media_transcode_crf),
        "-pix_fmt",
        "yuv420p",
        "-vf",
        f"scale=w=-2:h='trunc(min(ih,{height})/2)*2'",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-threads",
        str(settings.media_transcode_threads),
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "mp4",
        f"file:{output.resolve()}",
    ]


def parse_progress(line: str) -> float | None:
    """Seconds of output written, from one ``-progress`` line (or None)."""
    key, _, value = line.strip().partition("=")
    if key in {"out_time_us", "out_time_ms"}:  # both are microseconds
        number = _number(value)
        return number / 1_000_000 if number is not None else None
    return None


def transcode(
    settings: Settings,
    source: Path,
    output: Path,
    *,
    duration: float | None,
    on_progress: Callable[[float], None],
    stop: threading.Event | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> None:
    """Transcode ``source`` to an MP4 at ``output``; kill ffmpeg on stop/timeout.

    ``on_progress`` receives a percentage (0-100) every couple of seconds and
    is always called from the calling thread.
    """
    binary = tools(settings).ffmpeg
    if binary is None:
        raise FFmpegError("ffmpeg is not installed")
    argv = transcode_argv(settings, binary, source, output)
    latest: list[float] = [0.0]

    with tempfile.TemporaryFile() as stderr:
        try:
            process = popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, env=_ENV
            )
        except OSError as exc:
            raise FFmpegError(f"could not run ffmpeg: {exc}") from exc

        def read_progress() -> None:
            assert process.stdout is not None  # noqa: S101 - stdout=PIPE above
            for raw in process.stdout:
                seconds = parse_progress(raw.decode("utf-8", "replace"))
                if seconds is not None:
                    latest[0] = seconds

        reader = threading.Thread(target=read_progress, name="ffmpeg-progress", daemon=True)
        reader.start()
        deadline = time.monotonic() + settings.media_transcode_timeout_seconds
        reported = -1.0
        reason: str | None = None
        try:
            while process.poll() is None:
                if stop is not None and stop.is_set():
                    reason = "stopped"
                elif time.monotonic() > deadline:
                    reason = "timeout"
                if reason is not None:
                    process.kill()
                    break
                if duration:
                    percent = min(99.0, latest[0] * 100 / duration)
                    if percent - reported >= 1.0:
                        reported = percent
                        on_progress(round(percent, 1))
                if stop is not None:
                    stop.wait(_PROGRESS_INTERVAL_SECONDS / 4)
                else:
                    time.sleep(_PROGRESS_INTERVAL_SECONDS / 4)
        finally:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
            reader.join(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        if reason == "stopped":
            raise FFmpegStopped("transcoding was interrupted because the worker is stopping")
        if reason == "timeout":
            raise FFmpegError(
                f"transcoding exceeded {settings.media_transcode_timeout_seconds}s and was killed"
            )
        if process.returncode != 0:
            stderr.seek(0)
            tail = stderr.read().decode("utf-8", "replace").strip()[-_STDERR_TAIL:]
            raise FFmpegError(f"ffmpeg failed ({process.returncode}): {tail}")
    if not output.is_file() or output.stat().st_size == 0:
        raise FFmpegError("ffmpeg produced no output")
    on_progress(100.0)
