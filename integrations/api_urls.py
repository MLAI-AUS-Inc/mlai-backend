from django.urls import path
from . import api_views
from . import api_views_bridge
from . import api_views_connectors
from . import api_views_finance
from . import api_views_luma
from . import api_views_reconciliation
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
    path('reconciliation/report', api_views_reconciliation.ReconciliationReportView.as_view(), name='reconciliation_report'),
    path('reconciliation/profile', api_views_reconciliation.ReconciliationProfileView.as_view(), name='reconciliation_profile'),
    path('reconciliation/mappings', api_views_reconciliation.ReconciliationMappingView.as_view(), name='reconciliation_mappings'),
    path('reconciliation/enrichment-context', api_views_reconciliation.ReconciliationEnrichmentContextView.as_view(), name='reconciliation_enrichment_context'),
    path('reconciliation/statement-lines', api_views_reconciliation.ReconciliationStatementLineListView.as_view(), name='reconciliation_statement_lines'),
    path('reconciliation/statement-scans', api_views_reconciliation.ReconciliationStatementScanView.as_view(), name='reconciliation_statement_scans'),
    path('reconciliation/party-identities', api_views_reconciliation.ReconciliationPartyIdentityView.as_view(), name='reconciliation_party_identities'),
    path('reconciliation/rules', api_views_reconciliation.ReconciliationRuleListView.as_view(), name='reconciliation_rules'),
    path('reconciliation/rules/<int:rule_id>', api_views_reconciliation.ReconciliationRuleDetailView.as_view(), name='reconciliation_rule_detail'),
    path('reconciliation/decisions', api_views_reconciliation.ReconciliationDecisionListView.as_view(), name='reconciliation_decisions'),
    path('reconciliation/outcomes', api_views_reconciliation.ReconciliationOutcomeView.as_view(), name='reconciliation_outcomes'),
    path('reconciliation/knowledge-export', api_views_reconciliation.ReconciliationKnowledgeExportView.as_view(), name='reconciliation_knowledge_export'),
    path('reconciliation/learning-candidates/<str:candidate_id>', api_views_reconciliation.ReconciliationLearningCandidateView.as_view(), name='reconciliation_learning_candidate'),
    path('reconciliation/readiness', api_views_reconciliation.ReconciliationReadinessView.as_view(), name='reconciliation_readiness'),
    path('reconciliation/agent-runs', api_views_reconciliation.ReconciliationAgentRunView.as_view(), name='reconciliation_agent_runs'),
    path('reconciliation/agent-runs/<str:run_id>', api_views_reconciliation.ReconciliationAgentRunDetailView.as_view(), name='reconciliation_agent_run_detail'),
    path('reconciliation/agent-runs/<str:run_id>/retry', api_views_reconciliation.ReconciliationAgentRunRetryView.as_view(), name='reconciliation_agent_run_retry'),
    path('reconciliation/agent-runs/<str:run_id>/preview', api_views_reconciliation.ReconciliationAgentRunPreviewView.as_view(), name='reconciliation_agent_run_preview'),
    path('reconciliation/agent-runs/<str:run_id>/decisions', api_views_reconciliation.ReconciliationAgentRunDecisionView.as_view(), name='reconciliation_agent_run_decisions'),
    path('reconciliation/agent-runs/<str:run_id>/execute', api_views_reconciliation.ReconciliationAgentRunExecuteView.as_view(), name='reconciliation_agent_run_execute'),
    path('reconciliation/statement-suggestions/<int:suggestion_id>/preview', api_views_reconciliation.ReconciliationStatementSuggestionPreviewView.as_view(), name='reconciliation_statement_suggestion_preview'),
    path('reconciliation/statement-suggestions/<int:suggestion_id>/execute', api_views_reconciliation.ReconciliationStatementSuggestionExecuteView.as_view(), name='reconciliation_statement_suggestion_execute'),
    path('reconciliation/statement-suggestions/execute-safe', api_views_reconciliation.ReconciliationStatementSafeBatchView.as_view(), name='reconciliation_statement_safe_batch'),
    path('reconciliation/draft-bills', api_views_reconciliation.ReconciliationDraftBillView.as_view(), name='reconciliation_draft_bills'),
    path('reconciliation/xero-attachments', api_views_reconciliation.ReconciliationXeroAttachmentView.as_view(), name='reconciliation_xero_attachments'),
    path('reconciliation/suggestions/<int:suggestion_id>/decision', api_views_reconciliation.ReconciliationSuggestionDecisionView.as_view(), name='reconciliation_suggestion_decision'),
    path('reconciliation/payouts', api_views_reconciliation.ReconciliationPayoutListView.as_view(), name='reconciliation_payouts'),
    path(
        'reconciliation/cashflow-report',
        api_views_reconciliation.ReconciliationProfitabilityReportView.as_view(),
        name='reconciliation_cashflow_report',
    ),
    path(
        'reconciliation/event-finance-audit',
        api_views_reconciliation.ReconciliationEventFinanceAuditView.as_view(),
        name='reconciliation_event_finance_audit',
    ),
    path('reconciliation/payouts/correction-preview', api_views_reconciliation.ReconciliationPayoutCorrectionPreviewView.as_view(), name='reconciliation_payout_correction_preview'),
    path('reconciliation/payouts/<str:payout_id>/preview', api_views_reconciliation.ReconciliationPayoutPreviewView.as_view(), name='reconciliation_payout_preview'),
    path('reconciliation/payouts/<str:payout_id>/post', api_views_reconciliation.ReconciliationPayoutPostView.as_view(), name='reconciliation_payout_post'),
    path(
        'reconciliation/humanitix/payouts',
        api_views_reconciliation.HumanitixPayoutListView.as_view(),
        name='reconciliation_humanitix_payouts',
    ),
    path(
        'reconciliation/humanitix/status',
        api_views_reconciliation.HumanitixStatusView.as_view(),
        name='reconciliation_humanitix_status',
    ),
    path(
        'reconciliation/humanitix/sync',
        api_views_reconciliation.HumanitixSyncView.as_view(),
        name='reconciliation_humanitix_sync',
    ),
    path(
        'reconciliation/humanitix/events',
        api_views_reconciliation.HumanitixEventAggregateView.as_view(),
        name='reconciliation_humanitix_events',
    ),
    path(
        'reconciliation/humanitix/receipts/import',
        api_views_reconciliation.HumanitixReceiptImportView.as_view(),
        name='reconciliation_humanitix_receipt_import',
    ),
    path(
        'reconciliation/humanitix/payouts/correction-preview',
        api_views_reconciliation.HumanitixPayoutCorrectionPreviewView.as_view(),
        name='reconciliation_humanitix_payout_correction_preview',
    ),
    path(
        'reconciliation/humanitix/payouts/import',
        api_views_reconciliation.HumanitixPayoutImportView.as_view(),
        name='reconciliation_humanitix_payout_import',
    ),
    path(
        'reconciliation/humanitix/payouts/<str:payout_reference>/preview',
        api_views_reconciliation.HumanitixPayoutPreviewView.as_view(),
        name='reconciliation_humanitix_payout_preview',
    ),
    path(
        'reconciliation/humanitix/payouts/<str:payout_reference>/post',
        api_views_reconciliation.HumanitixPayoutPostView.as_view(),
        name='reconciliation_humanitix_payout_post',
    ),

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
    path('luma/connect', api_views_connectors.LumaConnectView.as_view(), name='luma_connect'),
    path(
        'humanitix/connect',
        api_views_connectors.HumanitixConnectView.as_view(),
        name='humanitix_connect',
    ),
    path('luma/events', api_views_connectors.LumaEventListView.as_view(), name='luma_events'),
    path('luma/selections', api_views_connectors.LumaSelectionView.as_view(), name='luma_selections'),
    path('financial/status', api_views_finance.FinancialStatusView.as_view(), name='financial_sources_status'),
    path('financial/sync', api_views_finance.FinancialSyncView.as_view(), name='financial_sources_sync'),
    path(
        'financial/connections/<int:connection_id>',
        api_views_finance.FinancialConnectionDetailView.as_view(),
        name='financial_source_connection_detail',
    ),
    path('financial/stripe/webhook', api_views_finance.StripeFinancialWebhookView.as_view(), name='financial_stripe_webhook'),
    path('financial/runs', api_views_finance.FinancialRunCreateView.as_view(), name='financial_run_create'),
    path(
        'financial/runs/<str:run_id>/sync-next-page',
        api_views_finance.FinancialRunSyncNextPageView.as_view(),
        name='financial_run_sync_next_page',
    ),
    path(
        'financial/runs/<str:run_id>/calculate-monthly-revenue',
        api_views_finance.FinancialRunCalculateView.as_view(),
        name='financial_run_calculate',
    ),
    path(
        'financial/runs/<str:run_id>/revenue-snapshots',
        api_views_finance.FinancialRunSnapshotsView.as_view(),
        name='financial_run_snapshots',
    ),
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
        'gmail/connection',
        api_views_connectors.GmailConnectionDetailView.as_view(),
        name='gmail_connection_detail',
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
        'google-analytics/properties',
        api_views_connectors.GoogleAnalyticsPropertyListView.as_view(),
        name='google_analytics_properties',
    ),
    path(
        'google-analytics/property-selections',
        api_views_connectors.GoogleAnalyticsPropertySelectionView.as_view(),
        name='google_analytics_property_selections',
    ),
    path(
        'linear/meeting-context',
        api_views_connectors.LinearMeetingContextView.as_view(),
        name='linear_meeting_context',
    ),
    path(
        'linear/issues',
        api_views_connectors.LinearMeetingIssueCreateView.as_view(),
        name='linear_meeting_issue_create',
    ),
    path(
        'linear/issues/receipts/<str:idempotency_key>',
        api_views_connectors.LinearMeetingIssueReceiptView.as_view(),
        name='linear_meeting_issue_receipt',
    ),
    path(
        'linear/projects/<str:project_id>/sizing-context',
        api_views_connectors.LinearProjectSizingContextView.as_view(),
        name='linear_project_sizing_context',
    ),
    path(
        'linear/project-updates',
        api_views_connectors.LinearMeetingProjectUpdateCreateView.as_view(),
        name='linear_meeting_project_update_create',
    ),
    # Startup updates / investor memo workflow
    path('startup-updates/profile', startup_update_api_views.StartupProfileView.as_view(), name='startup_updates_profile'),
    path('startup-updates/run', startup_update_api_views.StartupUpdateRunView.as_view(), name='startup_updates_run'),
    path('startup-updates/active-run', startup_update_api_views.StartupUpdateActiveRunView.as_view(), name='startup_updates_active_run'),
    path('startup-updates/open-runs', startup_update_api_views.StartupUpdateOpenRunsView.as_view(), name='startup_updates_open_runs'),
    path('startup-updates/monthly-dispatch-targets', startup_update_api_views.MonthlyDispatchTargetsView.as_view(), name='startup_updates_monthly_dispatch_targets'),
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
    path('startup-updates/runs/<str:run_id>/notion/backfill', startup_update_api_views.StartupUpdateNotionBackfillView.as_view(), name='startup_updates_notion_backfill'),
    path('startup-updates/runs/<str:run_id>/notion/classification-batch', startup_update_api_views.StartupUpdateNotionClassificationBatchView.as_view(), name='startup_updates_notion_classification_batch'),
    path('startup-updates/runs/<str:run_id>/notion/classification-results', startup_update_api_views.StartupUpdateNotionClassificationResultsView.as_view(), name='startup_updates_notion_classification_results'),
    path('startup-updates/runs/<str:run_id>/notion/extraction-batch', startup_update_api_views.StartupUpdateNotionExtractionBatchView.as_view(), name='startup_updates_notion_extraction_batch'),
    path('startup-updates/runs/<str:run_id>/notion/extraction-results', startup_update_api_views.StartupUpdateNotionExtractionResultsView.as_view(), name='startup_updates_notion_extraction_results'),
    path('startup-updates/runs/<str:run_id>/google-analytics/backfill', startup_update_api_views.StartupUpdateGoogleAnalyticsBackfillView.as_view(), name='startup_updates_google_analytics_backfill'),
    path('startup-updates/runs/<str:run_id>/google-analytics/classification-batch', startup_update_api_views.StartupUpdateGoogleAnalyticsClassificationBatchView.as_view(), name='startup_updates_google_analytics_classification_batch'),
    path('startup-updates/runs/<str:run_id>/google-analytics/classification-results', startup_update_api_views.StartupUpdateGoogleAnalyticsClassificationResultsView.as_view(), name='startup_updates_google_analytics_classification_results'),
    path('startup-updates/runs/<str:run_id>/google-analytics/extraction-batch', startup_update_api_views.StartupUpdateGoogleAnalyticsExtractionBatchView.as_view(), name='startup_updates_google_analytics_extraction_batch'),
    path('startup-updates/runs/<str:run_id>/google-analytics/extraction-results', startup_update_api_views.StartupUpdateGoogleAnalyticsExtractionResultsView.as_view(), name='startup_updates_google_analytics_extraction_results'),
    path('startup-updates/runs/<str:run_id>/timeline', startup_update_api_views.StartupUpdateTimelineView.as_view(), name='startup_updates_timeline'),
    path('startup-updates/runs/<str:run_id>/curation-context', startup_update_api_views.StartupUpdateCurationContextView.as_view(), name='startup_updates_curation_context'),
    path('startup-updates/runs/<str:run_id>/curation-results', startup_update_api_views.StartupUpdateCurationResultsView.as_view(), name='startup_updates_curation_results'),
    path('startup-updates/runs/<str:run_id>/review-candidates', startup_update_api_views.StartupUpdateReviewCandidatesView.as_view(), name='startup_updates_review_candidates'),
    path('startup-updates/runs/<str:run_id>/founder-review/auto-approve', startup_update_api_views.StartupUpdateFounderReviewAutoApproveView.as_view(), name='startup_updates_founder_review_auto_approve'),
    path('startup-updates/runs/<str:run_id>/curated-timeline', startup_update_api_views.StartupUpdateCuratedTimelineView.as_view(), name='startup_updates_curated_timeline'),
    path('startup-updates/runs/<str:run_id>/draft-results', startup_update_api_views.StartupUpdateDraftResultsView.as_view(), name='startup_updates_draft_results'),
    path('startup-updates/drafts', startup_update_api_views.StartupUpdateDraftListView.as_view(), name='startup_updates_draft_list'),
    path('startup-updates/drafts/<int:draft_id>', startup_update_api_views.StartupUpdateDraftDetailView.as_view(), name='startup_updates_draft_detail'),

    # Community bridge
    path('bridge/slack/events', api_views_bridge.SlackCommunityBridgeEventView.as_view(), name='community_bridge_slack_events'),
    path('bridge/buzz/events', api_views_bridge.BuzzCommunityBridgeEventView.as_view(), name='community_bridge_buzz_events'),
]
