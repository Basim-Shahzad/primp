from django.contrib import admin

from apps.core.admin_base import ReadOnlyAdmin

from .models import RefreshToken


@admin.register(RefreshToken)
class RefreshTokenAdmin(ReadOnlyAdmin):
    """Sessions: never shows the token hash."""

    exclude = ("token_hash",)
    list_display = ("user", "created_at", "expires_at", "revoked_at", "user_agent")
    list_filter = ("revoked_at",)
    search_fields = ("user__email", "user__phone")
    raw_id_fields = ("user", "replaced_by")
