"""Shared helpers for provider API tests."""

import io
import json
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import User
from apps.accounts.roles import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.businesses.factories import BusinessFactory
from apps.businesses.models import Business, VerificationStatus

P = "/api/v1/provider"


def png_bytes(size: tuple[int, int] = (64, 64), fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, format=fmt)
    return out.getvalue()


def pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def owner_headers(user: User | None = None) -> tuple[User, dict[str, str]]:
    user = user or UserFactory(role=Role.BUSINESS_OWNER)
    _, session = login_client(user)
    return user, bearer(session)


def customer_headers() -> dict[str, str]:
    _, session = login_client(make_customer())
    return bearer(session)


def draft_business(owner: User, **kw: Any) -> Business:
    kw.setdefault("verification_status", VerificationStatus.DRAFT)
    kw.setdefault("is_listed", False)
    return BusinessFactory(owner=owner, **kw)


def call(method: str, url: str, headers: dict[str, str], data: Any = None, **extra: Any) -> Any:
    client = Client()
    fn = getattr(client, method)
    if data is None:
        return fn(url, **headers, **extra)
    return fn(url, data=json.dumps(data), content_type="application/json", **headers, **extra)


def upload(url: str, headers: dict[str, str], name: str, data: bytes, **form: Any) -> Any:
    return Client().post(
        url,
        {"file": SimpleUploadedFile(name, data, content_type="application/octet-stream"), **form},
        **headers,
    )
