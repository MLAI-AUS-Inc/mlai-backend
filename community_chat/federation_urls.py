from django.urls import path
from .federation_views import (FederationRequestView, FederationAuthorizeView,
                               FederationTokenView, FederationEligibilityView)
urlpatterns = [
    path('requests/', FederationRequestView.as_view()),
    path('authorize/', FederationAuthorizeView.as_view()),
    path('token/', FederationTokenView.as_view()),
    path('eligibility/', FederationEligibilityView.as_view()),
]
