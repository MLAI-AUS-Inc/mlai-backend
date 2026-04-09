from django.urls import path
from . import api_views
from . import api_views_bridge
from . import api_views_startup_updates

urlpatterns = [
    # GitHub Integration
    path('github/', api_views.GithubTokenIdentityView.as_view(), name='github_integration_list'),  # POST create/update
    path('github/<str:slack_user_id>/', api_views.GithubTokenIdentityView.as_view(), name='github_integration_detail'), # GET, PATCH
    path('github/auth-url', api_views.GithubAuthUrlView.as_view(), name='github_auth_url'),
    path('github/refresh', api_views.GithubTokenRefreshView.as_view(), name='github_token_refresh'),  # POST refresh token
    path('github/reauth-url', api_views.GithubReauthUrlView.as_view(), name='github_reauth_url'),  # GET quick re-auth URL
    path('github/scan', api_views.GithubScanView.as_view(), name='github_scan'),
    path('github/scaffold', api_views.GithubScaffoldView.as_view(), name='github_scaffold'),
    path('github/scaffold/decision', api_views.GithubScaffoldDecisionView.as_view(), name='github_scaffold_decision'),
    
    # Pending Intents
    path('pending-intent/', api_views.IntentView.as_view(), name='pending_intent_list'), # POST save
    path('pending-intent/<str:slack_user_id>/', api_views.IntentView.as_view(), name='pending_intent_detail'), # DELETE clear

    # Legacy / Aliases (keeping them for now as they were in recent plans/usage)
    path('github/token', api_views.GithubTokenIdentityView.as_view(), name='github_token_legacy'),
    path('intent', api_views.IntentView.as_view(), name='integration_intent_legacy'),
    path('status', api_views.StatusView.as_view(), name='integration_status_legacy'),

    # Startup updates / investor memo workflow
    path('startup-updates/profile', api_views_startup_updates.StartupProfileView.as_view(), name='startup_updates_profile'),
    path('startup-updates/run', api_views_startup_updates.StartupUpdateRunView.as_view(), name='startup_updates_run'),
    path('startup-updates/active-run', api_views_startup_updates.StartupUpdateActiveRunView.as_view(), name='startup_updates_active_run'),
    path('startup-updates/open-runs', api_views_startup_updates.StartupUpdateOpenRunsView.as_view(), name='startup_updates_open_runs'),
    path('startup-updates/runs/<str:run_id>/status', api_views_startup_updates.StartupUpdateRunStatusView.as_view(), name='startup_updates_run_status'),
    path('startup-updates/runs/<str:run_id>/ingest-next-page', api_views_startup_updates.StartupUpdateIngestNextPageView.as_view(), name='startup_updates_ingest_next_page'),
    path('startup-updates/runs/<str:run_id>/hydration-candidates', api_views_startup_updates.StartupUpdateHydrationCandidatesView.as_view(), name='startup_updates_hydration_candidates'),
    path('startup-updates/runs/<str:run_id>/hydrate-threads', api_views_startup_updates.StartupUpdateHydrateThreadsView.as_view(), name='startup_updates_hydrate_threads'),
    path('startup-updates/runs/<str:run_id>/classification-batch', api_views_startup_updates.StartupUpdateClassificationBatchView.as_view(), name='startup_updates_classification_batch'),
    path('startup-updates/runs/<str:run_id>/classification-results', api_views_startup_updates.StartupUpdateClassificationResultsView.as_view(), name='startup_updates_classification_results'),
    path('startup-updates/runs/<str:run_id>/extraction-batch', api_views_startup_updates.StartupUpdateExtractionBatchView.as_view(), name='startup_updates_extraction_batch'),
    path('startup-updates/runs/<str:run_id>/extraction-results', api_views_startup_updates.StartupUpdateExtractionResultsView.as_view(), name='startup_updates_extraction_results'),
    path('startup-updates/runs/<str:run_id>/timeline', api_views_startup_updates.StartupUpdateTimelineView.as_view(), name='startup_updates_timeline'),
    path('startup-updates/runs/<str:run_id>/draft-results', api_views_startup_updates.StartupUpdateDraftResultsView.as_view(), name='startup_updates_draft_results'),
    path('startup-updates/drafts', api_views_startup_updates.StartupUpdateDraftListView.as_view(), name='startup_updates_draft_list'),
    path('startup-updates/drafts/<int:draft_id>', api_views_startup_updates.StartupUpdateDraftDetailView.as_view(), name='startup_updates_draft_detail'),

    # Community bridge
    path('bridge/slack/events', api_views_bridge.SlackCommunityBridgeEventView.as_view(), name='community_bridge_slack_events'),
]
