from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class Category(TimeStampedModel):
    """Two-level taxonomy: category -> sub-category."""

    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    icon = models.CharField(max_length=100, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["sort_order", "name"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(parent=models.F("id")), name="category_not_own_parent"
            )
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.parent_id is not None:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": _("A category cannot be its own parent.")})
            if self.parent.parent_id is not None:  # type: ignore[union-attr]
                raise ValidationError({"parent": _("Categories can be nested two levels deep.")})
            if self.pk and self.children.exists():
                raise ValidationError({"parent": _("A category with children cannot be nested.")})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    @property
    def is_top_level(self) -> bool:
        return self.parent_id is None
