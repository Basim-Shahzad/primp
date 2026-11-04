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


def register_routers() -> None:
    """Mount every app router. Imports are local so apps load before their routers."""
    from apps.accounts import api as accounts_api
    from apps.backoffice import (
        api_admin,
        api_banners,
        api_config,
        api_geo,
        api_listings,
        api_points,
        api_taxonomy,
        api_tickets,
        api_users,
    )
    from apps.bookings import api as bookings_api
    from apps.bookings import api_provider as bookings_provider
    from apps.businesses import api as businesses_api
    from apps.businesses import api_provider as businesses_provider
    from apps.catalog import api as catalog_api
    from apps.loyalty import api as loyalty_api
    from apps.notifications import api_provider as notifications_provider
    from apps.payments import api as payments_api
    from apps.reviews import api as reviews_api

    api.add_router("/auth", accounts_api.auth_router)
    api.add_router("/users", accounts_api.users_router)
    api.add_router("/users", loyalty_api.users_router)
    api.add_router("/users", bookings_api.users_router)
    api.add_router("/search", businesses_api.search_router)
    api.add_router("/categories", catalog_api.router)
    api.add_router("/businesses", businesses_api.businesses_router)
    api.add_router("/businesses", bookings_api.businesses_router)
    api.add_router("/businesses", reviews_api.businesses_router)
    api.add_router("/appointments", bookings_api.appointments_router)
    api.add_router("/appointments", reviews_api.appointments_router)
    api.add_router("/reviews", reviews_api.reviews_router)
    api.add_router("/payments", payments_api.router)
    api.add_router("/provider", businesses_provider.provider_router)
    api.add_router("/provider", bookings_provider.provider_router)
    api.add_router("/provider", notifications_provider.provider_router)
    api.add_router("/admin", api_admin.router)
    api.add_router("/admin", api_users.router)
    api.add_router("/admin", api_listings.router)
    api.add_router("/admin", api_taxonomy.router)
    api.add_router("/admin", api_geo.router)
    api.add_router("/admin", api_tickets.router)
    api.add_router("/admin", api_banners.router)
    api.add_router("/admin", api_points.router)
    api.add_router("/admin", api_config.router)
    api.add_router("/banners", api_banners.public_router)
    api.add_router("/tickets", api_tickets.user_router)
    api.add_router("/files", businesses_provider.files_router)


register_routers()
