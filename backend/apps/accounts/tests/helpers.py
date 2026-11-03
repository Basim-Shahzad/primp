import json
from typing import Any

from django.test import Client

from apps.accounts import tokens
from apps.accounts.cookies import CSRF_HEADER
from apps.accounts.models import User


def login_client(user: User) -> tuple[Client, tokens.SessionTokens]:
    """Client with refresh cookies set and a bearer-ready session."""
    from django.conf import settings

    session = tokens.issue_session(user)
    client = Client()
    client.cookies[settings.REFRESH_COOKIE_NAME] = session.refresh_token
    client.cookies[settings.REFRESH_CSRF_COOKIE_NAME] = "csrf-value"
    return client, session


def bearer(session: tokens.SessionTokens) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {session.access_token}"}


def post_json(client: Client, url: str, data: Any = None, **extra: Any) -> Any:
    return client.post(
        url,
        data=json.dumps(data) if data is not None else "",
        content_type="application/json",
        **extra,
    )


CSRF = {"HTTP_" + CSRF_HEADER.upper().replace("-", "_"): "csrf-value"}
