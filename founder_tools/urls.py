from django.urls import path

from .views import (
    FounderToolsActiveCompanyView,
    FounderToolsBootstrapView,
    FounderToolsCompanyView,
    FounderToolsProfileView,
)


urlpatterns = [
    path("bootstrap/", FounderToolsBootstrapView.as_view(), name="founder-tools-bootstrap"),
    path("profile/", FounderToolsProfileView.as_view(), name="founder-tools-profile"),
    path("companies/", FounderToolsCompanyView.as_view(), name="founder-tools-companies"),
    path("active-company/", FounderToolsActiveCompanyView.as_view(), name="founder-tools-active-company"),
]
