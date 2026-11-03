from typing import Any

from django.db.models.signals import post_delete, post_save

from . import pagecache


def _business_id(instance: Any) -> int | None:
    if instance.__class__.__name__ == "Business":
        return int(instance.pk)
    if hasattr(instance, "business_id"):
        return int(instance.business_id)
    if instance.__class__.__name__ == "StaffService":
        return int(instance.staff.business_id)
    return None


def _invalidate(sender: Any, instance: Any, **kwargs: Any) -> None:
    business_id = _business_id(instance)
    if business_id is not None:
        pagecache.invalidate(business_id)


for _model in (
    "businesses.Business",
    "businesses.BusinessPhoto",
    "businesses.Staff",
    "businesses.Service",
    "businesses.StaffService",
    "businesses.OpeningHours",
    "businesses.Closure",
):
    post_save.connect(_invalidate, sender=_model, dispatch_uid=f"page-save-{_model}")
    post_delete.connect(_invalidate, sender=_model, dispatch_uid=f"page-del-{_model}")
