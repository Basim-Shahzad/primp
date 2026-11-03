from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-secret-key-test-secret-key-test-secret-key-12345"
JWT_SIGNING_KEY = "test-jwt-signing-key-test-jwt-signing-key-12345"
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
REDIS_URL = env.str("TEST_REDIS_URL", default="redis://localhost:6379/15")  # noqa: F405
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}
CELERY_BROKER_URL = env.str("TEST_CELERY_BROKER_URL", default="redis://localhost:6379/14")  # noqa: F405
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
MEDIA_ROOT = BASE_DIR / "media_test"  # noqa: F405
LOGGING["root"]["level"] = "WARNING"  # type: ignore[index]  # noqa: F405
THROTTLE_ANON = "100000/min"
THROTTLE_USER = "100000/min"
