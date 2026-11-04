"""Base settings shared by every environment. All config comes from the environment."""

from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab
from kombu import Queue

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    env.read_env(str(_env_file))

DEBUG = env.bool("DJANGO_DEBUG", default=False)
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="insecure-base-key-not-for-production")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "django.contrib.postgres",
    "corsheaders",
    "django_celery_beat",
    "django_prometheus",
    "apps.core",
    "apps.geo",
    "apps.catalog",
    "apps.businesses",
    "apps.bookings",
    "apps.reviews",
    "apps.payments",
    "apps.loyalty",
    "apps.backoffice",
    "apps.accounts",
    "apps.notifications",
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": env.db("DATABASE_URL", default="postgis://primp:primp@localhost:5432/primp"),
}
DATABASES["default"]["ENGINE"] = "django.contrib.gis.db.backends.postgis"
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
BUSINESS_TIME_ZONE = "Asia/Karachi"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

_bucket = env.str("AWS_STORAGE_BUCKET_NAME", default="")
if _bucket:  # S3 / R2 / MinIO
    _default_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": _bucket,
            "endpoint_url": env.str("AWS_S3_ENDPOINT_URL", default="") or None,
            "access_key": env.str("AWS_ACCESS_KEY_ID", default=""),
            "secret_key": env.str("AWS_SECRET_ACCESS_KEY", default=""),
            "custom_domain": env.str("AWS_S3_CUSTOM_DOMAIN", default="") or None,
            "default_acl": None,
            "querystring_auth": False,
        },
    }
else:
    _default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
# Private storage for verification documents: never served by MEDIA_URL, no public URL
PRIVATE_MEDIA_ROOT = env.str("PRIVATE_MEDIA_ROOT", default=str(BASE_DIR / "private_media"))
_private_bucket = env.str("AWS_PRIVATE_BUCKET_NAME", default="")
if _private_bucket:
    _private_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": _private_bucket,
            "endpoint_url": env.str("AWS_S3_ENDPOINT_URL", default="") or None,
            "access_key": env.str("AWS_ACCESS_KEY_ID", default=""),
            "secret_key": env.str("AWS_SECRET_ACCESS_KEY", default=""),
            "default_acl": "private",
            "querystring_auth": True,
            "file_overwrite": False,
        },
    }
else:
    _private_storage = {
        "BACKEND": "apps.core.private_storage.PrivateFileSystemStorage",
        "OPTIONS": {"location": PRIVATE_MEDIA_ROOT},
    }
STORAGES = {
    "default": _default_storage,
    "private": _private_storage,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# CORS: explicit allow-list only
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
CORS_ALLOW_CREDENTIALS = True

# Email
EMAIL_BACKEND = env.str("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env.str("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="Primp <no-reply@primp.pk>")

# Celery
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_QUEUES = (Queue("critical"), Queue("default"), Queue("bulk"))
CELERY_TASK_ACKS_LATE = True
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "reminder-scan": {
        "task": "apps.notifications.tasks.scan_reminders",
        "schedule": 300.0,
        "options": {"queue": "default"},
    },
    "pending-escalation": {
        "task": "apps.bookings.tasks.escalate_pending_bookings",
        "schedule": 300.0,
        "options": {"queue": "default"},
    },
    "daily-stats-rebuild": {
        "task": "apps.bookings.tasks.rebuild_daily_stats",
        "schedule": crontab(hour=21, minute=10),  # 02:10 Asia/Karachi
        "options": {"queue": "bulk"},
    },
    "ticket-flag-sweep": {
        "task": "apps.backoffice.tasks.sweep_review_flags",
        "schedule": 300.0,
        "options": {"queue": "default"},
    },
    "loyalty-reconcile": {
        "task": "apps.loyalty.tasks.reconcile_accounts",
        "schedule": crontab(hour=21, minute=30),  # 02:30 Asia/Karachi
        "options": {"queue": "bulk"},
    },
}

