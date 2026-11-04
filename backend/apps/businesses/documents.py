"""Verification documents: upload, list and short-lived signed access (private storage)."""

from functools import partial

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError, conflict, not_found, validation_error

from . import files, selectors
from .models import BusinessDocument, DocumentKind, DocumentStatus
from .onboarding import EDITABLE

MAX_DOCUMENTS = 15


def _drop(document: BusinessDocument) -> None:
    path = document.file
    document.delete()
    transaction.on_commit(partial(files.delete_private, path))


def upload(owner_id: int, business_ref: str, kind: str, data: bytes) -> BusinessDocument:
    if kind not in DocumentKind.values:
        raise validation_error({"kind": [str(_("Unknown document kind."))]})
    sniffed = files.validate_document(data)
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        if business.verification_status not in EDITABLE:
            raise conflict("business_locked", _("Documents cannot be changed right now."))
        replaced = list(
            business.documents.filter(kind=kind).exclude(status=DocumentStatus.ACCEPTED)
        )
        if business.documents.count() - len(replaced) >= MAX_DOCUMENTS:
            raise AppError("too_many_documents", _("Too many documents uploaded."), 422)
        path = files.save_private(f"documents/{business.public_id}", data, sniffed.ext)
        document = BusinessDocument.objects.create(
            business=business,
            kind=kind,
            file=path,
            content_type=sniffed.content_type,
            size=len(data),
        )
        for old in replaced:  # a fresh upload supersedes earlier pending/rejected files
            _drop(old)
    return document


def list_documents(owner_id: int, business_ref: str) -> list[BusinessDocument]:
    business = selectors.owned_business(owner_id, business_ref)
    return list(business.documents.all())


def delete_document(owner_id: int, business_ref: str, document_id: int) -> None:
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        document = business.documents.filter(pk=document_id).first()
        if document is None:
            raise not_found()
        locked = business.verification_status not in EDITABLE
        if locked or document.status == DocumentStatus.ACCEPTED:
            raise conflict("document_locked", _("This document can no longer be removed."))
        path = document.file
        document.delete()
        transaction.on_commit(lambda: files.delete_private(path))


def download(token: str) -> tuple[BusinessDocument, bytes] | None:
    """Resolve a signed token to the document bytes, or None when invalid/expired."""
    document_id = files.read_token(token)
    if document_id is None:
        return None
    document = BusinessDocument.objects.filter(pk=document_id).first()
    if document is None:
        return None
    with files.private_storage().open(document.file, "rb") as handle:
        return document, bytes(handle.read())
