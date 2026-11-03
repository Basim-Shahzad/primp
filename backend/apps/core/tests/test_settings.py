import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings

BACKEND_DIR = Path(__file__).resolve().parents[3]

PROD_ENV = {
    "DJANGO_SECRET_KEY": "k" * 60 + "!Zx9#Qw",
    "DJANGO_ALLOWED_HOSTS": "api.primp.pk",
    "DATABASE_URL": "postgis://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "JWT_SIGNING_KEY": "j" * 64,
    "CORS_ALLOWED_ORIGINS": "https://primp.pk",
}


def run_manage(
    settings_module: str, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    full_env = {
        k: v for k, v in os.environ.items() if k not in PROD_ENV and not k.startswith("DJANGO_")
    }
    full_env.update(env)
    full_env["DJANGO_SETTINGS_MODULE"] = settings_module
    return subprocess.run(  # noqa: S603
        [sys.executable, "manage.py", *args],
        cwd=BACKEND_DIR,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_test_settings_loaded() -> None:
    assert settings.USE_TZ is True
    assert settings.TIME_ZONE == "UTC"
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.DATABASES["default"]["ENGINE"] == "django.contrib.gis.db.backends.postgis"


def test_argon2_is_first_hasher_in_base() -> None:
    from config.settings import base

    assert base.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")


def test_celery_queues_defined() -> None:
    names = {q.name for q in settings.CELERY_TASK_QUEUES}
    assert names == {"critical", "default", "bulk"}


def test_dev_settings_load() -> None:
    result = run_manage("config.settings.dev", {}, "check")
    assert result.returncode == 0, result.stderr


def test_prod_missing_env_fails_fast_with_clear_error() -> None:
    result = run_manage("config.settings.prod", {}, "check")
    assert result.returncode != 0
    assert "Missing required environment variables" in result.stderr
    assert "DJANGO_SECRET_KEY" in result.stderr


@pytest.mark.parametrize("missing", sorted(PROD_ENV))
def test_prod_each_required_var_is_enforced(missing: str) -> None:
    env = {k: v for k, v in PROD_ENV.items() if k != missing}
    result = run_manage("config.settings.prod", env, "check")
    assert result.returncode != 0
    assert missing in result.stderr


def test_prod_check_deploy_passes() -> None:
    result = run_manage(
        "config.settings.prod", PROD_ENV, "check", "--deploy", "--fail-level", "WARNING"
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_prod_docs_disabled_by_default() -> None:
    result = run_manage(
        "config.settings.prod",
        PROD_ENV,
        "shell",
        "-c",
        "from django.conf import settings; print(settings.API_DOCS_ENABLED)",
    )
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_request_id_header_added(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/nonexistent/", HTTP_X_REQUEST_ID="abc123")
    assert response["X-Request-ID"] == "abc123"
    response = client.get("/nonexistent/")
    assert len(response["X-Request-ID"]) == 32
