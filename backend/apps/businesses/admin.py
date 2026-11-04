from django.contrib import admin

from apps.core.admin_base import ReadOnlyAdmin

from .models import BusinessDocument


@admin.register(BusinessDocument)
class BusinessDocumentAdmin(ReadOnlyAdmin):
    """Verification documents: metadata only, never the stored file path or a link to it."""

    exclude = ("file",)
    list_display = ("business", "kind", "status", "content_type", "size", "created_at")
    list_filter = ("kind", "status")
    raw_id_fields = ("business",)
