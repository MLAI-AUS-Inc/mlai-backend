from django.urls import path

from .views import (
    VibeRaisingActiveCompanyView,
    VibeRaisingCompanyView,
    VibeRaisingProfileView,
    VibeRaisingStartupUpdateBootstrapView,
    VibeRaisingStartupUpdateRunView,
    VibeRaisingStartupUpdateStatusView,
)


urlpatterns = [
    path("profile/", VibeRaisingProfileView.as_view(), name="vibe-raising-profile"),
    path("companies/", VibeRaisingCompanyView.as_view(), name="vibe-raising-companies"),
    path("active-company/", VibeRaisingActiveCompanyView.as_view(), name="vibe-raising-active-company"),
    path(
        "startup-update/bootstrap/",
        VibeRaisingStartupUpdateBootstrapView.as_view(),
        name="vibe-raising-startup-update-bootstrap",
    ),
    path(
        "startup-update/run/",
        VibeRaisingStartupUpdateRunView.as_view(),
        name="vibe-raising-startup-update-run",
    ),
    path(
        "startup-update/status/",
        VibeRaisingStartupUpdateStatusView.as_view(),
        name="vibe-raising-startup-update-status",
    ),
]
