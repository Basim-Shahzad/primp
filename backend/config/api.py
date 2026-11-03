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

from apps.businesses.api import search_router  # noqa: E402

api.add_router("/search", search_router)

from apps.businesses.api import businesses_router  # noqa: E402

api.add_router("/businesses", businesses_router)

from apps.reviews.api import businesses_router as reviews_router  # noqa: E402

api.add_router("/businesses", reviews_router)

from apps.bookings.api import appointments_router  # noqa: E402

api.add_router("/appointments", appointments_router)

from apps.payments.api import router as payments_router  # noqa: E402

api.add_router("/payments", payments_router)

from apps.reviews.api import appointments_router as review_appointments_router  # noqa: E402
from apps.reviews.api import reviews_router  # noqa: E402

api.add_router("/appointments", review_appointments_router)
api.add_router("/reviews", reviews_router)

from apps.loyalty.api import users_router as loyalty_users_router  # noqa: E402

api.add_router("/users", loyalty_users_router)
