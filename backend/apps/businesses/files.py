"""Private document storage and short-lived signed download links."""

import io
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, storages
from django.utils.translation import gettext_lazy as _
from PIL import Image, UnidentifiedImageError

from apps.core.errors import AppError

_SALT = "primp.private-file"
JPEG = b"\xff\xd8\xff"
PNG = b"\x89PNG\r\n\x1a\n"
PDF = b"%PDF-"
KINDS = {"jpg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}


@dataclass(frozen=True)
class SniffedFile:
    ext: str
    content_type: str


def private_storage() -> Storage:
    return storages["private"]


def sniff(data: bytes) -> SniffedFile:
    """Decide the real file type from its content (never from the name or declared type)."""
    if not data:
        raise _invalid()
    if data.startswith(JPEG):
        ext = "jpg"
    elif data.startswith(PNG):
        ext = "png"
    elif data.startswith(PDF):
        ext = "pdf"
    else:
        raise _invalid()
    if ext == "pdf":
        if b"%%EOF" not in data[-2048:]:
            raise _invalid()
    else:
        try:
            Image.open(io.BytesIO(data)).verify()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise _invalid() from exc
    return SniffedFile(ext, KINDS[ext])


def _invalid() -> AppError:
    return AppError("invalid_document", _("Upload a valid JPG, PNG or PDF file."), 422)


def validate_document(data: bytes) -> SniffedFile:
    if len(data) > settings.DOCUMENT_MAX_BYTES:
        raise AppError("file_too_large", _("The file is larger than 10 MB."), 413)
    return sniff(data)


def save_private(prefix: str, data: bytes, ext: str) -> str:
    return str(private_storage().save(f"{prefix}/{uuid.uuid4().hex}.{ext}", ContentFile(data)))


def delete_private(path: str) -> None:
    private_storage().delete(path)


def make_token(document_id: int) -> str:
    return str(signing.dumps({"d": document_id}, salt=_SALT))


def read_token(token: str) -> int | None:
    try:
        data = signing.loads(token, salt=_SALT, max_age=settings.PRIVATE_URL_TTL_SECONDS)
    except signing.BadSignature:
        return None
    return int(data["d"])


def signed_url(document_id: int) -> str:
    """Relative URL valid for PRIVATE_URL_TTL_SECONDS. Only issue it to the owner or an admin."""
    return f"/api/v1/files/documents/{make_token(document_id)}"
