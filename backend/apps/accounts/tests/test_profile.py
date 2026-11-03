import io
from datetime import timedelta

import pytest
import time_machine
from django.core import mail
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.accounts import otp, tokens
from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import AccountDeletionRequest, DeletionStatus, User
from apps.accounts.tests.helpers import bearer, login_client, post_json
from apps.core.models import AuditLog
from apps.geo.factories import AreaFactory, CityFactory
from apps.notifications.adapters import FakeSMS

pytestmark = pytest.mark.django_db
ME = "/api/v1/users/me"


def auth_client(**kw):  # type: ignore[no-untyped-def]
    user = make_customer(**kw)
    _, session = login_client(user)
    return user, session


def patch(session, body):  # type: ignore[no-untyped-def]
    import json

    return Client().patch(
        ME, data=json.dumps(body), content_type="application/json", **bearer(session)
    )


def image_bytes(
    fmt: str = "JPEG", size=(100, 80), mode: str = "RGB", exif: bool = False, color=(200, 30, 30)
) -> bytes:  # type: ignore[no-untyped-def]
    img = Image.new(mode, size, color if mode == "RGB" else (200, 30, 30, 128))
    out = io.BytesIO()
    kwargs = {}
    if exif:
        e = Image.Exif()
        e[0x010F] = "SecretCameraMaker"
        e[0x0112] = 1
        kwargs["exif"] = e.tobytes()
    img.save(out, format=fmt, **kwargs)
    return out.getvalue()


def upload(session, data: bytes, name: str = "me.jpg", ctype: str = "image/jpeg"):  # type: ignore[no-untyped-def]
    return Client().post(
        ME + "/photo",
        {"file": SimpleUploadedFile(name, data, content_type=ctype)},
        **bearer(session),
    )


# ---- PATCH profile ----


def test_patch_name_and_location() -> None:
    area = AreaFactory()
    user, session = auth_client()
    r = patch(session, {"name": "  New Name ", "default_area_id": area.pk})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "New Name" and body["default_area_id"] == area.pk
    assert body["default_city_id"] == area.city_id
    user.profile.refresh_from_db()
    assert user.profile.default_area_id == area.pk


def test_patch_city_clears_mismatched_area() -> None:
    area, other_city = AreaFactory(), CityFactory()
    _, session = auth_client()
    patch(session, {"default_area_id": area.pk})
    r = patch(session, {"default_city_id": other_city.pk})
    assert r.json()["default_city_id"] == other_city.pk and r.json()["default_area_id"] is None


def test_patch_city_keeps_matching_area() -> None:
    area = AreaFactory()
    _, session = auth_client()
    patch(session, {"default_area_id": area.pk})
    r = patch(session, {"default_city_id": area.city_id})
    assert r.json()["default_area_id"] == area.pk


def test_patch_null_clears_location() -> None:
    area = AreaFactory()
    _, session = auth_client()
    patch(session, {"default_area_id": area.pk})
    r = patch(session, {"default_city_id": None, "default_area_id": None})
    assert r.json()["default_city_id"] is None and r.json()["default_area_id"] is None


def test_patch_area_wrong_city_422() -> None:
    area, other = AreaFactory(), CityFactory()
    _, session = auth_client()
    r = patch(session, {"default_city_id": other.pk, "default_area_id": area.pk})
    assert r.status_code == 422 and "default_area_id" in r.json()["fields"]


def test_patch_unknown_or_inactive_location_422() -> None:
    inactive = CityFactory(is_active=False)
    _, session = auth_client()
    assert patch(session, {"default_city_id": 999999}).status_code == 422
    assert patch(session, {"default_city_id": inactive.pk}).status_code == 422
    assert patch(session, {"default_area_id": 999999}).status_code == 422


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, 5])
def test_patch_name_validation(name: object) -> None:
    _, session = auth_client()
    assert patch(session, {"name": name}).status_code == 422


def test_patch_name_boundary_100() -> None:
    _, session = auth_client()
    assert patch(session, {"name": "x" * 100}).status_code == 200


def test_patch_empty_body_is_noop() -> None:
    _, session = auth_client(name="Keep")
    assert patch(session, {}).json()["name"] == "Keep"


def test_patch_anonymous_401() -> None:
    import json

    r = Client().patch(ME, data=json.dumps({"name": "x"}), content_type="application/json")
    assert r.status_code == 401


