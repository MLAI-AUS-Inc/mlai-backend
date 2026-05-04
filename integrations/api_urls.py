from django.urls import path
from . import api_views
from . import api_views_bridge
from . import api_views_connectors
from . import api_views_luma
from startup_updates import api_views as startup_update_api_views

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
    path('luma/attendee-report', api_views_luma.LumaAttendeeReportView.as_view(), name='luma_attendee_report'),
    
    # Pending Intents
    path('pending-intent/', api_views.IntentView.as_view(), name='pending_intent_list'), # POST save
    path('pending-intent/<str:slack_user_id>/', api_views.IntentView.as_view(), name='pending_intent_detail'), # DELETE clear

    # Legacy / Aliases (keeping them for now as they were in recent plans/usage)
    path('github/token', api_views.GithubTokenIdentityView.as_view(), name='github_token_legacy'),
    path('intent', api_views.IntentView.as_view(), name='integration_intent_legacy'),
    path('status', api_views.StatusView.as_view(), name='integration_status_legacy'),

    # Founder data-source connectors
    path('sources/status', api_views_connectors.ConnectorSourcesStatusView.as_view(), name='connector_sources_status'),
    path('sources/sync', api_views_connectors.ConnectorSourcesSyncView.as_view(), name='connector_sources_sync'),
    path(
        'sources/connections/<int:connection_id>',
        api_views_connectors.ConnectorSourceConnectionDetailView.as_view(),
        name='connector_source_connection_detail',
    ),
    path('financial/status', api_views_connectors.FinancialSourcesStatusView.as_view(), name='financial_sources_status'),
    path('financial/sync', api_views_connectors.FinancialSourcesSyncView.as_view(), name='financial_sources_sync'),
    path(
        'financial/bank-feed/accounts',
        api_views_connectors.BankFeedAccountListView.as_view(),
        name='financial_bank_feed_accounts',
    ),
    path(
        'financial/bank-feed/transactions',
        api_views_connectors.BankFeedTransactionListView.as_view(),
        name='financial_bank_feed_transactions',
    ),
    path(
        'financial/xero/preview',
        api_views_connectors.XeroPreviewView.as_view(),
        name='financial_xero_preview',
    ),
    path(
        'financial/xero/invoices',
        api_views_connectors.XeroInvoiceListView.as_view(),
        name='financial_xero_invoices',
    ),
    path(
        'gmail/preview',
        api_views_connectors.GmailPreviewView.as_view(),
        name='gmail_preview',
    ),
    path(
        'slack/channels',
        api_views_connectors.SlackChannelListView.as_view(),
        name='slack_channels',
    ),
    path(
        'slack/channel-selections',
        api_views_connectors.SlackChannelSelectionView.as_view(),
        name='slack_channel_selections',
    ),
    path(
        'slack/preview',
        api_views_connectors.SlackPreviewView.as_view(),
        name='slack_preview',
    ),
    path(
        'linear/projects',
        api_views_connectors.LinearProjectListView.as_view(),
        name='linear_projects',
    ),
    path(
        'linear/project-selections',
        api_views_connectors.LinearProjectSelectionView.as_view(),
        name='linear_project_selections',
    ),
    path(
        'linear/preview',
        api_views_connectors.LinearPreviewView.as_view(),
        name='linear_preview',
    ),
    path(
        'financial/connections/<int:connection_id>',
        api_views_connectors.ConnectorSourceConnectionDetailView.as_view(),
        name='financial_source_connection_detail',
    ),

    # Startup updates / investor memo workflow
    path('startup-updates/profile', startup_update_api_views.StartupProfileView.as_view(), name='startup_updates_profile'),
    path('startup-updates/run', startup_update_api_views.StartupUpdateRunView.as_view(), name='startup_updates_run'),
    path('startup-updates/active-run', startup_update_api_views.StartupUpdateActiveRunView.as_view(), name='startup_updates_active_run'),
    path('startup-updates/open-runs', startup_update_api_views.StartupUpdateOpenRunsView.as_view(), name='startup_updates_open_runs'),
    path('startup-updates/runs/<str:run_id>/status', startup_update_api_views.StartupUpdateRunStatusView.as_view(), name='startup_updates_run_status'),
    path('startup-updates/runs/<str:run_id>/ingest-next-page', startup_update_api_views.StartupUpdateIngestNextPageView.as_view(), name='startup_updates_ingest_next_page'),
    path('startup-updates/runs/<str:run_id>/hydration-candidates', startup_update_api_views.StartupUpdateHydrationCandidatesView.as_view(), name='startup_updates_hydration_candidates'),
    path('startup-updates/runs/<str:run_id>/hydrate-threads', startup_update_api_views.StartupUpdateHydrateThreadsView.as_view(), name='startup_updates_hydrate_threads'),
    path('startup-updates/runs/<str:run_id>/classification-batch', startup_update_api_views.StartupUpdateClassificationBatchView.as_view(), name='startup_updates_classification_batch'),
    path('startup-updates/runs/<str:run_id>/classification-results', startup_update_api_views.StartupUpdateClassificationResultsView.as_view(), name='startup_updates_classification_results'),
    path('startup-updates/runs/<str:run_id>/extraction-batch', startup_update_api_views.StartupUpdateExtractionBatchView.as_view(), name='startup_updates_extraction_batch'),
    path('startup-updates/runs/<str:run_id>/extraction-results', startup_update_api_views.StartupUpdateExtractionResultsView.as_view(), name='startup_updates_extraction_results'),
    path('startup-updates/runs/<str:run_id>/slack/backfill', startup_update_api_views.StartupUpdateSlackBackfillView.as_view(), name='startup_updates_slack_backfill'),
    path('startup-updates/runs/<str:run_id>/slack/classification-batch', startup_update_api_views.StartupUpdateSlackClassificationBatchView.as_view(), name='startup_updates_slack_classification_batch'),
    path('startup-updates/runs/<str:run_id>/slack/classification-results', startup_update_api_views.StartupUpdateSlackClassificationResultsView.as_view(), name='startup_updates_slack_classification_results'),
    path('startup-updates/runs/<str:run_id>/slack/extraction-batch', startup_update_api_views.StartupUpdateSlackExtractionBatchView.as_view(), name='startup_updates_slack_extraction_batch'),
    path('startup-updates/runs/<str:run_id>/slack/extraction-results', startup_update_api_views.StartupUpdateSlackExtractionResultsView.as_view(), name='startup_updates_slack_extraction_results'),
    path('startup-updates/runs/<str:run_id>/linear/backfill', startup_update_api_views.StartupUpdateLinearBackfillView.as_view(), name='startup_updates_linear_backfill'),
    path('startup-updates/runs/<str:run_id>/linear/classification-batch', startup_update_api_views.StartupUpdateLinearClassificationBatchView.as_view(), name='startup_updates_linear_classification_batch'),
    path('startup-updates/runs/<str:run_id>/linear/classification-results', startup_update_api_views.StartupUpdateLinearClassificationResultsView.as_view(), name='startup_updates_linear_classification_results'),
    path('startup-updates/runs/<str:run_id>/linear/extraction-batch', startup_update_api_views.StartupUpdateLinearExtractionBatchView.as_view(), name='startup_updates_linear_extraction_batch'),
    path('startup-updates/runs/<str:run_id>/linear/extraction-results', startup_update_api_views.StartupUpdateLinearExtractionResultsView.as_view(), name='startup_updates_linear_extraction_results'),
    path('startup-updates/runs/<str:run_id>/timeline', startup_update_api_views.StartupUpdateTimelineView.as_view(), name='startup_updates_timeline'),
    path('startup-updates/runs/<str:run_id>/draft-results', startup_update_api_views.StartupUpdateDraftResultsView.as_view(), name='startup_updates_draft_results'),
    path('startup-updates/drafts', startup_update_api_views.StartupUpdateDraftListView.as_view(), name='startup_updates_draft_list'),
    path('startup-updates/drafts/<int:draft_id>', startup_update_api_views.StartupUpdateDraftDetailView.as_view(), name='startup_updates_draft_detail'),

    # Community bridge
    path('bridge/slack/events', api_views_bridge.SlackCommunityBridgeEventView.as_view(), name='community_bridge_slack_events'),
]
