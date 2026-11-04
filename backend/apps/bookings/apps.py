from django.apps import AppConfig


class BookingsConfig(AppConfig):
    name = "apps.bookings"
    label = "bookings"

    def ready(self) -> None:
        from . import analytics_receivers, signals  # noqa: F401

        analytics_receivers.connect()
