import pytest
from django.test import Client

from apps.accounts import otp
from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import CustomerProfile, User
from apps.accounts.roles import Role
from apps.accounts.tests.helpers import bearer, login_client, post_json
from apps.core.models import AuditLog
from apps.notifications.adapters import FakeSMS

pytestmark = pytest.mark.django_db
OTP_REQ = "/api/v1/auth/otp/request"
REG = "/api/v1/auth/register/business-owner"
BECOME = "/api/v1/users/me/become-owner"
PHONE = "+923001234567"


def code_for(run_commit, phone: str = "03001234567") -> str:  # type: ignore[no-untyped-def]
    with run_commit():
        assert post_json(Client(), OTP_REQ, {"phone": phone}).status_code == 202
    return FakeSMS.outbox[-1].body.split("code is ")[1][:6]


def body(otp_code: str, **kw: object) -> dict[str, object]:
    return {
        "name": "Sana Khan",
        "phone": "03001234567",
        "code": otp_code,
        "email": "Sana@Example.com",
        "password": "Str0ng-pass-phrase!",
        **kw,
    }


def test_register_owner_success(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = code_for(run_commit)
    r = post_json(Client(), REG, body(code))
    assert r.status_code == 201, r.content
    assert r.json()["access_token"]
    user = User.objects.get(phone=PHONE)
    assert user.role == Role.BUSINESS_OWNER and user.is_phone_verified
    assert user.email == "sana@example.com" and not user.is_email_verified
    assert user.check_password("Str0ng-pass-phrase!")
    assert CustomerProfile.objects.get(user=user).name == "Sana Khan"
    assert AuditLog.objects.filter(action="user.owner_registered", actor_id=user.pk).exists()
    me = Client().get(
        "/api/v1/users/me", **{"HTTP_AUTHORIZATION": f"Bearer {r.json()['access_token']}"}
    )
    assert me.json()["role"] == "business_owner"


def test_register_owner_wrong_code_creates_nothing(run_commit) -> None:  # type: ignore[no-untyped-def]
    code_for(run_commit)
    r = post_json(Client(), REG, body("000000"))
    assert r.status_code in (400, 422) and r.json()["code"]
    assert not User.objects.filter(phone=PHONE).exists()


def test_register_owner_code_is_single_use(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = code_for(run_commit)
    assert post_json(Client(), REG, body(code)).status_code == 201
    r = post_json(Client(), REG, body(code, phone="03009998888", email="x@example.com"))
    assert r.status_code in (400, 422)


def test_register_owner_phone_taken(run_commit) -> None:  # type: ignore[no-untyped-def]
    UserFactory(phone=PHONE)
    code = code_for(run_commit)
    r = post_json(Client(), REG, body(code))
    assert r.status_code == 409 and r.json()["code"] == "phone_taken"


def test_register_owner_email_taken_keeps_phone_free(run_commit) -> None:  # type: ignore[no-untyped-def]
    UserFactory(email="sana@example.com")
    code = code_for(run_commit)
    r = post_json(Client(), REG, body(code))
    assert r.status_code == 409 and r.json()["code"] == "email_taken"
    assert not User.objects.filter(phone=PHONE).exists()


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"name": ""}, "name"),
        ({"name": "n" * 101}, "name"),
        ({"phone": "123"}, "phone"),
        ({"code": "12"}, "code"),
        ({"email": "nope"}, "email"),
        ({"password": ""}, "password"),
        ({"password": "short"}, "password"),
    ],
)
def test_register_owner_validation(run_commit, bad: dict[str, object], field: str) -> None:  # type: ignore[no-untyped-def]
    code = code_for(run_commit)
    r = post_json(Client(), REG, body(code, **bad))
    assert r.status_code == 422 and field in r.json()["fields"], r.content


def test_register_owner_missing_fields() -> None:
    r = post_json(Client(), REG, {})
    assert r.status_code == 422 and {"name", "phone", "code", "email", "password"} <= set(
        r.json()["fields"]
    )


def test_register_owner_ip_throttled(run_commit) -> None:  # type: ignore[no-untyped-def]
    codes = []
    for i in range(21):
        r = post_json(Client(), REG, body("000000", email=f"a{i}@example.com"))
        codes.append(r.status_code)
    assert codes[-1] == 429


# ---- become owner


def test_customer_becomes_owner_and_keeps_customer_abilities() -> None:
    user = make_customer()
    _, session = login_client(user)
    r = Client().post(BECOME, **bearer(session))
    assert r.status_code == 200 and r.json()["role"] == "business_owner"
    user.refresh_from_db()
    assert user.role == Role.BUSINESS_OWNER
    audit = AuditLog.objects.get(action="user.became_owner")
    assert audit.before == {"role": "customer"} and audit.after == {"role": "business_owner"}
    # still a normal account: customer endpoints keep working with the same token
    assert Client().get("/api/v1/users/me/appointments", **bearer(session)).status_code == 200


def test_become_owner_is_idempotent() -> None:
    user = UserFactory(role=Role.BUSINESS_OWNER)
    _, session = login_client(user)
    assert Client().post(BECOME, **bearer(session)).status_code == 200
    assert not AuditLog.objects.filter(action="user.became_owner").exists()


def test_become_owner_requires_verified_phone() -> None:
    user = UserFactory(is_phone_verified=False)
    _, session = login_client(user)
    r = Client().post(BECOME, **bearer(session))
    assert r.status_code == 422 and r.json()["code"] == "phone_not_verified"
    user.refresh_from_db()
    assert user.role == Role.CUSTOMER


def test_become_owner_without_phone_rejected() -> None:
    user = UserFactory(phone=None)
    _, session = login_client(user)
    assert Client().post(BECOME, **bearer(session)).status_code == 422


def test_admin_cannot_become_owner() -> None:
    user = UserFactory(role=Role.ADMIN)
    _, session = login_client(user)
    assert Client().post(BECOME, **bearer(session)).status_code == 403


def test_become_owner_requires_auth() -> None:
    assert Client().post(BECOME).status_code == 401
    assert Client().post(BECOME, HTTP_AUTHORIZATION="Bearer bad").status_code == 401


def test_otp_module_exposes_login_purpose() -> None:
    assert otp.LOGIN == "login"
