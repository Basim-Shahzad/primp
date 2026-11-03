from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError

from . import pagecache
from .models import Business, Service, Staff, StaffService


def link_staff_service(staff: Staff, service: Service) -> StaffService:
    """Allow a staff member to perform a service (same business only)."""
    if staff.business_id != service.business_id:
        raise AppError(
            "business_mismatch",
            _("Staff and service must belong to the same business."),
            422,
        )
    link, _created = StaffService.objects.get_or_create(staff=staff, service=service)
    return link


def set_rating(business_id: int, avg: Decimal, count: int) -> None:
    Business.objects.filter(pk=business_id).update(rating_avg=avg, rating_count=count)
    pagecache.invalidate(business_id)
