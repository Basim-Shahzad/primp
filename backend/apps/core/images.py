"""Safe image handling: validate real content, strip metadata, resize, re-encode."""

import io
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _
from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import AppError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000
ALLOWED = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png")}


@dataclass
class ProcessedImage:
    content: bytes
    ext: str
    content_type: str
    width: int
    height: int


def _invalid() -> AppError:
    return AppError("invalid_image", _("Upload a valid JPG or PNG image."), 422)


def process_image(
    data: bytes, *, max_dimension: int = 512, max_bytes: int = MAX_UPLOAD_BYTES
) -> ProcessedImage:
    if len(data) > max_bytes:
        raise AppError("file_too_large", _("The image is too large."), 413)
    if not data:
        raise _invalid()
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        probe.verify()
        if fmt not in ALLOWED:
            raise _invalid()
        loaded = Image.open(io.BytesIO(data))
        if loaded.width * loaded.height > MAX_PIXELS:
            raise _invalid()
        image = ImageOps.exif_transpose(loaded)  # honour orientation, then drop all metadata
        image.load()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise _invalid() from exc
    ext, content_type = ALLOWED[fmt]
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    clean = image.copy()
    clean.info.clear()  # drop EXIF, ICC, text chunks and any other embedded metadata
    out = io.BytesIO()
    if fmt == "JPEG":
        if clean.mode != "RGB":
            clean = clean.convert("RGB")
        clean.save(out, format="JPEG", quality=85, optimize=True)
    else:
        clean.save(out, format="PNG", optimize=True)
    return ProcessedImage(out.getvalue(), ext, content_type, clean.width, clean.height)