# Auth / JWT
JWT_SIGNING_KEY = env.str("JWT_SIGNING_KEY", default="insecure-base-jwt-key-not-for-production")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME = timedelta(minutes=env.int("ACCESS_TOKEN_MINUTES", default=15))
ADMIN_ACCESS_TOKEN_LIFETIME = timedelta(minutes=env.int("ADMIN_ACCESS_TOKEN_MINUTES", default=5))
REFRESH_TOKEN_LIFETIME = timedelta(days=env.int("REFRESH_TOKEN_DAYS", default=30))
ADMIN_REFRESH_TOKEN_LIFETIME = timedelta(hours=env.int("ADMIN_REFRESH_TOKEN_HOURS", default=8))
REFRESH_COOKIE_NAME = env.str("REFRESH_COOKIE_NAME", default="primp_refresh")
REFRESH_COOKIE_SECURE = env.bool("REFRESH_COOKIE_SECURE", default=True)
REFRESH_COOKIE_PATH = "/api/v1/auth"
REFRESH_CSRF_COOKIE_NAME = env.str("REFRESH_CSRF_COOKIE_NAME", default="primp_csrf")
REFRESH_TOKEN_IN_BODY = env.bool("REFRESH_TOKEN_IN_BODY", default=False)

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "apps.core.logging.RequestIDFilter"}},
    "formatters": {"json": {"()": "apps.core.logging.JSONFormatter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_id"],
        }
    },
    "root": {"handlers": ["console"], "level": env.str("LOG_LEVEL", default="INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}

SMS_ADAPTER = env.str("SMS_ADAPTER", default="console")
FRONTEND_URL = env.str("FRONTEND_URL", default="http://localhost:3000")
GOOGLE_CLIENT_IDS = env.list("GOOGLE_CLIENT_IDS", default=[])
BOOKING_HOLD_SECONDS = env.int("BOOKING_HOLD_SECONDS", default=300)
FRONTEND_REVALIDATE_URL = env.str("FRONTEND_REVALIDATE_URL", default="")
FRONTEND_REVALIDATE_SECRET = env.str("FRONTEND_REVALIDATE_SECRET", default="")
PAYMENT_GATEWAY = env.str("PAYMENT_GATEWAY", default="fake")
PAYMENT_WEBHOOK_SECRET = env.str("PAYMENT_WEBHOOK_SECRET", default="dev-webhook-secret")
REMINDER_OFFSETS = {
    "reminder_24h": env.int("REMINDER_24H_SECONDS", default=24 * 3600),
    "reminder_2h": env.int("REMINDER_2H_SECONDS", default=2 * 3600),
}
REMINDER_GRACE_SECONDS = env.int("REMINDER_GRACE_SECONDS", default=1800)
NOTIFICATIONS_RAISE_ON_RENDER_ERROR = env.bool("NOTIFICATIONS_RAISE_ON_RENDER_ERROR", default=False)
SMS_API_URL = env.str("SMS_API_URL", default="")
SMS_API_KEY = env.str("SMS_API_KEY", default="")
SMS_SENDER_ID = env.str("SMS_SENDER_ID", default="Primp")
LOYALTY_RAISE_ON_ERROR = env.bool("LOYALTY_RAISE_ON_ERROR", default=False)

# Provider side (Plan 6.2)
PROVIDER_TERMS_VERSION = env.str("PROVIDER_TERMS_VERSION", default="2026-01")
CITY_MAX_RADIUS_KM = env.float("CITY_MAX_RADIUS_KM", default=60.0)
PRIVATE_URL_TTL_SECONDS = env.int("PRIVATE_URL_TTL_SECONDS", default=300)
DOCUMENT_MAX_BYTES = env.int("DOCUMENT_MAX_BYTES", default=10 * 1024 * 1024)
ANALYTICS_RAISE_ON_ERROR = env.bool("ANALYTICS_RAISE_ON_ERROR", default=False)
# default ticket priority per type (Plan 6.3 moves this into platform settings)
TICKET_PRIORITIES = {
    "booking_dispute": "high",
    "refund_request": "high",
    "flagged_review": "normal",
    "provider_complaint": "normal",
    "account_issue": "normal",
    "failed_notification": "low",
    "other": "low",
}
AUTO_TICKET_LATE_CANCELLATIONS = env.bool("AUTO_TICKET_LATE_CANCELLATIONS", default=False)
DUPLICATE_DISTANCE_METERS = env.int("DUPLICATE_DISTANCE_METERS", default=30)
DUPLICATE_NAME_SIMILARITY = env.float("DUPLICATE_NAME_SIMILARITY", default=0.5)
PENDING_ESCALATION_MINUTES = env.int("PENDING_ESCALATION_MINUTES", default=120)
PENDING_AUTO_CANCEL_MINUTES = env.int("PENDING_AUTO_CANCEL_MINUTES", default=720)
PROVIDER_COMPLETE_TOLERANCE_MINUTES = env.int("PROVIDER_COMPLETE_TOLERANCE_MINUTES", default=15)
PROVIDER_NO_SHOW_GRACE_MINUTES = env.int("PROVIDER_NO_SHOW_GRACE_MINUTES", default=15)
THROTTLE_ANON = env.str("THROTTLE_ANON", default="120/min")
THROTTLE_USER = env.str("THROTTLE_USER", default="600/min")
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)
SENTRY_DSN = env.str("SENTRY_DSN", default="")
LOG_REDACT_PII = env.bool("LOG_REDACT_PII", default=True)
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=True)

# GDAL blocking of file/network access needs GDAL >= 3.9; the API never opens
# user-supplied spatial references, so the distro GDAL is acceptable.
SILENCED_SYSTEM_CHECKS = ["gis.W001"]
