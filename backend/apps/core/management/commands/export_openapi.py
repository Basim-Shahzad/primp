"""Print the OpenAPI schema as stable, sorted JSON (for `make openapi` and the staleness test)."""

import json
from typing import Any

from django.core.management.base import BaseCommand


def render_schema() -> str:
    from config.api import api

    return json.dumps(api.get_openapi_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class Command(BaseCommand):
    help = "Write the OpenAPI schema (JSON) to stdout"

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write(render_schema(), ending="")
