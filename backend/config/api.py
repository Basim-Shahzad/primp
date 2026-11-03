from django.conf import settings
from ninja import NinjaAPI
from ninja.throttling import AnonRateThrottle, AuthRateThrottle

from apps.core.errors import register_exception_handlers

docs_on = settings.API_DOCS_ENABLED

api = NinjaAPI(
    title="Primp API",
    version="1.0",
    urls_namespace="api-v1",
    docs_url="/docs" if docs_on else None,
    openapi_url="/openapi.json" if docs_on else None,
    throttle=[
        AnonRateThrottle(settings.THROTTLE_ANON),
        AuthRateThrottle(settings.THROTTLE_USER),
    ],
)
register_exception_handlers(api)

from apps.accounts.api import auth_router, users_router  # noqa: E402

api.add_router("/auth", auth_router)
api.add_router("/users", users_router)

from apps.bookings.api import businesses_router as availability_router  # noqa: E402

api.add_router("/businesses", availability_router)
