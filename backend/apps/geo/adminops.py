"""City and area management for the backoffice."""

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from django.contrib.gis.geos import GEOSGeometry, Point, Polygon
from django.db.models import Count, QuerySet
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError, conflict, not_found, validation_error

from .models import Area, City

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_VERTICES = 5000
MAX_CSV_BYTES = 1024 * 1024
MAX_CSV_ROWS = 2000
CSV_COLUMNS = ("name", "slug", "lat", "lng", "is_pilot")
TRUE = {"true", "1", "yes", "y"}
FALSE = {"false", "0", "no", "n", ""}


def city_snapshot(c: City) -> dict[str, Any]:
    return {
        "name": c.name,
        "slug": c.slug,
        "lat": c.centre.y,
        "lng": c.centre.x,
        "visibility": c.visibility,
        "is_active": c.is_active,
        "version": c.version,
    }


def area_snapshot(a: Area) -> dict[str, Any]:
    return {
        "name": a.name,
        "slug": a.slug,
        "lat": a.centre.y,
        "lng": a.centre.x,
        "is_pilot": a.is_pilot,
        "is_active": a.is_active,
        "has_polygon": a.polygon is not None,
        "version": a.version,
    }


def list_cities() -> QuerySet[City]:
    return City.objects.annotate(area_count=Count("areas")).order_by("name", "id")


def get_city(city_id: int, *, lock: bool = False) -> City:
    qs = City.objects.select_for_update(of=("self",)) if lock else City.objects
    city = qs.filter(pk=city_id).first()
    if city is None:
        raise not_found()
    return city


def get_area(city_id: int, area_id: int, *, lock: bool = False) -> Area:
    qs = Area.objects.select_for_update(of=("self",)) if lock else Area.objects
    area = qs.filter(pk=area_id, city_id=city_id).select_related("city").first()
    if area is None:
        raise not_found()
    return area


def areas_of(city_id: int) -> list[Area]:
    return list(Area.objects.filter(city_id=city_id).order_by("name", "id"))


def _point(lat: float, lng: float) -> Point:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise validation_error({"lat": [str(_("Coordinates are out of range."))]})
    return Point(lng, lat, srid=4326)


def _slug(slug: str | None, name: str, taken: Any, exclude_pk: int | None = None) -> str:
    if slug is None:
        base, n = (slugify(name) or "item")[:45], 1
        final = base
        while taken(final, exclude_pk):
            n += 1
            final = f"{base}-{n}"
        return final
    if not SLUG_RE.match(slug) or len(slug) > 50:
        raise validation_error({"slug": [str(_("Use lowercase letters, digits and hyphens."))]})
    if taken(slug, exclude_pk):
        raise AppError("slug_taken", _("This slug is already used."), 409, {"slug": ["In use."]})
    return slug


def _city_slug_taken(slug: str, exclude_pk: int | None) -> bool:
    qs = City.objects.filter(slug=slug)
    return (qs.exclude(pk=exclude_pk) if exclude_pk else qs).exists()


def parse_polygon(geojson: dict[str, Any]) -> Polygon:
    """A valid GeoJSON Polygon (closed rings, in range, GEOS-valid) or a 422."""

    def bad(message: Any) -> AppError:
        return validation_error({"polygon": [str(message)]})

    if geojson.get("type") != "Polygon":
        raise bad(_("The polygon must be a GeoJSON Polygon."))
    try:
        geometry = GEOSGeometry(json.dumps(geojson), srid=4326)
    except Exception as exc:  # GEOS/GDAL raise several unrelated types for malformed input
        raise bad(_("The polygon is not valid GeoJSON.")) from exc
    if not isinstance(geometry, Polygon) or geometry.empty:
        raise bad(_("The polygon is empty."))
    if geometry.num_coords > MAX_VERTICES:
        raise bad(_("The polygon has too many points."))
    minx, miny, maxx, maxy = geometry.extent
    if not (minx >= -180 and maxx <= 180 and miny >= -90 and maxy <= 90):
        raise bad(_("Polygon coordinates are out of range."))
    if not geometry.valid:
        raise bad(_("The polygon is not valid (self-intersecting?)."))
    return geometry


# ---------------------------------------------------------------- cities


def create_city(
    *, name: str, slug: str | None, lat: float, lng: float, visibility: str, is_active: bool
) -> City:
    return City.objects.create(
        name=name,
        slug=_slug(slug, name, _city_slug_taken),
        centre=_point(lat, lng),
        visibility=visibility,
        is_active=is_active,
    )


def _stale(version: int, current: int) -> None:
    if version != current:
        raise conflict("stale_version", _("The record changed; reload it and try again."))


