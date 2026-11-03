from collections.abc import Callable
from typing import Any

from django.http import HttpRequest

from .errors import AppError, forbidden, not_found


def require_role(*roles: str) -> Callable[[HttpRequest], Any]:
    """Return a checker to call at the top of an endpoint: `require_role("admin")(request)`.

    Anonymous requests give 401, wrong role 403. Returns the authenticated user.
    """

    def check(request: HttpRequest) -> Any:
        user = getattr(request, "auth", None)
        if user is None:
            raise AppError("unauthenticated", "Authentication required.", 401)
        if getattr(user, "role", None) not in roles:
            raise forbidden()
        return user

    return check


def owned_or_404(obj: Any, owner_id: int, owner_attr: str = "customer_id") -> Any:
    """Return obj when it belongs to owner_id, else 404 (never leak existence)."""
    if obj is None or getattr(obj, owner_attr) != owner_id:
        raise not_found()
    return obj
