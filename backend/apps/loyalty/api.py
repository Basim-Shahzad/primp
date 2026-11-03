from django.http import HttpRequest
from ninja import Query, Router

from apps.accounts.auth import jwt_auth
from apps.core.pagination import CursorParams, paginate_cursor

from . import services
from .models import PointTransaction
from .schemas import PointsOut, TransactionOut, TransactionPageOut

users_router = Router(tags=["loyalty"])


@users_router.get("/me/points", auth=jwt_auth, response=PointsOut)
def my_points(request: HttpRequest) -> PointsOut:
    balance, lifetime = services.summary(request.auth.pk)  # type: ignore[attr-defined]
    return PointsOut(balance=balance, lifetime_earned=lifetime)


@users_router.get("/me/points/transactions", auth=jwt_auth, response=TransactionPageOut)
def my_transactions(request: HttpRequest, params: Query[CursorParams]) -> TransactionPageOut:
    qs = PointTransaction.objects.filter(customer_id=request.auth.pk)  # type: ignore[attr-defined]
    page = paginate_cursor(
        qs, ordering=["-created_at", "-id"], cursor=params.cursor, limit=params.limit
    )
    return TransactionPageOut(
        items=[
            TransactionOut(
                id=t.pk,
                delta=t.delta,
                reason=t.reason,
                reference_type=t.reference_type,
                reference_id=t.reference_id,
                expires_at=t.expires_at,
                created_at=t.created_at,
            )
            for t in page.items
        ],
        next_cursor=page.next_cursor,
    )
