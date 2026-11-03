from django.apps import AppConfig


class ReviewsConfig(AppConfig):
    name = "apps.reviews"
    label = "reviews"

    def ready(self) -> None:
        from . import signals  # noqa: F401
