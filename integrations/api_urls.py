from django.urls import path
from . import api_views

urlpatterns = [
    # GitHub Integration
    path('github/', api_views.GithubTokenIdentityView.as_view(), name='github_integration_list'),  # POST create/update
    path('github/<str:slack_user_id>/', api_views.GithubTokenIdentityView.as_view(), name='github_integration_detail'), # GET, PATCH
    path('github/auth-url', api_views.GithubAuthUrlView.as_view(), name='github_auth_url'),
    path('github/scan', api_views.GithubScanView.as_view(), name='github_scan'),
    
    # Pending Intents
    path('pending-intent/', api_views.IntentView.as_view(), name='pending_intent_list'), # POST save
    path('pending-intent/<str:slack_user_id>/', api_views.IntentView.as_view(), name='pending_intent_detail'), # DELETE clear

    # Legacy / Aliases (keeping them for now as they were in recent plans/usage)
    path('github/token', api_views.GithubTokenIdentityView.as_view(), name='github_token_legacy'),
    path('intent', api_views.IntentView.as_view(), name='integration_intent_legacy'),
    path('status', api_views.StatusView.as_view(), name='integration_status_legacy'),
]
