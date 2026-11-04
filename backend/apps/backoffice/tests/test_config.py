import logging
import threading
from typing import Any
from unittest import mock

import pytest
from django.db import OperationalError, connection

from apps.backoffice import config_service
from apps.backoffice.config_registry import REGISTRY, UnknownSetting
from apps.backoffice.models import PlatformSetting, PlatformSettingHistory
from apps.backoffice.tests.helpers import A, admin_headers, call, reauth
from apps.core.models import AuditLog, ImmutableRecordError

pytestmark = pytest.mark.django_db
C = f"{A}/config"


def boss() -> tuple[Any, dict[str, str]]:
    admin, h = admin_headers("super_admin")
    assert reauth(h).status_code == 200
    return admin, h


def patch(h: dict[str, str], key: str, value: Any, version: int | None = None) -> Any:
    current = PlatformSetting.objects.filter(key=key).values_list("version", flat=True).first() or 0
    return call(
        "patch",
        f"{C}/{key}",
        h,
        {"value": value, "version": current if version is None else version},
    )


def audit() -> list[AuditLog]:
    return list(AuditLog.objects.filter(action="admin.config_changed"))


# ------------------------------------------------------------ listing and defaults


def test_listing_shows_every_registered_key_with_default_schema_and_version() -> None:
    _, h = admin_headers("super_admin")
    r = call("get", C, h)  # reading needs no re-authentication
    assert r.status_code == 200, r.content
    rows = {x["key"]: x for x in r.json()}
    assert set(rows) == set(REGISTRY)
    required = {
        "reminder_offsets",
        "default_cancellation_notice_hours",
        "commission_rate_bps",
        "slot_hold_seconds",
        "pending_escalation_minutes",
        "pending_auto_cancel_minutes",
        "otp_limits",
        "points_expiry_months",
        "points_adjust_daily_limit",
        "auto_ticket_priorities",
    }
    assert required <= set(rows)
    hold = rows["slot_hold_seconds"]
    assert (hold["value"], hold["default"], hold["is_default"], hold["version"]) == (
        300,
        300,
        True,
        0,
    )
    assert (
        hold["value_schema"]["minimum"] == 60
        and hold["value_schema"]["maximum"] == 1800
        and hold["description"]
    )
    assert rows["otp_limits"]["value"]["ttl_seconds"] == 300


def test_only_super_admins_may_read_or_change_settings() -> None:
    for kind in ("support_agent", "ops_admin"):
        _, h = admin_headers(kind)
        reauth(h)
        assert call("get", C, h).status_code == 403
        assert patch(h, "slot_hold_seconds", 400).status_code == 403
        assert call("get", f"{C}/slot_hold_seconds/history", h).status_code == 403
    assert call("get", C, {}).status_code == 401
    assert not PlatformSetting.objects.exists()


def test_defaults_come_from_django_settings_when_no_override_exists(settings) -> None:  # type: ignore[no-untyped-def]
    assert config_service.get("slot_hold_seconds") == 300
    settings.BOOKING_HOLD_SECONDS = 123
    from django.core.cache import cache

    cache.clear()
    assert config_service.get("slot_hold_seconds") == 123
    assert config_service.get("pending_escalation_minutes") == settings.PENDING_ESCALATION_MINUTES
    assert config_service.get("auto_ticket_late_cancellations") is False
    assert config_service.get("auto_ticket_priorities")["booking_dispute"] == "high"
    with pytest.raises(UnknownSetting):
        config_service.get("no_such_key")


# ------------------------------------------------------------ changing a setting


