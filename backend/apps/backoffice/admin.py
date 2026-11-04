from django.contrib import admin

from apps.core.admin_base import ReadOnlyAdmin

from .models import PlatformSettingHistory


@admin.register(PlatformSettingHistory)
class PlatformSettingHistoryAdmin(ReadOnlyAdmin):
    list_display = ("key", "version", "changed_by", "changed_at")
    list_filter = ("key",)
    raw_id_fields = ("changed_by",)
