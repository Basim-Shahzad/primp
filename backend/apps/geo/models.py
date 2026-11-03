from django.contrib.gis.db import models as gis_models
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class CityVisibility(models.TextChoices):
    HIDDEN = "hidden", _("Hidden")
    PREVIEW = "preview", _("Preview")
    PUBLIC = "public", _("Public")


class City(TimeStampedModel):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    centre = gis_models.PointField(geography=False, srid=4326)
    visibility = models.CharField(
        max_length=10, choices=CityVisibility.choices, default=CityVisibility.HIDDEN
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "cities"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_visible(self) -> bool:
        return self.is_active and self.visibility == CityVisibility.PUBLIC


class Area(TimeStampedModel):
    city = models.ForeignKey(City, on_delete=models.CASCADE, related_name="areas")
    name = models.CharField(max_length=100)
    slug = models.SlugField()
    centre = gis_models.PointField(geography=False, srid=4326)
    polygon = gis_models.PolygonField(geography=False, srid=4326, null=True, blank=True)
    is_pilot = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["city", "slug"], name="area_slug_unique_per_city"),
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.city.name}"
