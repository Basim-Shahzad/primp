from .base import *  # noqa: F403

DEBUG = env.bool("DJANGO_DEBUG", default=True)  # noqa: F405
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver", "web"]
CORS_ALLOWED_ORIGINS = env.list(  # noqa: F405
    "CORS_ALLOWED_ORIGINS", default=["http://localhost:3000"]
)
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_EAGER", default=False)  # noqa: F405
LOG_REDACT_PII = env.bool("LOG_REDACT_PII", default=False)  # noqa: F405
