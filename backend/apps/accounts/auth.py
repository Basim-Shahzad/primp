from typing import Any

from django.http import HttpRequest
from ninja.security import HttpBearer

from .models import User, UserStatus
from .tokens import decode_access_token, session_is_active


def user_from_token(token: str) -> User | None:
    claims = decode_access_token(token)
    if claims is None:
        return None
    user = User.objects.filter(public_id=claims["sub"]).first()
    if user is None or user.status != UserStatus.ACTIVE:
        return None
    if not session_is_active(claims["sid"]):
        return None
    return user


class JWTAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> Any:
        return user_from_token(token)


jwt_auth = JWTAuth()


def optional_user(request: HttpRequest) -> User | None:
    """Resolve the bearer user without failing for anonymous requests."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return user_from_token(token.strip())