def test_patch_creates_profile_for_user_without_one() -> None:
    user = UserFactory()
    _, session = login_client(user)
    assert patch(session, {"name": "Late"}).json()["name"] == "Late"


# ---- photo ----


def test_photo_upload_resizes_and_stores() -> None:
    user, session = auth_client()
    r = upload(session, image_bytes(size=(2000, 1000)))
    assert r.status_code == 200
    user.profile.refresh_from_db()
    path = user.profile.photo
    assert path.startswith(f"profiles/{user.public_id}/") and path.endswith(".jpg")
    with default_storage.open(path) as f:
        img = Image.open(f)
        assert max(img.size) == 512 and img.size == (512, 256) and img.format == "JPEG"
    assert r.json()["photo_url"]


def test_small_image_not_upscaled() -> None:
    user, session = auth_client()
    upload(session, image_bytes(size=(64, 32)))
    user.profile.refresh_from_db()
    with default_storage.open(user.profile.photo) as f:
        assert Image.open(f).size == (64, 32)


def test_exif_stripped() -> None:
    src = image_bytes(exif=True)
    assert Image.open(io.BytesIO(src)).getexif().get(0x010F) == "SecretCameraMaker"
    user, session = auth_client()
    upload(session, src)
    user.profile.refresh_from_db()
    with default_storage.open(user.profile.photo) as f:
        raw = f.read()
    assert b"SecretCameraMaker" not in raw
    assert len(Image.open(io.BytesIO(raw)).getexif()) == 0


def test_png_with_alpha_kept_as_png() -> None:
    user, session = auth_client()
    r = upload(session, image_bytes("PNG", mode="RGBA"), "a.png", "image/png")
    assert r.status_code == 200
    user.profile.refresh_from_db()
    assert user.profile.photo.endswith(".png")
    with default_storage.open(user.profile.photo) as f:
        img = Image.open(f)
        assert img.format == "PNG" and img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 128


def test_rotation_from_exif_applied() -> None:
    img = Image.new("RGB", (200, 100), (10, 10, 10))
    e = Image.Exif()
    e[0x0112] = 6  # rotate 90
    out = io.BytesIO()
    img.save(out, "JPEG", exif=e.tobytes())
    user, session = auth_client()
    upload(session, out.getvalue())
    user.profile.refresh_from_db()
    with default_storage.open(user.profile.photo) as f:
        assert Image.open(f).size == (100, 200)


