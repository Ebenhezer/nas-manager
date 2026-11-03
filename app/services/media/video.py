"""Video metadata and playback classification (Phase 9).

Browsers reliably play H.264 (+AAC/MP3) in MP4/MOV and VP8/VP9/AV1
(+Opus/Vorbis) in WebM. Those files are streamed as they are (HTTP Range,
so seeking works); every other video with a video stream needs a transcode
first, and a file without a video stream cannot be played at all.
"""

from __future__ import annotations

from pathlib import Path

from app.models import MediaItem
from app.models.media import PLAYBACK_DIRECT, PLAYBACK_TRANSCODE, PLAYBACK_UNSUPPORTED
from app.services.media.ffmpeg import ProbeResult

_MP4_EXTENSIONS = {".mp4", ".m4v", ".mov"}
_MP4_VIDEO = {"h264"}
_MP4_AUDIO = {"aac", "mp3"}
_WEBM_VIDEO = {"vp8", "vp9", "av1"}
_WEBM_AUDIO = {"opus", "vorbis"}


def _formats(probe: ProbeResult) -> set[str]:
    return set((probe.format_name or "").split(","))


def classify_playback(probe: ProbeResult, filename: str) -> str:
    if not probe.has_video:
        return PLAYBACK_UNSUPPORTED
    suffix = Path(filename).suffix.lower()
    formats = _formats(probe)
    audio_ok_mp4 = probe.audio_codec is None or probe.audio_codec in _MP4_AUDIO
    if (
        suffix in _MP4_EXTENSIONS
        and formats & {"mp4", "mov"}
        and probe.video_codec in _MP4_VIDEO
        and audio_ok_mp4
    ):
        return PLAYBACK_DIRECT
    audio_ok_webm = probe.audio_codec is None or probe.audio_codec in _WEBM_AUDIO
    if (
        suffix == ".webm"
        and "webm" in formats
        and probe.video_codec in _WEBM_VIDEO
        and audio_ok_webm
    ):
        return PLAYBACK_DIRECT
    return PLAYBACK_TRANSCODE


def stream_media_type(item: MediaItem) -> str:
    """The type a directly playable original is streamed as."""
    return "video/webm" if Path(item.name).suffix.lower() == ".webm" else "video/mp4"


def poster_offset(duration: float | None) -> float:
    """Where to grab the poster frame: 10 % in, at most one second."""
    if not duration:
        return 0.0
    return min(1.0, duration / 10)


def apply_probe(item: MediaItem, probe: ProbeResult) -> None:
    item.duration_seconds = probe.duration
    item.video_codec = probe.video_codec
    item.audio_codec = probe.audio_codec
    item.container = probe.format_name
    item.bitrate = probe.bitrate
    item.rotation = probe.rotation
    if probe.width and probe.height:
        width, height = probe.width, probe.height
        if probe.rotation in {90, 270}:
            width, height = height, width
        item.width, item.height = width, height
    item.playback = classify_playback(probe, item.name)
