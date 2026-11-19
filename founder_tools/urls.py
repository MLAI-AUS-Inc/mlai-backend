from django.urls import path

from .my_startup.handoff import CreateStartupHandoffView
from .views import (
    FounderToolsActiveCompanyView,
    FounderToolsBootstrapView,
    FounderToolsCompanyDetailView,
    FounderToolsCompanyMonthlyUpdatesView,
    FounderToolsCompanyView,
    FounderToolsProfileView,
)
from .website_lead_reminders import opt_out_view as website_lead_opt_out
from .website_leads import FounderWebsiteLeadView
from .website_preflight import FounderWebsitePreflightView


urlpatterns = [
    path("my-startup-handoff/", CreateStartupHandoffView.as_view(), name="my-startup-handoff"),
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
    path("website-lead/", FounderWebsiteLeadView.as_view(), name="founder-tools-website-lead"),
    path("website-preflight/", FounderWebsitePreflightView.as_view(), name="founder-tools-website-preflight"),
    path("website-lead/opt-out/", website_lead_opt_out, name="founder-tools-website-lead-opt-out"),
]
