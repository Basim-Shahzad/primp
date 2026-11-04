"""Review moderation by admins (hide / restore / remove) with notices to author and business."""

from typing import Any

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.errors import conflict, validation_error
from apps.notifications import services as notifications
from apps.reviews import adminops as reviews

from ..permissions import AdminActor

PAST = {"hide": "hidden", "remove": "removed", "restore": "restored"}


def moderate(actor: AdminActor, review_ref: str, action: str, reason: str) -> Any:
    actor.require("reviews.moderate")
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise validation_error({"reason": [str(_("Enter a reason of 1 to 500 characters."))]})
    allowed_from, target = reviews.ALLOWED[action]
    with transaction.atomic():
        review = reviews.get_review(review_ref, lock=True)
        if review.status not in allowed_from:
            raise conflict(
                "invalid_transition",
                _("A %(from)s review cannot be %(to)s.")
                % {"from": review.status, "to": PAST[action]},
            )
        # the reviews service recomputes the rating aggregate and writes the audit entry
        updated = reviews.moderate(review.pk, target, actor.id, reason, ip=actor.ip)
        payload = {"business_name": review.business.name, "action": PAST[action], "reason": reason}
        notifications.notify(
            review.customer_id, "review_moderated_author", payload, include_sms=False
        )
        notifications.notify(
            review.business.owner_id,
            "review_moderated_business",
            {**payload, "reason": ""},
            include_sms=False,
        )
    return updated
