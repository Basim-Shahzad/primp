from django.db import models

from .permissions import PERMISSION_CHOICES


class AdminPermission(models.Model):
    """Anchor model: it only exists to hold the admin permissions (no table)."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = PERMISSION_CHOICES

    def __str__(self) -> str:
        return "admin permission"
