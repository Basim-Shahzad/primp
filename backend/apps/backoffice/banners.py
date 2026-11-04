"""Promotional banners: admin management, the public feed and click/impression counters."""

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F, Max, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_redis import get_redis_connection
from redis.exceptions import RedisError

from apps.core import audit, ratelimit
from apps.core.errors import AppError, conflict, not_found, validation_error
from apps.core.images import process_image
from apps.geo import adminops as geo

from .models import Banner
from .permissions import AdminActor

PERMISSION = "banners.manage"
SIZES = {"desktop": 1600, "mobile": 800}
MAX_BANNERS = 200
TRACK_LIMIT = (30, 60)  # per IP, per banner, per kind, per minute
VERSION_KEY = "banners:version"
COUNTER_KEY = "banner:{id}:{kind}"
KINDS = ("impressions", "clicks")


# ---------------------------------------------------------------- validation


def check_link(url: str) -> str:
    """An https URL or an internal path; javascript:, data:, http: and //host are refused."""
    value = url.strip()
    bad = validation_error(
        {"link_url": [str(_("Use an https:// address or a path like /offers."))]}
    )
    if not value or len(value) > 500 or any(c in value for c in "\r\n\t "):
        raise bad
    if value.startswith("/"):
        if value.startswith("//") or "\\" in value:
            raise bad
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise bad
    return value


def snapshot(b: Banner) -> dict[str, Any]:
    return {
        "title": b.title,
        "subtitle": b.subtitle,
        "link_url": b.link_url,
        "city_id": b.city_id,
        "starts_at": b.starts_at.isoformat(),
        "ends_at": b.ends_at.isoformat(),
        "is_enabled": b.is_enabled,
        "sort_order": b.sort_order,
        "has_desktop_image": bool(b.desktop_image),
        "has_mobile_image": bool(b.mobile_image),
        "version": b.version,
    }


def _window(starts: datetime, ends: datetime) -> None:
    if ends <= starts:
        raise validation_error({"ends_at": [str(_("The end must be after the start."))]})


def _city(city_id: int | None) -> None:
    if city_id is not None:
        geo.get_city(city_id)  # 404 for an unknown city


def _stale(banner: Banner, version: int) -> None:
    if banner.version != version:
        raise conflict("stale_version", _("The banner changed; reload it and try again."))


def _needs_images(banner: Banner) -> None:
    if banner.is_enabled and not (banner.desktop_image and banner.mobile_image):
        raise AppError(
            "images_required",
            _("Upload the desktop and mobile images before enabling the banner."),
            422,
            {"is_enabled": [str(_("Images are missing."))]},
        )


# ---------------------------------------------------------------- cache


def bump_cache() -> None:
    """Invalidate every cached public feed (the version is part of each key)."""
    try:
        cache.incr(VERSION_KEY)
    except ValueError:
        cache.set(VERSION_KEY, 2, timeout=None)
    transaction.on_commit(lambda: cache.incr(VERSION_KEY) if cache.get(VERSION_KEY) else None)


def _feed_key(city_id: int | None) -> str:
    return f"banners:feed:{cache.get(VERSION_KEY, 1)}:{city_id or 0}"


# ---------------------------------------------------------------- admin


def get(banner_id: int, *, lock: bool = False) -> Banner:
    qs = Banner.objects.select_for_update(of=("self",)) if lock else Banner.objects
    banner = qs.filter(pk=banner_id).first()
    if banner is None:
        raise not_found()
    return banner


def admin_list(
    status: str | None, city_id: int | None, now: datetime | None = None
) -> QuerySet[Banner]:
    now = now or timezone.now()
    qs = Banner.objects.all()
    if city_id is not None:
        qs = qs.filter(city_id=city_id)
    if status == "active":
        qs = qs.filter(is_enabled=True, starts_at__lte=now, ends_at__gt=now)
    elif status == "scheduled":
        qs = qs.filter(starts_at__gt=now)
    elif status == "expired":
        qs = qs.filter(ends_at__lte=now)
    elif status == "disabled":
        qs = qs.filter(is_enabled=False)
    return qs.order_by("sort_order", "id")


