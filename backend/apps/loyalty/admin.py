from django.contrib import admin

from .models import LoyaltyAccount, PointRule, PointTransaction


@admin.register(PointRule)
class PointRuleAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
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
class LoyaltyAccountAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("customer", "balance", "lifetime_earned")
    readonly_fields = ("customer", "balance", "lifetime_earned")


@admin.register(PointTransaction)
class PointTransactionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("customer", "delta", "reason", "reference_type", "reference_id", "created_at")

    def has_change_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False

    def has_delete_permission(self, request, obj=None):  # type: ignore[no-untyped-def]
        return False
