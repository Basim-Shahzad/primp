"""Test helpers for admin accounts."""

from django.contrib.auth.models import Group

from apps.accounts.factories import CustomerProfileFactory, UserFactory
from apps.accounts.models import User
from apps.accounts.roles import Role

KINDS = ("support_agent", "ops_admin", "super_admin")


def make_admin(kind: str = "ops_admin", **kwargs: object) -> User:
    """An admin account in the named permission group (super_admin also gets the role)."""
    role = Role.SUPER_ADMIN if kind == "super_admin" else Role.ADMIN
    user = UserFactory(role=role, **kwargs)
    CustomerProfileFactory(user=user, name=f"Admin {kind}")
    user.groups.add(Group.objects.get(name=kind))
    return user
