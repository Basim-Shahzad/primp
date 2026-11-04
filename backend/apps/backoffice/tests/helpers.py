import json
from typing import Any

from django.test import Client

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import User
from apps.accounts.roles import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.backoffice.factories import make_admin

A = "/api/v1/admin"
ROLES = ["customer", "business_owner", "staff", "support_agent", "ops_admin", "super_admin"]


def user_for(role: str) -> User:
    if role == "customer":
        return make_customer()
    if role in ("business_owner", "staff"):
        return UserFactory(role=Role(role))
    return make_admin(role)


def headers_for(user: User) -> dict[str, str]:
    _, session = login_client(user)
    return bearer(session)


def admin_headers(kind: str = "ops_admin") -> tuple[User, dict[str, str]]:
    user = make_admin(kind)
    return user, headers_for(user)


def reauth(headers: dict[str, str], password: str = "Str0ng-pass-phrase!") -> Any:
    return Client().post(
        f"{A}/reauth",
        data=json.dumps({"password": password}),
        content_type="application/json",
        **headers,
    )


def call(method: str, url: str, headers: dict[str, str], data: Any = None, **extra: Any) -> Any:
    client = Client()
    fn = getattr(client, method)
    if data is None:
        return fn(url, **headers, **extra)
    return fn(url, data=json.dumps(data), content_type="application/json", **headers, **extra)
