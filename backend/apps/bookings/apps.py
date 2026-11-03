from django.apps import AppConfig


class BookingsConfig(AppConfig):
    name = "apps.bookings"
    label = "bookings"

    def ready(self) -> None:
        from . import signals  # noqa: F401
