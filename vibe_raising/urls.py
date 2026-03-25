from django.urls import path

from .views import (
    VibeRaisingActiveCompanyView,
    VibeRaisingCompanyView,
    VibeRaisingProfileView,
)


urlpatterns = [
    path("profile/", VibeRaisingProfileView.as_view(), name="vibe-raising-profile"),
    path("companies/", VibeRaisingCompanyView.as_view(), name="vibe-raising-companies"),
    path("active-company/", VibeRaisingActiveCompanyView.as_view(), name="vibe-raising-active-company"),
]
