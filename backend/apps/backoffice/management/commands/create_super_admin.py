"""Create (or promote) the first super admin. Credentials come from the environment or a prompt.

Environment: ADMIN_EMAIL, ADMIN_PASSWORD (and optionally ADMIN_NAME). The password is never
logged or echoed.
"""

import getpass
import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import CustomerProfile, User
from apps.accounts.roles import Role
from apps.accounts.services import validate_password
from apps.core import audit
from apps.core.errors import AppError


class Command(BaseCommand):
    help = "Create a super admin account (credentials from ADMIN_EMAIL/ADMIN_PASSWORD or a prompt)."

    def handle(self, *args: Any, **options: Any) -> None:
        from django.contrib.auth.models import Group

        email = (os.environ.get("ADMIN_EMAIL") or input("Email: ")).strip().lower()
        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
        name = os.environ.get("ADMIN_NAME", "Super Admin")
        if not email or not password:
            raise CommandError("Email and password are required.")
        if User.objects.filter(email=email).exists():
            raise CommandError("An account with this email already exists.")
        try:
            validate_password(password, User(email=email))
        except AppError as exc:
            raise CommandError("; ".join(exc.fields.get("password", ["Weak password."]))) from exc
        user = User.objects.create_user(
            email=email,
            password=password,
            role=Role.SUPER_ADMIN,
            is_email_verified=True,
            is_superuser=False,
        )
        CustomerProfile.objects.create(user=user, name=name)
        user.groups.add(Group.objects.get(name="super_admin"))
        audit.record("admin.super_admin_created", target_type="user", target_id=user.pk)
        self.stdout.write(f"Super admin created: {email}")
