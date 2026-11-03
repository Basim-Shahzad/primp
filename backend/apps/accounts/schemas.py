import uuid

from ninja import Schema

from .models import User


class TokenOut(Schema):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    refresh_token: str | None = None


class RefreshIn(Schema):
    refresh_token: str | None = None


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
