"""Invalidate cached next-available-slot values when inputs change."""

from typing import Any

from django.db.models.signals import post_delete, post_save

from . import cache_keys


def _invalidate(sender: Any, instance: Any, **kwargs: Any) -> None:
    business_id = getattr(instance, "business_id", None)
    if business_id is None and hasattr(instance, "staff"):
        business_id = instance.staff.business_id
    if business_id is not None:
        cache_keys.invalidate_next_slot(business_id)


for _model in (
    "businesses.Closure",
    "businesses.OpeningHours",
    "businesses.Service",
    "businesses.Staff",
    "businesses.StaffService",
    "bookings.Appointment",
):
    post_save.connect(_invalidate, sender=_model, dispatch_uid=f"invalidate-save-{_model}")
    post_delete.connect(_invalidate, sender=_model, dispatch_uid=f"invalidate-del-{_model}")
