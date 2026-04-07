from __future__ import annotations

from django.contrib import admin
from django.urls import include, path
from webui.dashboard.views_health import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check),
    path("", include("webui.dashboard.urls")),
]
