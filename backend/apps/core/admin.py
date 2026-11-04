from django.contrib import admin

from .admin_base import ReadOnlyAdmin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "action", "target_type", "target_id", "actor_id", "ip_address")
    list_filter = ("action", "target_type")
    search_fields = ("action", "target_id", "request_id")
    date_hierarchy = "created_at"
