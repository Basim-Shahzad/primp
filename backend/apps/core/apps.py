from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        from django.conf import settings

        from .logging import install_redaction

        if settings.LOG_REDACT_PII:
            install_redaction()
        if settings.SENTRY_DSN:  # pragma: no cover - needs a real DSN
            import sentry_sdk

            sentry_sdk.init(dsn=settings.SENTRY_DSN, send_default_pii=False, traces_sample_rate=0.0)