def pending_counts(banner_id: int) -> tuple[int, int]:
    """Counts still waiting in Redis (not yet flushed to the database)."""
    try:
        conn = get_redis_connection("default")
        values = [conn.get(COUNTER_KEY.format(id=banner_id, kind=k)) for k in KINDS]
    except RedisError:
        return 0, 0
    return tuple(int(v or 0) for v in values)  # type: ignore[return-value]


def create(actor: AdminActor, data: dict[str, Any]) -> Banner:
    actor.require(PERMISSION)
    data = {**data, "link_url": check_link(data["link_url"])}
    _window(data["starts_at"], data["ends_at"])
    _city(data.get("city_id"))
    with transaction.atomic():
        if Banner.objects.count() >= MAX_BANNERS:
            raise conflict("too_many_banners", _("Too many banners; delete old ones first."))
        last = Banner.objects.aggregate(m=Max("sort_order"))["m"]
        banner = Banner(**data, is_enabled=False, sort_order=(last if last is not None else -1) + 1)
        banner.save()
        audit.record(
            "admin.banner_created",
            actor_id=actor.id,
            target_type="banner",
            target_id=banner.pk,
            after=snapshot(banner),
            ip=actor.ip,
        )
        bump_cache()
    return banner


def update(actor: AdminActor, banner_id: int, version: int, changes: dict[str, Any]) -> Banner:
    actor.require(PERMISSION)
    with transaction.atomic():
        banner = get(banner_id, lock=True)
        _stale(banner, version)
        before = snapshot(banner)
        if "link_url" in changes:
            changes["link_url"] = check_link(changes["link_url"])
        if "city_id" in changes:
            _city(changes["city_id"])
        for key, value in changes.items():
            setattr(banner, key, value)
        _window(banner.starts_at, banner.ends_at)
        _needs_images(banner)
        banner.version += 1
        banner.save()
        audit.record(
            "admin.banner_updated",
            actor_id=actor.id,
            target_type="banner",
            target_id=banner.pk,
            before=before,
            after=snapshot(banner),
            ip=actor.ip,
        )
        bump_cache()
    return banner


def delete(actor: AdminActor, banner_id: int, version: int) -> None:
    actor.require(PERMISSION)
    with transaction.atomic():
        banner = get(banner_id, lock=True)
        _stale(banner, version)
        before = snapshot(banner)
        paths = [p for p in (banner.desktop_image, banner.mobile_image) if p]
        banner.delete()
        audit.record(
            "admin.banner_deleted",
            actor_id=actor.id,
            target_type="banner",
            target_id=banner_id,
            before=before,
            ip=actor.ip,
        )
        for path in paths:
            transaction.on_commit(lambda path=path: default_storage.delete(path))  # type: ignore[misc]
        bump_cache()


def reorder(actor: AdminActor, ids: list[int]) -> list[Banner]:
    actor.require(PERMISSION)
    with transaction.atomic():
        banners = list(Banner.objects.select_for_update(of=("self",)).order_by("sort_order", "id"))
        if len(ids) != len(set(ids)) or set(ids) != {b.pk for b in banners}:
            raise validation_error({"ids": [str(_("Send every banner id exactly once."))]})
        by_id = {b.pk: b for b in banners}
        for position, banner_id in enumerate(ids):
            b = by_id[banner_id]
            if b.sort_order != position:
                b.sort_order = position
                b.version += 1
                b.save(update_fields=["sort_order", "version", "updated_at"])
        audit.record(
            "admin.banners_reordered",
            actor_id=actor.id,
            target_type="banner",
            target_id="all",
            before={"ids": [b.pk for b in banners]},
            after={"ids": ids},
            ip=actor.ip,
        )
        bump_cache()
    return [by_id[i] for i in ids]