def test_change_is_validated_stored_historied_audited_and_visible_at_once() -> None:
    admin, h = boss()
    assert config_service.get("slot_hold_seconds") == 300  # warms the cache
    r = patch(h, "slot_hold_seconds", 420)
    assert r.status_code == 200, r.content
    assert r.json() == {"key": "slot_hold_seconds", "value": 420, "version": 1}
    assert config_service.get("slot_hold_seconds") == 420  # next read sees it
    row = PlatformSetting.objects.get(key="slot_hold_seconds")
    assert (row.value, row.version, row.updated_by_id, row.schema_id) == (
        420,
        1,
        admin.pk,
        "slot_hold_seconds",
    )
    hist = PlatformSettingHistory.objects.get()
    assert (hist.key, hist.old_value, hist.new_value, hist.version, hist.changed_by_id) == (
        "slot_hold_seconds",
        300,
        420,
        1,
        admin.pk,
    )
    (a,) = audit()
    assert (
        a.actor_id == admin.pk and a.target_type == "setting" and a.target_id == "slot_hold_seconds"
    )
    assert (
        a.before == {"value": 300, "version": 0}
        and a.after == {"value": 420, "version": 1}
        and a.ip_address
    )
    listed = {x["key"]: x for x in call("get", C, h).json()}["slot_hold_seconds"]
    assert (listed["value"], listed["is_default"], listed["version"]) == (420, False, 1)
    assert listed["updated_by"] and listed["updated_at"]
    second = patch(h, "slot_hold_seconds", 500)
    assert second.json()["version"] == 2 and config_service.get("slot_hold_seconds") == 500
    assert PlatformSettingHistory.objects.filter(key="slot_hold_seconds").count() == 2


def test_changes_need_a_recent_reauthentication() -> None:
    _, h = admin_headers("super_admin")
    r = patch(h, "slot_hold_seconds", 400)
    assert r.status_code == 403 and r.json()["code"] == "reauth_required"
    assert not PlatformSetting.objects.exists() and not audit()
    reauth(h)
    assert patch(h, "slot_hold_seconds", 400).status_code == 200


INT_KEYS = {
    "default_cancellation_notice_hours": (0, 72),
    "commission_rate_bps": (0, 5000),
    "slot_hold_seconds": (60, 1800),
    "pending_escalation_minutes": (5, 1440),
    "pending_auto_cancel_minutes": (30, 4320),
    "points_expiry_months": (1, 60),
    "points_adjust_daily_limit": (1, 1_000_000),
    "points_adjust_max": (1, 100_000),
}


@pytest.mark.parametrize("key", sorted(INT_KEYS))
def test_integer_settings_enforce_their_bounds(key: str) -> None:
    _, h = boss()
    low, high = INT_KEYS[key]
    # cross-key partners are lifted out of the way so only the bounds are under test
    if key == "pending_escalation_minutes":
        assert patch(h, "pending_auto_cancel_minutes", 4320).status_code == 200
    if key == "points_adjust_max":
        assert patch(h, "points_adjust_daily_limit", 1_000_000).status_code == 200
    for bad in (low - 1, high + 1, "x", 1.5, None, True):
        r = patch(h, key, bad)
        assert r.status_code == 422 and "value" in r.json()["fields"], (key, bad)
    stored = PlatformSetting.objects.filter(key=key).count()
    assert stored == 0
    for good in (low, high):
        if key == "pending_auto_cancel_minutes" and good == low:
            continue  # 30 would not exceed the escalation default (checked in the cross-key test)
        if key == "points_adjust_daily_limit" and good == low:
            continue
        assert patch(h, key, good).status_code == 200, (key, good)
    assert not [x for x in audit() if x.target_id == key and x.after["value"] not in (low, high)]


def test_object_settings_are_validated_field_by_field() -> None:
    _, h = boss()
    ok = {"reminder_24h": 86400, "reminder_2h": 7200}
    assert patch(h, "reminder_offsets", ok).status_code == 200
    for bad in (
        {"reminder_24h": 7200, "reminder_2h": 7200},  # not longer
        {"reminder_24h": 86400},  # missing field
        {**ok, "extra": 1},  # unknown field
        {"reminder_24h": "x", "reminder_2h": 7200},
        {"reminder_24h": 86400, "reminder_2h": 10},  # below the minimum
        [],
        "text",
    ):
        r = patch(h, "reminder_offsets", bad)
        assert r.status_code == 422 and "value" in r.json()["fields"], bad
    otp = {
        "ttl_seconds": 300,
        "resend_cooldown_seconds": 60,
        "max_verify_attempts": 5,
        "max_requests_per_hour": 5,
        "max_requests_per_ip_hour": 20,
    }
    assert patch(h, "otp_limits", otp).status_code == 200
    for bad in (
        {**otp, "resend_cooldown_seconds": 300},
        {**otp, "ttl_seconds": 59},
        {**otp, "max_verify_attempts": 11},
        {**otp, "max_requests_per_hour": 0},
    ):
        assert patch(h, "otp_limits", bad).status_code == 422, bad
    assert PlatformSetting.objects.get(key="otp_limits").value == otp


