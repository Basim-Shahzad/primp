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


class SuspensionRecord(models.Model):
    """One suspension of an account: why, by whom and the listing state to restore."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    suspended_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.CharField(max_length=500)
    previous_listings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reactivated_at = models.DateTimeField(null=True, blank=True)
    reactivated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reactivation_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(reactivated_at__isnull=True),
                name="one_open_suspension_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} suspended"