def update_city(city: City, version: int, changes: dict[str, Any]) -> City:
    _stale(version, city.version)
    if "slug" in changes:
        city.slug = _slug(changes["slug"], city.name, _city_slug_taken, city.pk)
    for field in ("name", "visibility", "is_active"):
        if field in changes:
            setattr(city, field, changes[field])
    if "lat" in changes or "lng" in changes:
        city.centre = _point(changes.get("lat", city.centre.y), changes.get("lng", city.centre.x))
    city.version += 1
    city.save()
    return city


# ---------------------------------------------------------------- areas


def _area_slug_taken(city_id: int) -> Any:
    def taken(slug: str, exclude_pk: int | None) -> bool:
        qs = Area.objects.filter(city_id=city_id, slug=slug)
        return (qs.exclude(pk=exclude_pk) if exclude_pk else qs).exists()

    return taken


def create_area(
    city: City,
    *,
    name: str,
    slug: str | None,
    lat: float,
    lng: float,
    polygon: dict[str, Any] | None,
    is_pilot: bool,
    is_active: bool,
) -> Area:
    return Area.objects.create(
        city=city,
        name=name,
        slug=_slug(slug, name, _area_slug_taken(city.pk)),
        centre=_point(lat, lng),
        polygon=parse_polygon(polygon) if polygon is not None else None,
        is_pilot=is_pilot,
        is_active=is_active,
    )


def update_area(area: Area, version: int, changes: dict[str, Any]) -> Area:
    _stale(version, area.version)
    if "slug" in changes:
        area.slug = _slug(changes["slug"], area.name, _area_slug_taken(area.city_id), area.pk)
    for field in ("name", "is_pilot", "is_active"):
        if field in changes:
            setattr(area, field, changes[field])
    if "lat" in changes or "lng" in changes:
        area.centre = _point(changes.get("lat", area.centre.y), changes.get("lng", area.centre.x))
    if "polygon" in changes:
        area.polygon = parse_polygon(changes["polygon"]) if changes["polygon"] is not None else None
    area.version += 1
    area.save()
    return area


# ---------------------------------------------------------------- CSV import


@dataclass(frozen=True)
class RowError:
    row: int
    message: str


def _flag(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    return None


def validate_csv(city: City, data: bytes) -> tuple[list[dict[str, Any]], list[RowError]]:
    """Parse and validate every row. Row numbers count the header as row 1."""
    if len(data) > MAX_CSV_BYTES:
        raise AppError("file_too_large", _("The CSV is larger than 1 MB."), 413)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AppError("invalid_csv", _("The file must be UTF-8 text."), 422) from exc
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in CSV_COLUMNS if c not in header]
    if missing:
        raise AppError(
            "invalid_csv",
            _("Missing columns: %(cols)s.") % {"cols": ", ".join(missing)},
            422,
            {"columns": missing},
        )
    existing = set(Area.objects.filter(city=city).values_list("slug", flat=True))
    seen: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    errors: list[RowError] = []
    for number, raw in enumerate(reader, start=2):
        if number - 1 > MAX_CSV_ROWS:
            raise AppError("invalid_csv", _("At most 2000 rows per file."), 422)
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        problems: list[str] = []
        name, slug = row.get("name", ""), row.get("slug", "")
        if not name or len(name) > 100:
            problems.append("name must be 1-100 characters")
        if not SLUG_RE.match(slug) or len(slug) > 50:
            problems.append("slug must be lowercase letters, digits and hyphens (max 50)")
        elif slug in existing:
            problems.append(f"slug '{slug}' already exists in this city")
        elif slug in seen:
            problems.append(f"slug '{slug}' repeats row {seen[slug]}")
        else:
            seen[slug] = number
        lat = lng = None
        try:
            lat, lng = float(row.get("lat", "")), float(row.get("lng", ""))
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
        except ValueError:
            problems.append("lat and lng must be valid coordinates")
        pilot = _flag(row.get("is_pilot", ""))
        if pilot is None:
            problems.append("is_pilot must be true/false")
        if problems:
            errors.extend(RowError(number, p) for p in problems)
        else:
            rows.append({"name": name, "slug": slug, "lat": lat, "lng": lng, "is_pilot": pilot})
    if not rows and not errors:
        raise AppError("invalid_csv", _("The file has no rows."), 422)
    return rows, errors


def import_areas(city: City, rows: list[dict[str, Any]]) -> list[Area]:
    return Area.objects.bulk_create(
        [
            Area(
                city=city,
                name=r["name"],
                slug=r["slug"],
                centre=Point(r["lng"], r["lat"], srid=4326),
                is_pilot=r["is_pilot"],
            )
            for r in rows
        ]
    )