def test_priorities_and_flags() -> None:
    _, h = boss()
    assert (
        patch(
            h, "auto_ticket_priorities", {"other": "urgent", "booking_dispute": "low"}
        ).status_code
        == 200
    )
    assert config_service.get("auto_ticket_priorities") == {
        "other": "urgent",
        "booking_dispute": "low",
    }
    for bad in ({"nonsense": "low"}, {"other": "critical"}, {"other": 3}, ["x"]):
        assert patch(h, "auto_ticket_priorities", bad).status_code == 422, bad
    assert patch(h, "auto_ticket_late_cancellations", True).status_code == 200
    assert config_service.get("auto_ticket_late_cancellations") is True
    assert patch(h, "auto_ticket_late_cancellations", "yes please").status_code == 422


def test_rules_that_span_two_settings() -> None:
    _, h = boss()
    assert (
        patch(h, "pending_escalation_minutes", 720).status_code == 422
    )  # not before the auto-cancel (720)
    assert (
        patch(h, "pending_auto_cancel_minutes", 120).status_code == 422
    )  # not after the escalation (120)
    assert patch(h, "pending_auto_cancel_minutes", 121).status_code == 200
    assert patch(h, "pending_escalation_minutes", 120).status_code == 200
    assert patch(h, "points_adjust_max", 50_001).status_code == 422  # daily limit is 50,000
    assert patch(h, "points_adjust_max", 50_000).status_code == 200
    assert patch(h, "points_adjust_daily_limit", 49_999).status_code == 422
    assert patch(h, "points_adjust_daily_limit", 50_000).status_code == 200


def test_versions_unknown_keys_and_missing_fields() -> None:
    _, h = boss()
    assert patch(h, "slot_hold_seconds", 400).status_code == 200
    stale = patch(h, "slot_hold_seconds", 500, version=0)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    ahead = patch(h, "slot_hold_seconds", 500, version=7)
    assert ahead.status_code == 409
    assert config_service.get("slot_hold_seconds") == 400 and len(audit()) == 1
    assert patch(h, "no_such_key", 1, version=0).status_code == 404
    assert call("patch", f"{C}/slot_hold_seconds", h, {"value": 400}).status_code == 422
    assert call("patch", f"{C}/slot_hold_seconds", h, {"version": 1}).status_code == 422
    assert (
        call("patch", f"{C}/slot_hold_seconds", h, {"value": 400, "version": -1}).status_code == 422
    )


# ------------------------------------------------------------ history


def test_history_lists_changes_newest_first_and_is_append_only() -> None:
    admin, h = boss()
    for value in (400, 500, 600):
        patch(h, "slot_hold_seconds", value)
    patch(h, "points_expiry_months", 6)
    r = call("get", f"{C}/slot_hold_seconds/history", h).json()
    assert r["total"] == 3 and [x["version"] for x in r["items"]] == [3, 2, 1]
    assert (r["items"][0]["old_value"], r["items"][0]["new_value"]) == (500, 600)
    assert r["items"][2]["old_value"] == 300 and r["items"][0]["changed_by"] == str(admin.public_id)
    page = call("get", f"{C}/slot_hold_seconds/history?limit=1&offset=1", h).json()
    assert [x["version"] for x in page["items"]] == [2]
    assert call("get", f"{C}/slot_hold_seconds/history?limit=0", h).status_code == 422
    assert call("get", f"{C}/nope/history", h).status_code == 404
    entry = PlatformSettingHistory.objects.first()
    assert entry is not None
    with pytest.raises(ImmutableRecordError):
        entry.save()
    with pytest.raises(ImmutableRecordError):
        entry.delete()
    with pytest.raises(ImmutableRecordError):
        PlatformSettingHistory.objects.all().update(version=9)
    with pytest.raises(ImmutableRecordError):
        PlatformSettingHistory.objects.all().delete()


# ------------------------------------------------------------ resilience


