from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core.health import healthz, readyz
from config.api import api

urlpatterns = [
    path(settings.ADMIN_URL_PATH, admin.site.urls),
    path("healthz", healthz),
    path("readyz", readyz),
    path("api/v1/", api.urls),
    path("", include("django_prometheus.urls")),
]
