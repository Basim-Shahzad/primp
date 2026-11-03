import factory
from django.contrib.gis.geos import Point
from factory.django import DjangoModelFactory

from .models import Area, City, CityVisibility

KARACHI = (67.0011, 24.8607)  # lng, lat


class CityFactory(DjangoModelFactory):
    class Meta:
        model = City

    name = factory.Sequence(lambda n: f"City {n}")
    slug = factory.Sequence(lambda n: f"city-{n}")
    centre = Point(*KARACHI, srid=4326)
    visibility = CityVisibility.PUBLIC


class AreaFactory(DjangoModelFactory):
    class Meta:
        model = Area

    city = factory.SubFactory(CityFactory)
    name = factory.Sequence(lambda n: f"Area {n}")
    slug = factory.Sequence(lambda n: f"area-{n}")
    centre = Point(*KARACHI, srid=4326)
