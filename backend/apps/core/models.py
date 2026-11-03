import uuid
from typing import Any, NoReturn

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PublicIdMixin(models.Model):
    """Exposes a random UUID instead of the sequential primary key."""

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet["SoftDeleteModel"]):
    def alive(self) -> "SoftDeleteQuerySet":
        return self.filter(deleted_at__isnull=True)

    def dead(self) -> "SoftDeleteQuerySet":
        return self.filter(deleted_at__isnull=False)

    def delete(self) -> tuple[int, dict[str, int]]:
        count = self.alive().update(deleted_at=timezone.now())
        return count, {self.model._meta.label: count}


class SoftDeleteManager(models.Manager["SoftDeleteModel"]):
    """Default manager hides soft-deleted rows."""

    def get_queryset(self) -> SoftDeleteQuerySet:
        return SoftDeleteQuerySet(self.model, using=self._db).alive()


class SoftDeleteModel(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager["SoftDeleteModel"]()

    class Meta:
        abstract = True

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])
        return 1, {self._meta.label: 1}

    def restore(self) -> None:
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])


class ImmutableRecordError(Exception):
    """Raised on any attempt to modify or delete an append-only record."""


class AuditLogQuerySet(models.QuerySet["AuditLog"]):
    def update(self, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("Audit log entries cannot be updated.")

    def delete(self) -> NoReturn:
        raise ImmutableRecordError("Audit log entries cannot be deleted.")

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("Audit log entries cannot be updated.")


class AuditLog(models.Model):
    """Append-only record of sensitive actions. actor_id is a plain integer so the log
    survives user removal and core has no dependency on the accounts app."""

    actor_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["target_type", "target_id"])]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("Audit log entries cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("Audit log entries cannot be deleted.")
