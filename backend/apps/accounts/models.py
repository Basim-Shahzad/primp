import secrets
from typing import Any

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdMixin, TimeStampedModel

REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Role(models.TextChoices):
    CUSTOMER = "customer", _("Customer")
    BUSINESS_OWNER = "business_owner", _("Business owner")
    STAFF = "staff", _("Staff")
    ADMIN = "admin", _("Admin")
    SUPER_ADMIN = "super_admin", _("Super admin")


ADMIN_ROLES = (Role.ADMIN, Role.SUPER_ADMIN)


class AuthProvider(models.TextChoices):
    PASSWORD = "password", _("Password")
    OTP = "otp", _("OTP")
    GOOGLE = "google", _("Google")


class UserStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    SUSPENDED = "suspended", _("Suspended")
    DELETED = "deleted", _("Deleted")


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(
        self,
        email: str | None = None,
        phone: str | None = None,
        password: str | None = None,
        **extra: Any,
    ) -> "User":
        if not email and not phone:
            raise ValueError("Either email or phone is required")
        user = self.model(
            email=self.normalize_email(email) if email else None, phone=phone, **extra
        )
        if user.email:
            user.email = user.email.lower()
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> "User":
        extra.setdefault("role", Role.SUPER_ADMIN)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_email_verified", True)
        return self.create_user(email=email, password=password, **extra)


class User(AbstractBaseUser, PermissionsMixin, PublicIdMixin, TimeStampedModel):
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=16, unique=True, null=True, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    is_email_verified = models.BooleanField(default=False)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)
    auth_provider = models.CharField(
        max_length=10, choices=AuthProvider.choices, default=AuthProvider.PASSWORD
    )
    status = models.CharField(max_length=10, choices=UserStatus.choices, default=UserStatus.ACTIVE)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []  # type: ignore[misc]

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone__isnull=False),
                name="user_email_or_phone_required",
            ),
        ]

    def __str__(self) -> str:
        return self.email or self.phone or str(self.public_id)

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.status == UserStatus.ACTIVE

    @property
    def is_staff(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_admin_role(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > timezone.now()


def generate_referral_code() -> str:
    return "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(8))


class CustomerProfile(TimeStampedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    name = models.CharField(max_length=100, blank=True)
    photo = models.CharField(max_length=255, blank=True)
    default_city = models.ForeignKey(
        "geo.City", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    default_area = models.ForeignKey(
        "geo.Area", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    referral_code = models.CharField(max_length=12, unique=True, editable=False)
    referred_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals"
    )

    def __str__(self) -> str:
        return self.name or str(self.user_id)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.referral_code:
            super().save(*args, **kwargs)
            return
        for _attempt in range(10):
            self.referral_code = generate_referral_code()
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError:
                if not self._state.adding:
                    raise
                self.referral_code = ""
        raise RuntimeError("Could not generate a unique referral code")


class RefreshToken(models.Model):
    """Server-stored refresh token (hash only). Tokens rotate on each use; a family is the
    chain of rotations starting from one login."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    family_id = models.UUIDField(db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    user_agent = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.family_id} ({self.user_id})"


class TokenPurpose(models.TextChoices):
    EMAIL_VERIFY = "email_verify", _("Email verification")
    PASSWORD_RESET = "password_reset", _("Password reset")
    EMAIL_CHANGE = "email_change", _("Email change")


class OneTimeToken(models.Model):
    """Single-use, expiring token sent by email. Only the hash is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="one_time_tokens")
    purpose = models.CharField(max_length=20, choices=TokenPurpose.choices)
    token_hash = models.CharField(max_length=64, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.purpose} for {self.user_id}"


class DeletionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    PROCESSED = "processed", _("Processed")
    CANCELLED = "cancelled", _("Cancelled")


class AccountDeletionRequest(TimeStampedModel):
    """Records a customer's request; processing is an admin task (Plan 6.3)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="deletion_requests")
    status = models.CharField(
        max_length=10, choices=DeletionStatus.choices, default=DeletionStatus.PENDING
    )
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status="pending"),
                name="one_pending_deletion_request_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} ({self.status})"


class Wishlist(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="wishlist_items")
    business = models.ForeignKey("businesses.Business", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "business"], name="wishlist_unique_user_business"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.business_id}"
