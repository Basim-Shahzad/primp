from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    CUSTOMER = "customer", _("Customer")
    BUSINESS_OWNER = "business_owner", _("Business owner")
    STAFF = "staff", _("Staff")
    ADMIN = "admin", _("Admin")
    SUPER_ADMIN = "super_admin", _("Super admin")