def set_image(actor: AdminActor, banner_id: int, kind: str, data: bytes) -> Banner:
    actor.require(PERMISSION)
    image = process_image(data, max_dimension=SIZES[kind])
    with transaction.atomic():
        banner = get(banner_id, lock=True)
        field = f"{kind}_image"
        old = getattr(banner, field)
        path = default_storage.save(
            f"banners/{uuid.uuid4().hex}.{image.ext}", ContentFile(image.content)
        )
        setattr(banner, field, path)
        banner.version += 1
        banner.save(update_fields=[field, "version", "updated_at"])
        audit.record(
            "admin.banner_image_set",
            actor_id=actor.id,
            target_type="banner",
            target_id=banner.pk,
            before={field: bool(old)},
            after={field: True, "kind": kind},
            ip=actor.ip,
        )
        if old:
            transaction.on_commit(lambda: default_storage.delete(old))
        bump_cache()
    return banner


# ---------------------------------------------------------------- public feed


def public_feed(city_id: int | None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Enabled banners for the city (and all-city ones) whose window contains `now`.

    The cache holds the enabled candidates; the schedule is applied at read time, so the
    boundaries are exact even though the candidate list is cached for 60 seconds.
    """
    now = now or timezone.now()
    key = _feed_key(city_id)
    candidates: list[dict[str, Any]] | None = cache.get(key)
    if candidates is None:
        qs = (
            Banner.objects.filter(is_enabled=True)
            .exclude(desktop_image="")
            .exclude(mobile_image="")
        )
        qs = (
            qs.filter(city__isnull=True)
            if city_id is None
            else qs.filter(city_id__in=[city_id]) | qs.filter(city__isnull=True)
        )
        candidates = [
            {
                "id": b.pk,
                "title": b.title,
                "subtitle": b.subtitle,
                "desktop_image_url": str(default_storage.url(b.desktop_image)),
                "mobile_image_url": str(default_storage.url(b.mobile_image)),
                "link_url": b.link_url,
                "starts_at": b.starts_at,
                "ends_at": b.ends_at,
            }
            for b in qs.order_by("sort_order", "id")
        ]
        cache.set(key, candidates, timeout=60)
    return [c for c in candidates if c["starts_at"] <= now < c["ends_at"]]


# ---------------------------------------------------------------- tracking


def track(banner_id: int, kind: str, ip: str, now: datetime | None = None) -> None:
    """Count one impression or click in Redis (flushed to the database by a periodic task)."""
    now = now or timezone.now()
    ratelimit.enforce(
        f"banner-track:{kind}:{banner_id}:{ip}", limit=TRACK_LIMIT[0], window_seconds=TRACK_LIMIT[1]
    )
    banner = Banner.objects.filter(
        pk=banner_id, is_enabled=True, starts_at__lte=now, ends_at__gt=now
    ).first()
    if banner is None:
        raise not_found()
    try:
        get_redis_connection("default").incr(COUNTER_KEY.format(id=banner_id, kind=kind))
    except RedisError:
        Banner.objects.filter(pk=banner_id).update(**{kind: F(kind) + 1})  # degrade to the DB


def flush_counters() -> int:
    """Move Redis counters into the database. Atomic take (GETDEL): running twice adds nothing."""
    flushed = 0
    conn = get_redis_connection("default")
    for banner_id in Banner.objects.values_list("pk", flat=True):
        taken: dict[str, int] = {}
        for kind in KINDS:
            value = conn.getdel(COUNTER_KEY.format(id=banner_id, kind=kind))
            if value:
                taken[kind] = int(value)
        if not taken:
            continue
        try:
            with transaction.atomic():
                Banner.objects.filter(pk=banner_id).update(
                    **{kind: F(kind) + n for kind, n in taken.items()}
                )
        except Exception:
            for kind, n in taken.items():  # put the counts back so nothing is lost
                conn.incrby(COUNTER_KEY.format(id=banner_id, kind=kind), n)
            raise
        flushed += sum(taken.values())
    return flushed
