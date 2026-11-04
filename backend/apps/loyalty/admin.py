from django.contrib import admin

from apps.core.admin_base import ReadOnlyAdmin

from .models import LoyaltyAccount, PointRule, PointTransaction


@admin.register(PointRule)
class PointRuleAdmin(ReadOnlyAdmin):
    list_display = (
        "action",
        "kind",
        "points",
        "unit_amount",
        "is_active",
        "valid_from",
        "valid_to",
    )
    list_filter = ("is_active", "kind")


@admin.register(LoyaltyAccount)
class LoyaltyAccountAdmin(ReadOnlyAdmin):
    list_display = ("customer", "balance", "lifetime_earned")
    raw_id_fields = ("customer",)


@admin.register(PointTransaction)
class PointTransactionAdmin(ReadOnlyAdmin):
    list_display = ("customer", "delta", "reason", "reference_type", "reference_id", "created_at")
    raw_id_fields = ("customer", "admin")
