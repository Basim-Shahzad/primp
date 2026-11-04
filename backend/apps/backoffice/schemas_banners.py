from datetime import datetime
from typing import Annotated, Literal

from ninja import Field, Schema
from pydantic import AwareDatetime, StringConstraints

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Subtitle = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Link = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class BannerIn(Schema):
    title: Title
    subtitle: Subtitle = ""
    link_url: Link
    city_id: int | None = None
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class BannerPatchIn(Schema):
    version: int = Field(ge=1)
    title: Title | None = None
    subtitle: Subtitle | None = None
    link_url: Link | None = None
    city_id: int | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    is_enabled: bool | None = None


class BannerOut(Schema):
    id: int
    title: str
    subtitle: str
    link_url: str
    city_id: int | None
    starts_at: datetime
    ends_at: datetime
    is_enabled: bool
    sort_order: int
    desktop_image_url: str | None
    mobile_image_url: str | None
    impressions: int
    clicks: int
    status: str
    version: int


class BannerQuery(Schema):
    status: Literal["active", "scheduled", "expired", "disabled"] | None = None
    city_id: int | None = None


class ReorderIn(Schema):
    ids: list[int] = Field(max_length=200)


class PublicBannerOut(Schema):
    id: int
    title: str
    subtitle: str
    desktop_image_url: str
    mobile_image_url: str
    link_url: str
