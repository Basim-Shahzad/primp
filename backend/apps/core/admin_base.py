"""Read-only Django admin pages: records that must change only through audited services."""

from typing import Any

from django.contrib import admin
from django.http import HttpRequest


class ReadOnlyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """View and search only: no add, change, delete or bulk actions."""

    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return [f.name for f in self.model._meta.fields if f.name not in self.exclude_fields()]

    def exclude_fields(self) -> tuple[str, ...]:
        return tuple(self.exclude or ())
