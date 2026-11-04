"""Public business imagery: logo and the photo gallery (several sizes, metadata stripped)."""

import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError, conflict, not_found, validation_error
from apps.core.images import ProcessedImage, process_image

from . import selectors
from .invalidation import touch
from .models import Business, BusinessPhoto

MAX_PHOTOS = 20
SIZES = {"": 1600, "_480": 480, "_160": 160}  # suffix -> longest edge


def _store(prefix: str, data: bytes) -> str:
    """Save the image at every size. Returns the path of the full-size copy."""
    base = f"{prefix}/{uuid.uuid4().hex}"
    main: str | None = None
    for suffix, edge in SIZES.items():
        image: ProcessedImage = process_image(data, max_dimension=edge)
        path = default_storage.save(f"{base}{suffix}.{image.ext}", ContentFile(image.content))
        if suffix == "":
            main = path
    assert main is not None
    return str(main)


def variant_path(path: str, suffix: str) -> str:
    stem, dot, ext = path.rpartition(".")
    return f"{stem}{suffix}{dot}{ext}"


def _delete_all(path: str) -> None:
    for suffix in SIZES:
        default_storage.delete(variant_path(path, suffix))


def photo_urls(path: str) -> dict[str, str]:
    return {
        "url": str(default_storage.url(path)),
        "medium": str(default_storage.url(variant_path(path, "_480"))),
        "thumb": str(default_storage.url(variant_path(path, "_160"))),
    }


def set_logo(owner_id: int, business_ref: str, data: bytes) -> Business:
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        old = business.logo
        business.logo = _store(f"businesses/{business.public_id}/logo", data)
        business.save(update_fields=["logo", "updated_at"])
        if old:
            transaction.on_commit(lambda: _delete_all(old))
    touch(business.pk)
    return business


def add_photos(owner_id: int, business_ref: str, files: list[bytes]) -> list[BusinessPhoto]:
    if not files:
        raise validation_error({"files": [str(_("Upload at least one image."))]})
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        existing = business.photos.count()
        if existing + len(files) > MAX_PHOTOS:
            raise AppError(
                "too_many_photos",
                _("A business can have at most 20 photos."),
                422,
                {"files": [str(_("Limit is 20 photos."))]},
            )
        created: list[BusinessPhoto] = []
        has_cover = business.photos.filter(is_cover=True).exists()
        last = business.photos.order_by("-sort_order").values_list("sort_order", flat=True).first()
        next_order = (last or 0) + 1
        for i, data in enumerate(files):
            path = _store(f"businesses/{business.public_id}", data)
            photo = BusinessPhoto.objects.create(
                business=business,
                image=path,
                sort_order=next_order + i,
                is_cover=not has_cover and i == 0,
            )
            created.append(photo)
    touch(business.pk)
    return created


def _owned_photo(owner_id: int, business_ref: str, photo_id: int) -> tuple[Business, BusinessPhoto]:
    business = selectors.owned_business(owner_id, business_ref, lock=True)
    photo = business.photos.filter(pk=photo_id).first()
    if photo is None:
        raise not_found()
    return business, photo


def reorder(owner_id: int, business_ref: str, ids: list[int]) -> list[BusinessPhoto]:
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        current = set(business.photos.values_list("pk", flat=True))
        if len(ids) != len(set(ids)) or set(ids) != current:
            raise validation_error({"photo_ids": [str(_("Send every photo id exactly once."))]})
        for position, photo_id in enumerate(ids):
            BusinessPhoto.objects.filter(pk=photo_id).update(sort_order=position)
        photos = list(business.photos.all())
    touch(business.pk)
    return photos


def make_cover(owner_id: int, business_ref: str, photo_id: int) -> BusinessPhoto:
    with transaction.atomic():
        business, photo = _owned_photo(owner_id, business_ref, photo_id)
        business.photos.filter(is_cover=True).exclude(pk=photo.pk).update(is_cover=False)
        photo.is_cover = True
        photo.save(update_fields=["is_cover", "updated_at"])
    touch(business.pk)
    return photo


def delete_photo(owner_id: int, business_ref: str, photo_id: int) -> None:
    with transaction.atomic():
        business, photo = _owned_photo(owner_id, business_ref, photo_id)
        remaining = business.photos.exclude(pk=photo.pk).count()
        if remaining == 0 and business.is_listed:
            raise conflict("last_photo", _("A listed business must keep at least one photo."))
        path, was_cover = photo.image, photo.is_cover
        photo.delete()
        if was_cover:
            nxt = business.photos.order_by("sort_order", "id").first()
            if nxt is not None:
                nxt.is_cover = True
                nxt.save(update_fields=["is_cover", "updated_at"])
        transaction.on_commit(lambda: _delete_all(path))
    touch(business.pk)
