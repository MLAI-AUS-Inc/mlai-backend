from django.urls import path

from .views import (
    FounderToolsActiveCompanyView,
    FounderToolsBootstrapView,
    FounderToolsCompanyDetailView,
    FounderToolsCompanyMonthlyUpdatesView,
    FounderToolsCompanyView,
    FounderToolsProfileView,
)


urlpatterns = [
    path("bootstrap/", FounderToolsBootstrapView.as_view(), name="founder-tools-bootstrap"),
    path("profile/", FounderToolsProfileView.as_view(), name="founder-tools-profile"),
    path("companies/", FounderToolsCompanyView.as_view(), name="founder-tools-companies"),
    path(
        "companies/<uuid:company_id>/",
        FounderToolsCompanyDetailView.as_view(),
        name="founder-tools-company-detail",
    ),
    path(
        "companies/<uuid:company_id>/monthly-updates/",
        FounderToolsCompanyMonthlyUpdatesView.as_view(),
        name="founder-tools-company-monthly-updates",
    ),
    path("active-company/", FounderToolsActiveCompanyView.as_view(), name="founder-tools-active-company"),
]
