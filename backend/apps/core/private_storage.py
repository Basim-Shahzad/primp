"""Storage backends for private files: they never expose a direct URL."""

from typing import Any

from django.core.files.storage import FileSystemStorage, InMemoryStorage


class NoPublicUrlMixin:
    def url(self, name: str | None) -> str:
        raise ValueError("Private files are not publicly addressable; use a signed download link.")


class PrivateFileSystemStorage(NoPublicUrlMixin, FileSystemStorage):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.pop("base_url", None)
        super().__init__(*args, **kwargs)


class PrivateInMemoryStorage(NoPublicUrlMixin, InMemoryStorage):
    pass
