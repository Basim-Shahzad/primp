"""Owner notification preferences and in-app feed (`/provider/...`)."""

from django.http import HttpRequest
from ninja import Query, Router

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.core.errors import validation_error
from apps.core.permissions import require_role

from . import feed, preferences
from .provider_schemas import (
    FeedItemOut,
    FeedOut,
    FeedQuery,
    PreferenceOut,
    PreferencePatchIn,
    ReadIn,
    ReadOut,
)

provider_router = Router(tags=["provider"], auth=jwt_auth)
owner_only = require_role(Role.BUSINESS_OWNER)


def _prefs(rows: list[preferences.Preference]) -> list[PreferenceOut]:
    return [
        PreferenceOut(event=r.event, label=r.label, dashboard=r.dashboard, sms=r.sms, email=r.email)
        for r in rows
    ]


@provider_router.get("/notification-preferences", response=list[PreferenceOut])
def get_preferences(request: HttpRequest) -> list[PreferenceOut]:
    user = owner_only(request)
    return _prefs(preferences.list_preferences(user.pk))


@provider_router.patch("/notification-preferences", response=list[PreferenceOut])
def patch_preferences(request: HttpRequest, payload: PreferencePatchIn) -> list[PreferenceOut]:
    user = owner_only(request)
    updates = [(p.event, p.sms, p.email) for p in payload.preferences]
    return _prefs(preferences.update_preferences(user.pk, updates))


@provider_router.get("/notifications", response=FeedOut)
def list_notifications(request: HttpRequest, params: Query[FeedQuery]) -> FeedOut:
    user = owner_only(request)
    page, unread = feed.feed(
        user.pk, cursor=params.cursor, limit=params.limit, unread_only=params.unread_only
    )
    items = []
    for n, (title, body) in zip(page.items, feed.render_items(page.items), strict=True):
        items.append(
            FeedItemOut(
                id=n.pk,
                event=n.template_key,
                title=title,
                body=body,
                created_at=n.created_at,
                read_at=n.read_at,
                status=n.status,
            )
        )
    return FeedOut(items=items, next_cursor=page.next_cursor, unread_count=unread)


@provider_router.post("/notifications/read", response=ReadOut)
def mark_notifications_read(request: HttpRequest, payload: ReadIn) -> ReadOut:
    user = owner_only(request)
    if payload.all == (payload.ids is not None):
        raise validation_error({"ids": ["Send either `ids` or `all: true`, not both or neither."]})
    updated = feed.mark_read(user.pk, ids=payload.ids, all_=payload.all)
    _page, unread = feed.feed(user.pk, cursor=None, limit=1)
    return ReadOut(updated=updated, unread_count=unread)
