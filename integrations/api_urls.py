from django.urls import path
from . import api_views

urlpatterns = [
    path('github/token', api_views.GithubTokenIdentityView.as_view(), name='github_token'),
    path('intent', api_views.IntentView.as_view(), name='integration_intent'),
    path('status', api_views.StatusView.as_view(), name='integration_status'),
]
