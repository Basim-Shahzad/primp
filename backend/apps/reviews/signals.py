from typing import Any

from django.db.models.signals import post_delete, post_save

from apps.businesses import pagecache


def _invalidate(sender: Any, instance: Any, **kwargs: Any) -> None:
    review = getattr(instance, "review", instance)
    pagecache.invalidate(review.business_id)


for _model in ("reviews.Review", "reviews.ReviewReply"):
    post_save.connect(_invalidate, sender=_model, dispatch_uid=f"page-save-{_model}")
    post_delete.connect(_invalidate, sender=_model, dispatch_uid=f"page-del-{_model}")
