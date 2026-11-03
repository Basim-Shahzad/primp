"""Adapter pattern: an interface, named implementations and a settings-selected registry."""

from typing import Any, Generic, TypeVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

A = TypeVar("A")


class AdapterRegistry(Generic[A]):
    def __init__(self, kind: str, setting_name: str) -> None:
        self.kind = kind
        self.setting_name = setting_name
        self._classes: dict[str, type[A]] = {}

    def register(self, name: str) -> Any:
        def decorator(cls: type[A]) -> type[A]:
            self._classes[name] = cls
            return cls

        return decorator

    def names(self) -> list[str]:
        return sorted(self._classes)

    def get(self, name: str | None = None) -> A:
        name = name or getattr(settings, self.setting_name)
        try:
            return self._classes[name]()
        except KeyError:
            raise ImproperlyConfigured(
                f"Unknown {self.kind} adapter '{name}'. Available: {', '.join(self.names())}"
            ) from None
