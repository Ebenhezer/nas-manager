"""Image metadata and thumbnails (Pillow).

Pillow is only ever asked to decode files whose header-declared pixel count
is within ``NAS_MEDIA_MAX_IMAGE_PIXELS``: ``Image.open`` reads the header
only, and Pillow's own decompression-bomb check (a warning above the limit,
an error above twice the limit) is promoted to an error here so an oversized
or hostile file is rejected before any pixel data is decoded.

EXIF parsing is best effort: a corrupt or exotic EXIF block yields no
metadata rather than an error, because a photo without readable EXIF is
still a photo.
"""

from __future__ import annotations

import contextlib
import os
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

SIZE_SMALL = 320
SIZE_LARGE = 1280
THUMB_SIZES = {"small": SIZE_SMALL, "large": SIZE_LARGE}
_WEBP_QUALITY = 80

_TAG_ORIENTATION = 0x0112
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_DATETIME = 0x0132
_TAG_DATETIME_ORIGINAL = 0x9003
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON = 1, 2, 3, 4

# Treat "too many pixels" as an error everywhere, never as a mere warning.
warnings.simplefilter("error", Image.DecompressionBombWarning)


class ImageError(Exception):
    """The file could not be read as an image (corrupt, unsupported, too large)."""


@dataclass
class ImageMetadata:
    width: int | None = None
    height: int | None = None
    taken_at: datetime | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    orientation: int | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


def _open(path: Path, max_pixels: int) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        image = Image.open(path)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageError(f"image exceeds the {max_pixels} pixel limit") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ImageError(f"not a readable image: {exc}") from exc
    width, height = image.size
    if width * height > max_pixels:  # defence in depth; Pillow already checked
        image.close()
        raise ImageError(f"image exceeds the {max_pixels} pixel limit")
    return image


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if c.isprintable()).strip()
    return cleaned[:64] or None


def _exif_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _gps_coordinate(values: Any, ref: Any, limit: float) -> float | None:
    try:
        degrees, minutes, seconds = (float(v) for v in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    result = degrees + minutes / 60 + seconds / 3600
    if _text(ref) in {"S", "W"}:
        result = -result
    if not -limit <= result <= limit:
        return None
    return round(result, 7)


def _parse_exif(image: Image.Image, meta: ImageMetadata) -> None:
    exif = image.getexif()
    orientation = exif.get(_TAG_ORIENTATION)
    if isinstance(orientation, int) and 1 <= orientation <= 8:
        meta.orientation = orientation
    meta.camera_make = _text(exif.get(_TAG_MAKE))
    meta.camera_model = _text(exif.get(_TAG_MODEL))
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    meta.taken_at = _exif_datetime(sub.get(_TAG_DATETIME_ORIGINAL)) or _exif_datetime(
        exif.get(_TAG_DATETIME)
    )
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    if gps:
        lat = _gps_coordinate(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF), 90.0)
        lon = _gps_coordinate(gps.get(_GPS_LON), gps.get(_GPS_LON_REF), 180.0)
        if lat is not None and lon is not None:
            meta.gps_lat, meta.gps_lon = lat, lon


def read_metadata(path: Path, *, max_pixels: int) -> ImageMetadata:
    """Dimensions and EXIF of ``path``; raises ``ImageError`` if unreadable."""
    meta = ImageMetadata()
    with _open(path, max_pixels) as image:
        width, height = image.size
        # Corrupt EXIF must not hide the photo: keep whatever was parsed.
        with contextlib.suppress(Exception):
            _parse_exif(image, meta)
    # Orientations 5-8 rotate by 90 degrees: report the displayed size.
    if meta.orientation in {5, 6, 7, 8}:
        width, height = height, width
    meta.width, meta.height = width, height
    return meta


def _save_webp(image: Image.Image, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    temporary = target.parent / f".tmp-{uuid.uuid4().hex}.webp"
    try:
        image.save(temporary, format="WEBP", quality=_WEBP_QUALITY, method=4)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def make_thumbnails(source: Path, targets: dict[str, Path], *, max_pixels: int) -> None:
    """Write upright WebP thumbnails of ``source`` for each size in ``targets``.

    ``targets`` maps a size name from ``THUMB_SIZES`` to its output path.
    Each output is written to a temp file and renamed into place.
    """
    try:
        with _open(source, max_pixels) as opened:
            # JPEG can decode at a reduced scale, which saves memory and time.
            opened.draft("RGB", (SIZE_LARGE, SIZE_LARGE))
            upright = ImageOps.exif_transpose(opened)
            has_alpha = upright.mode in {"RGBA", "LA", "PA"} or (
                upright.mode == "P" and "transparency" in upright.info
            )
            base = upright.convert("RGBA" if has_alpha else "RGB")
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ImageError(f"could not decode image: {exc}") from exc
    for name in sorted(targets, key=lambda n: -THUMB_SIZES[n]):
        copy = base.copy()
        copy.thumbnail((THUMB_SIZES[name], THUMB_SIZES[name]))
        _save_webp(copy, targets[name])