def test_replacing_photo_deletes_old_file(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, session = auth_client()
    with run_commit():
        upload(session, image_bytes())
    user.profile.refresh_from_db()
    old = user.profile.photo
    assert default_storage.exists(old)
    with run_commit():
        upload(session, image_bytes(color=(1, 2, 3)))
    user.profile.refresh_from_db()
    assert user.profile.photo != old and not default_storage.exists(old)
    assert default_storage.exists(user.profile.photo)


def test_fake_image_text_renamed_jpg_rejected() -> None:
    user, session = auth_client()
    r = upload(session, b"this is definitely not an image", "evil.jpg")
    assert r.status_code == 422 and r.json()["code"] == "invalid_image"
    user.profile.refresh_from_db()
    assert user.profile.photo == ""


def test_empty_file_rejected() -> None:
    _, session = auth_client()
    r = upload(session, b"")
    assert r.status_code in (422, 400)


def test_truncated_image_rejected() -> None:
    _, session = auth_client()
    r = upload(session, image_bytes()[:60])
    assert r.status_code == 422


def test_disallowed_format_gif_rejected() -> None:
    _, session = auth_client()
    r = upload(session, image_bytes("GIF"), "a.gif", "image/gif")
    assert r.status_code == 422 and r.json()["code"] == "invalid_image"


def test_oversize_file_413() -> None:
    _, session = auth_client()
    r = upload(session, image_bytes() + b"\0" * (5 * 1024 * 1024))
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"


def test_size_boundary_exactly_5mb_is_not_413() -> None:
    from apps.core.images import MAX_UPLOAD_BYTES, process_image

    data = image_bytes()
    padded = data + b"\0" * (MAX_UPLOAD_BYTES - len(data))
    assert len(padded) == MAX_UPLOAD_BYTES
    assert process_image(padded).ext == "jpg"  # trailing bytes ignored; not rejected for size


def test_pixel_bomb_rejected() -> None:
    _, session = auth_client()
    out = io.BytesIO()
    Image.new("L", (7000, 7000)).save(out, "PNG")
    assert out.tell() < 5 * 1024 * 1024
    assert upload(session, out.getvalue(), "b.png", "image/png").status_code == 422


def test_photo_missing_file_422() -> None:
    _, session = auth_client()
    r = Client().post(ME + "/photo", {}, **bearer(session))
    assert r.status_code == 422


def test_photo_anonymous_401() -> None:
    r = Client().post(ME + "/photo", {"file": SimpleUploadedFile("a.jpg", image_bytes())})
    assert r.status_code == 401


# ---- phone change ----

CHANGE = ME + "/phone/change"
CONFIRM = ME + "/phone/confirm"
NEW = "+923009998877"


def test_phone_change_flow(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, session = auth_client(phone="+923001111111")
    with run_commit():
        r = post_json(Client(), CHANGE, {"phone": "0300 9998877"}, **bearer(session))
    assert r.status_code == 202
    assert FakeSMS.outbox[-1].to == NEW
    user.refresh_from_db()
    assert user.phone == "+923001111111"  # old number kept until confirmed
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    r = post_json(Client(), CONFIRM, {"phone": NEW, "code": code}, **bearer(session))
    assert r.status_code == 200 and r.json()["phone"] == NEW
    user.refresh_from_db()
    assert user.phone == NEW and user.is_phone_verified
    assert AuditLog.objects.filter(action="user.phone_changed").exists()


def test_phone_change_to_number_of_other_account_409() -> None:
    UserFactory(phone=NEW)
    _, session = auth_client()
    r = post_json(Client(), CHANGE, {"phone": NEW}, **bearer(session))
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"
    assert FakeSMS.outbox == []


def test_phone_change_same_number_409() -> None:
    _, session = auth_client(phone=NEW)
    r = post_json(Client(), CHANGE, {"phone": NEW}, **bearer(session))
    assert r.status_code == 409 and r.json()["code"] == "phone_unchanged"


def test_phone_change_wrong_code_keeps_old(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, session = auth_client(phone="+923001111111")
    with run_commit():
        post_json(Client(), CHANGE, {"phone": NEW}, **bearer(session))
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    wrong = "000000" if code != "000000" else "111111"
    r = post_json(Client(), CONFIRM, {"phone": NEW, "code": wrong}, **bearer(session))
    assert r.status_code == 400
    user.refresh_from_db()
    assert user.phone == "+923001111111"


def test_phone_change_otp_cannot_be_used_for_login(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, session = auth_client()
    with run_commit():
        post_json(Client(), CHANGE, {"phone": NEW}, **bearer(session))
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    r = post_json(Client(), "/api/v1/auth/otp/verify", {"phone": NEW, "code": code})
    assert r.status_code == 400 and not User.objects.filter(phone=NEW).exists()


def test_login_otp_cannot_confirm_phone_change(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, session = auth_client()
    with run_commit():
        otp.request_otp(NEW, otp.LOGIN, None)
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    r = post_json(Client(), CONFIRM, {"phone": NEW, "code": code}, **bearer(session))
    assert r.status_code == 400


def test_phone_confirm_race_number_taken_after_request(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, session = auth_client()
    with run_commit():
        post_json(Client(), CHANGE, {"phone": NEW}, **bearer(session))
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    UserFactory(phone=NEW)
    r = post_json(Client(), CONFIRM, {"phone": NEW, "code": code}, **bearer(session))
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"


@pytest.mark.parametrize("url", [CHANGE, CONFIRM])
def test_phone_endpoints_anonymous_401(url: str) -> None:
    assert post_json(Client(), url, {"phone": NEW, "code": "123456"}).status_code == 401


def test_phone_change_invalid_number_422() -> None:
    _, session = auth_client()
    assert post_json(Client(), CHANGE, {"phone": "123"}, **bearer(session)).status_code == 422


# ---- email change ----

ECHANGE = ME + "/email/change"
ECONFIRM = ME + "/email/confirm"


def test_email_change_flow_and_token_reuse(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, session = auth_client(email="old@example.com")
    with run_commit():
        r = post_json(Client(), ECHANGE, {"email": "New@Example.com"}, **bearer(session))
    assert r.status_code == 202
    assert mail.outbox[-1].to == ["new@example.com"]
    user.refresh_from_db()
    assert user.email == "old@example.com"
    token = mail.outbox[-1].body.split("token=")[1].strip()
    r = post_json(Client(), ECONFIRM, {"token": token})
    assert r.status_code == 200 and r.json()["email"] == "new@example.com"
    user.refresh_from_db()
    assert user.email == "new@example.com" and user.is_email_verified
    again = post_json(Client(), ECONFIRM, {"token": token})
    assert again.status_code == 400 and again.json()["code"] == "invalid_token"


def test_email_change_taken_409() -> None:
    UserFactory(email="taken@example.com")
    _, session = auth_client()
    r = post_json(Client(), ECHANGE, {"email": "taken@example.com"}, **bearer(session))
    assert r.status_code == 409 and r.json()["code"] == "email_taken"


def test_email_change_same_email_409() -> None:
    _, session = auth_client(email="me@example.com")
    r = post_json(Client(), ECHANGE, {"email": "ME@example.com"}, **bearer(session))
    assert r.status_code == 409 and r.json()["code"] == "email_unchanged"


def test_email_confirm_when_taken_in_meantime(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, session = auth_client(email="old@example.com")
    with run_commit():
        post_json(Client(), ECHANGE, {"email": "new@example.com"}, **bearer(session))
    token = mail.outbox[-1].body.split("token=")[1].strip()
    UserFactory(email="new@example.com")
    r = post_json(Client(), ECONFIRM, {"token": token})
    assert r.status_code == 409
    user.refresh_from_db()
    assert user.email == "old@example.com"


def test_email_confirm_token_expiry(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, session = auth_client()
    with run_commit():
        post_json(Client(), ECHANGE, {"email": "new@example.com"}, **bearer(session))
    token = mail.outbox[-1].body.split("token=")[1].strip()
    with time_machine.travel(timezone.now() + timedelta(hours=24, seconds=1), tick=False):
        assert post_json(Client(), ECONFIRM, {"token": token}).status_code == 400


def test_email_change_validation_and_auth() -> None:
    _, session = auth_client()
    assert post_json(Client(), ECHANGE, {"email": "bad"}, **bearer(session)).status_code == 422
    assert post_json(Client(), ECHANGE, {"email": "a@example.com"}).status_code == 401


def test_password_reset_token_not_valid_for_email_change(run_commit) -> None:  # type: ignore[no-untyped-def]
    UserFactory(email="a@example.com")
    with run_commit():
        post_json(Client(), "/api/v1/auth/password/reset/request", {"email": "a@example.com"})
    token = mail.outbox[-1].body.split("token=")[1].strip()
    assert post_json(Client(), ECONFIRM, {"token": token}).status_code == 400


# ---- deletion request ----

DEL = ME + "/deletion-request"


def test_deletion_request_records_and_logs_out_everywhere() -> None:
    user, s1 = auth_client()
    s2 = tokens.issue_session(user)
    r = post_json(Client(), DEL, {"reason": "leaving"}, **bearer(s1))
    assert r.status_code == 202 and r.json()["status"] == "pending"
    req = AccountDeletionRequest.objects.get(user=user)
    assert req.reason == "leaving" and req.status == DeletionStatus.PENDING
    assert Client().get(ME, **bearer(s1)).status_code == 401
    assert Client().get(ME, **bearer(s2)).status_code == 401
    assert AuditLog.objects.filter(action="user.deletion_requested").count() == 1
    user.refresh_from_db()
    assert user.status == "active"  # actual deletion is an admin task


def test_deletion_request_idempotent_while_pending() -> None:
    user, s1 = auth_client()
    post_json(Client(), DEL, {}, **bearer(s1))
    s2 = tokens.issue_session(user)
    r = post_json(Client(), DEL, None, **bearer(s2))
    assert r.status_code == 202
    assert AccountDeletionRequest.objects.filter(user=user).count() == 1


def test_deletion_reason_too_long_422() -> None:
    _, s = auth_client()
    assert post_json(Client(), DEL, {"reason": "x" * 501}, **bearer(s)).status_code == 422


def test_deletion_anonymous_401() -> None:
    assert post_json(Client(), DEL, {}).status_code == 401
