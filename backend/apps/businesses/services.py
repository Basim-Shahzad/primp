from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError

from .models import Service, Staff, StaffService


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
