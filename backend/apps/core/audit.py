from typing import Any

from django.http import HttpRequest

from .http import get_client_ip
from .logging import request_id_var
from .models import AuditLog


def record(
    action: str,
    *,
    actor_id: int | None = None,
    target_type: str = "",
    target_id: Any = "",
    before: Any = None,
    after: Any = None,
    request: HttpRequest | None = None,
    ip: str | None = None,
) -> AuditLog:
    rid = request_id_var.get()
    return AuditLog.objects.create(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        before=before,
        after=after,
        ip_address=ip or (get_client_ip(request) if request else None),
        request_id="" if rid == "-" else rid,
    )
