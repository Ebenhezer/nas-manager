"""A stand-in for ffprobe/ffmpeg (Phase 9 tests).

``FakeFFmpeg.run`` replaces ``app.services.media.ffmpeg._run``: ffprobe calls
answer with canned JSON chosen by file name, poster extraction writes a real
JPEG. ``fake_popen`` replaces the transcoder's ``Popen`` with a small Python
child process that prints ``-progress`` lines and writes the output file,
so the real reader/kill/timeout logic is exercised.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image

H264_MP4: dict[str, Any] = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5", "bit_rate": "800000"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}
MPEG4_AVI: dict[str, Any] = {
    "format": {"format_name": "avi", "duration": "3.0"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "mpeg4",
            "width": 640,
            "height": 480,
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
        },
        {"codec_type": "audio", "codec_name": "mp3"},
    ],
}
AUDIO_ONLY: dict[str, Any] = {
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "60"},
    "streams": [
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (10, 200, 10)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeFFmpeg:
    def __init__(self, probes: dict[str, dict[str, Any]] | None = None) -> None:
        # Keyed by file suffix (".mp4") or full name ("clip.mp4").
        self.probes = probes or {".mp4": H264_MP4, ".avi": MPEG4_AVI, ".m4a.mp4": AUDIO_ONLY}
        self.calls: list[list[str]] = []
        self.fail_posters = False

    def _probe_for(self, path: Path) -> dict[str, Any]:
        for key in (path.name, "".join(path.suffixes[-2:]), path.suffix):
            if key in self.probes:
                return self.probes[key]
        return {"format": {}, "streams": []}

    def run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(argv))
        if argv[1:] == ["-version"]:
            return subprocess.CompletedProcess(argv, 0, b"ffmpeg version 0.0-fake\n", b"")
        target = Path(argv[-1].removeprefix("file:"))
        if Path(argv[0]).name.startswith("ffprobe"):
            body = json.dumps(self._probe_for(target)).encode()
            return subprocess.CompletedProcess(argv, 0, body, b"")
        if self.fail_posters:
            from app.services.media.ffmpeg import FFmpegError

            raise FFmpegError("ffmpeg failed (1): Invalid data found when processing input")
        target.write_bytes(_jpeg())
        return subprocess.CompletedProcess(argv, 0, b"", b"")


CHILD = r"""
import sys, time
out, duration, mode = sys.argv[1], float(sys.argv[2]), sys.argv[3]
for step in range(1, 4):
    print(f"out_time_us={int(step * duration / 3 * 1_000_000)}", flush=True)
    time.sleep(0.05)
if mode == "fail":
    sys.stderr.write("Error while decoding stream #0:0: Invalid data")
    sys.exit(1)
if mode == "hang":
    time.sleep(60)
if mode != "empty":
    with open(out, "wb") as handle:
        handle.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2000)
print("progress=end", flush=True)
"""


def fake_popen(mode: str = "ok", duration: float = 3.0) -> Any:
    """A ``Popen`` replacement running ``CHILD`` instead of ffmpeg."""
    seen: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        seen.append(list(argv))
        output = argv[-1].removeprefix("file:")
        return subprocess.Popen(  # noqa: S603 - test helper, fixed argv
            [sys.executable, "-c", CHILD, output, str(duration), mode], **kwargs
        )

    popen.calls = seen  # type: ignore[attr-defined]
    return popen
