from django.contrib import admin

from apps.core.admin_base import ReadOnlyAdmin

from .models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "business", "status", "scheduled_start", "customer")
    list_filter = ("status",)
    search_fields = ("public_id", "business__name")
    raw_id_fields = ("customer", "business", "staff")
