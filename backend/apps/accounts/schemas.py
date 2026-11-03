import uuid
from typing import Annotated

from ninja import Field, Schema
from pydantic import AfterValidator, EmailStr, StringConstraints

from .models import User
from .phone import normalize_pk_mobile

PKPhone = Annotated[str, AfterValidator(normalize_pk_mobile), Field(max_length=20)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Email = Annotated[EmailStr, Field(max_length=254)]
Password = Annotated[str, Field(min_length=1, max_length=128)]


class TokenOut(Schema):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    refresh_token: str | None = None
    is_new_user: bool | None = None


class RefreshIn(Schema):
    refresh_token: str | None = None


class RegisterIn(Schema):
    name: Name
    email: Email
    password: Password


class LoginIn(Schema):
    email: Email
    password: Password


class TokenIn(Schema):
    token: str = Field(min_length=1, max_length=200)


class OTPRequestIn(Schema):
    phone: PKPhone


class OTPVerifyIn(Schema):
    phone: PKPhone
    code: str = Field(pattern=r"^\d{6}$")
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""


class GoogleIn(Schema):
    id_token: str = Field(min_length=1, max_length=4096)


class PasswordResetRequestIn(Schema):
    email: Email


class PasswordResetConfirmIn(Schema):
    token: str = Field(min_length=1, max_length=200)
    new_password: Password


class MessageOut(Schema):
    detail: str


class OTPRequestOut(Schema):
    expires_in: int
    resend_in: int


class UserOut(Schema):
    id: uuid.UUID
    email: str | None
    phone: str | None
    is_email_verified: bool
    is_phone_verified: bool
    role: str
    name: str
    photo_url: str | None
    referral_code: str | None

    @staticmethod
    def from_user(user: User) -> "UserOut":
        from .selectors import profile_of

        profile = profile_of(user)
        return UserOut(
            id=user.public_id,
            email=user.email,
            phone=user.phone,
            is_email_verified=user.is_email_verified,
            is_phone_verified=user.is_phone_verified,
            role=user.role,
            name=profile.name if profile else "",
            photo_url=None,
            referral_code=profile.referral_code if profile else None,
        )
