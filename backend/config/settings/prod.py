from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

REQUIRED_ENV = [
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DATABASE_URL",
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "JWT_SIGNING_KEY",
    "CORS_ALLOWED_ORIGINS",
]
_missing = [name for name in REQUIRED_ENV if not env.str(name, default="")]  # noqa: F405
if _missing:
    raise ImproperlyConfigured(
        "Missing required environment variables for production: " + ", ".join(_missing)
    )

DEBUG = False
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=False)  # noqa: F405

SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