def test_reads_fall_back_to_the_default_when_the_database_is_down(caplog) -> None:  # type: ignore[no-untyped-def]
    _, h = boss()
    patch(h, "slot_hold_seconds", 777)
    from django.core.cache import cache

    cache.clear()
    with (
        mock.patch("apps.backoffice.models.PlatformSetting.objects") as manager,
        caplog.at_level(logging.WARNING),
    ):
        manager.filter.side_effect = OperationalError("db down")
        assert config_service.get("slot_hold_seconds") == 300  # the code default, not a crash
    assert "using the default" in caplog.text
    cache.clear()
    assert config_service.get("slot_hold_seconds") == 777  # the database is back


def test_reads_survive_a_cache_outage() -> None:
    _, h = boss()
    patch(h, "slot_hold_seconds", 555)
    with mock.patch("apps.backoffice.config_service.cache") as broken:
        broken.get.side_effect = ConnectionError("redis down")
        broken.set.side_effect = ConnectionError("redis down")
        assert config_service.get("slot_hold_seconds") == 555
        config_service.invalidate("slot_hold_seconds")  # must not raise either


def test_an_invalid_stored_value_falls_back_to_the_default(caplog) -> None:  # type: ignore[no-untyped-def]
    PlatformSetting.objects.create(key="slot_hold_seconds", value="garbage", schema_id="x")
    from django.core.cache import cache

    cache.clear()
    with caplog.at_level(logging.ERROR):
        assert config_service.get("slot_hold_seconds") == 300
    assert "invalid" in caplog.text
    assert config_service.version_of("slot_hold_seconds") == 1
    assert config_service.version_of("points_expiry_months") == 0


def test_the_value_is_cached_between_reads(django_assert_num_queries) -> None:  # type: ignore[no-untyped-def]
    config_service.get("slot_hold_seconds")
    with django_assert_num_queries(0):
        assert config_service.get("slot_hold_seconds") == 300


# ------------------------------------------------------------ concurrency


@pytest.mark.django_db(transaction=True)
def test_two_admins_changing_the_same_setting_one_wins(frozen, long_tokens) -> None:  # type: ignore[no-untyped-def]
    _, h1 = boss()
    _, h2 = boss()
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def worker(headers: dict[str, str], value: int) -> None:
        try:
            barrier.wait()
            codes.append(
                call(
                    "patch", f"{C}/slot_hold_seconds", headers, {"value": value, "version": 0}
                ).status_code
            )
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(h1, 400)),
        threading.Thread(target=worker, args=(h2, 500)),
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 409]
    assert PlatformSetting.objects.get(key="slot_hold_seconds").version == 1
    assert PlatformSettingHistory.objects.count() == 1 and len(audit()) == 1


# ------------------------------------------------- changes reach the running code


def test_changes_take_effect_without_a_restart() -> None:
    from datetime import UTC, datetime

    from apps.accounts import otp
    from apps.bookings import holds
    from apps.loyalty.services import expiry_from
    from apps.notifications import services as notifications

    _, h = boss()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    before_expiry = expiry_from(now)
    assert holds.hold_ttl_seconds() == 300
    assert patch(h, "slot_hold_seconds", 600).status_code == 200
    assert holds.hold_ttl_seconds() == 600

    limits = {**otp.limits(), "ttl_seconds": 120}
    assert patch(h, "otp_limits", limits).status_code == 200
    assert otp.limits()["ttl_seconds"] == 120

    assert patch(h, "points_expiry_months", 6).status_code == 200
    assert expiry_from(now) < before_expiry

    offsets = {"reminder_24h": 90000, "reminder_2h": 3600}
    r = patch(h, "reminder_offsets", offsets)
    assert r.status_code == 200
    assert notifications.reminder_offsets() == offsets


def test_ticket_priorities_and_default_notice_follow_the_setting() -> None:
    from apps.backoffice.services import tickets

    _, h = boss()
    base = dict(config_service.get("auto_ticket_priorities"))
    assert patch(h, "auto_ticket_priorities", {**base, "other": "urgent"}).status_code == 200
    assert tickets.default_priority("other") == "urgent"
    assert patch(h, "default_cancellation_notice_hours", 6).status_code == 200
    assert config_service.get("default_cancellation_notice_hours") == 6
