from typing import Annotated, Any, Literal

from ninja import Field, Schema
from pydantic import StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Visibility = Literal["hidden", "preview", "public"]


class CityIn(Schema):
    name: Name
    slug: Slug | None = None
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    visibility: Visibility = "hidden"
    is_active: bool = True


class CityPatchIn(Schema):
    version: int = Field(ge=1)
    name: Name | None = None
    slug: Slug | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    visibility: Visibility | None = None
    is_active: bool | None = None
    force: bool = False


class CityOut(Schema):
    id: int
    name: str
    slug: str
    lat: float
    lng: float
    visibility: str
    is_active: bool
    version: int
    area_count: int
    listed_businesses: int


class AreaIn(Schema):
    name: Name
    slug: Slug | None = None
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    polygon: dict[str, Any] | None = None
    is_pilot: bool = False
    is_active: bool = True


class AreaPatchIn(Schema):
    version: int = Field(ge=1)
    name: Name | None = None
    slug: Slug | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    polygon: dict[str, Any] | None = None
    is_pilot: bool | None = None
    is_active: bool | None = None
    force: bool = False


class AreaOut(Schema):
    id: int
    city_id: int
    name: str
    slug: str
    lat: float
    lng: float
    polygon: dict[str, Any] | None
    is_pilot: bool
    is_active: bool
    version: int
    listed_businesses: int


class ImportOut(Schema):
    imported: int
