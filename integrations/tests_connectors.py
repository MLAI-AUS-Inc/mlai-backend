from unittest.mock import MagicMock, patch
import urllib.parse
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalFinancialRecord,
    ExternalServiceProvider,
    FinancialAccount,
    GoogleConnection,
)
from startup_updates.models import (
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailSyncCursor,
    GmailThreadArtifact,
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
    MonthlyUpdateDraft,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupDataDeletionRequest,
    StartupEvent,
    UserStartupBinding,
    StartupMetricObservation,
)
from startup_updates.services import publish_xero_metric_observations
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()


def _json_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _xero_profit_and_loss_report(
    *,
    total_income: str,
    net_profit: str,
    cost_of_sales: Optional[str] = None,
    operating_expenses: Optional[str] = None,
    total_expenses: Optional[str] = None,
) -> dict:
    rows = [
        {
            "RowType": "Section",
            "Title": "Income",
            "Rows": [
                {
                    "RowType": "SummaryRow",
                    "Cells": [{"Value": "Total Income"}, {"Value": total_income}],
                }
            ],
        },
    ]
    if cost_of_sales is not None:
        rows.append(
            {
                "RowType": "Section",
                "Title": "Cost of Sales",
                "Rows": [
                    {
                        "RowType": "SummaryRow",
                        "Cells": [{"Value": "Total Cost of Sales"}, {"Value": cost_of_sales}],
                    }
                ],
            }
        )
    if operating_expenses is not None:
        rows.append(
            {
                "RowType": "Section",
                "Title": "Operating Expenses",
                "Rows": [
                    {
                        "RowType": "SummaryRow",
                        "Cells": [{"Value": "Total Operating Expenses"}, {"Value": operating_expenses}],
                    }
                ],
            }
        )
    if total_expenses is not None:
        rows.append(
            {
                "RowType": "SummaryRow",
                "Cells": [{"Value": "Total Expenses"}, {"Value": total_expenses}],
            }
        )
    rows.append(
        {
            "RowType": "Row",
            "Cells": [{"Value": "Net Profit"}, {"Value": net_profit}],
        }
    )
    return {
        "Reports": [
            {
                "Rows": rows
            }
        ]
    }


def _xero_balance_sheet_report(*, total_bank: str) -> dict:
    return {
        "Reports": [
            {
                "Rows": [
                    {
                        "RowType": "Section",
                        "Title": "Bank",
                        "Rows": [
                            {
                                "RowType": "SummaryRow",
                                "Cells": [{"Value": "Total Bank"}, {"Value": total_bank}],
                            }
                        ],
                    }
                ]
            }
        ]
    }


@override_settings(
    DEFAULT_FRONTEND_URL="http://localhost:5173",
    VIBE_RAISING_URL="http://localhost:5173",
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
    STRIPE_CONNECT_CLIENT_ID="ca_test_1234567890",
    STRIPE_SECRET_KEY="sk_test_1234567890",
    STRIPE_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/stripe",
    XERO_CLIENT_ID="xero-client-real-123",
    XERO_CLIENT_SECRET="xero-client-secret-real-123",
    XERO_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/xero",
    XERO_OAUTH_SCOPES=[
        "offline_access",
        "accounting.invoices.read",
        "accounting.payments.read",
        "accounting.settings.read",
        "accounting.contacts.read",
    ],
    NOTION_CLIENT_ID="notion-client-id",
    NOTION_CLIENT_SECRET="notion-client-secret",
    NOTION_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/notion",
    GOOGLE_DRIVE_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/google-drive",
    SLACK_CLIENT_ID="slack-client-id",
    SLACK_CLIENT_SECRET="slack-client-secret",
    SLACK_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/slack",
    LINEAR_CLIENT_ID="linear-client-id",
    LINEAR_CLIENT_SECRET="linear-client-secret",
    LINEAR_OAUTH_REDIRECT_URI="http://localhost:8000/integrations/callback/linear",
    BASIQ_API_KEY="YXBwLWtleS1mb3ItYmFzaXEtc2FuZGJveA==",
    BASIQ_API_BASE_URL="https://au-api.basiq.io",
    BASIQ_CONSENT_UI_URL="https://consent.basiq.io/home",
)
class ConnectorEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.client = Client()
        self.client.force_login(self.user)
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.user)

    def test_sources_status_returns_all_connector_sources(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="google-refresh",
            scope="gmail.readonly",
        )
        ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.STRIPE,
            access_token="stripe-access",
            refresh_token="stripe-refresh",
            external_account_id="acct_123",
            account_label="acct_123",
        )

        response = self.api_client.get("/api/v1/integrations/sources/status")

        self.assertEqual(response.status_code, 200)
        sources = {source["key"]: source for source in response.data["sources"]}
        self.assertEqual(
            set(sources),
            {"gmail", "stripe", "xero", "bank_feed", "notion", "google_drive", "slack", "linear"},
        )
        self.assertEqual(sources["gmail"]["status"], "connected")
        self.assertEqual(sources["gmail"]["accountLabel"], "founder@gmail.com")
        self.assertTrue(sources["gmail"]["hasGmailScope"])
        self.assertFalse(sources["gmail"]["needsGmailReconnect"])
        self.assertTrue(sources["gmail"]["canDisconnect"])
        self.assertTrue(sources["gmail"]["canDeleteData"])
        self.assertEqual(sources["gmail"]["googlePermissionsUrl"], "https://myaccount.google.com/permissions")
        self.assertEqual(sources["stripe"]["status"], "connected")
        self.assertEqual(sources["stripe"]["externalAccountId"], "acct_123")
        self.assertEqual(sources["xero"]["status"], "not_connected")
        self.assertFalse(sources["xero"]["canRequestReportScopes"])
        self.assertFalse(sources["xero"]["needsReportScopeConfiguration"])

    def test_sources_status_prompts_reconnect_when_gmail_scope_missing(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="google-refresh",
            scope="openid https://www.googleapis.com/auth/userinfo.email",
        )

        response = self.api_client.get("/api/v1/integrations/sources/status")

        self.assertEqual(response.status_code, 200)
        sources = {source["key"]: source for source in response.data["sources"]}
        self.assertEqual(sources["gmail"]["status"], "error")
        self.assertFalse(sources["gmail"]["selected"])
        self.assertFalse(sources["gmail"]["hasGmailScope"])
        self.assertTrue(sources["gmail"]["needsGmailReconnect"])
        self.assertEqual(sources["gmail"]["warning"], "Reconnect Gmail to grant read access.")

    def _create_gmail_startup_data(self, *, status=ContentFactoryRunStatus.COMPLETED):
        organization = Organization.objects.create(name="Acme", domain="acme.com")
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="google-refresh",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            is_default_for_gmail=True,
        )
        GmailSyncCursor.objects.create(
            organization=organization,
            google_connection=google_connection,
            last_history_id="history-1",
        )
        message = GmailMessageArtifact.objects.create(
            organization=organization,
            google_connection=google_connection,
            gmail_message_id="msg-1",
            gmail_thread_id="thread-1",
            internal_date=timezone.now(),
            subject="Investor update signal",
        )
        GmailThreadArtifact.objects.create(
            organization=organization,
            google_connection=google_connection,
            gmail_thread_id="thread-1",
            source_message_ids=["msg-1"],
            cleaned_text="A customer signed.",
        )
        GmailAttachmentArtifact.objects.create(
            organization=organization,
            message_artifact=message,
            filename="contract.pdf",
            mime_type="application/pdf",
            part_id="1",
            gmail_attachment_id="att-1",
        )
        run = ContentFactoryRun.objects.create(
            run_id=f"startup-update-{status}",
            workflow="startup_monthly_update",
            domain=organization.domain,
            status=status,
            run_request={
                "binding_id": binding.id,
                "google_connection_id": google_connection.id,
                "input_sources": ["gmail"],
            },
            result={"draft": "contains Gmail-derived content"},
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 4, 1),
            status="draft",
            structured_memo={"title": "April update"},
        )
        StartupEvent.objects.create(
            organization=organization,
            run=run,
            canonical_key=f"event-{status}",
            event_type="customer_win",
            title="Customer signed",
            month_bucket=date(2026, 4, 1),
            evidence_message_ids=["msg-1"],
            source_thread_ids=["thread-1"],
        )
        StartupMetricObservation.objects.create(
            organization=organization,
            run=run,
            metric_key="mrr",
            metric_name="MRR",
            value_text="$12,000",
            period_month=date(2026, 4, 1),
            source_provider="gmail",
            evidence_message_ids=["msg-1"],
        )
        return organization, google_connection, run

    def _create_non_gmail_startup_data(self, organization):
        binding = UserStartupBinding.objects.get(user=self.user, organization=organization)
        synced_at = timezone.now()
        slack_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=self.user,
            organization=organization,
            access_token="slack-token",
            account_label="Acme Slack",
            status=ExternalServiceConnectionStatus.CONNECTED,
            sync_cursor={"latest_seen_by_channel": {"C123": "1770000000.000100"}},
            last_synced_at=synced_at,
        )
        SlackChannelSelection.objects.create(
            connection=slack_connection,
            user=self.user,
            organization=organization,
            channel_id="C123",
            channel_name="investor-updates",
            selected=True,
            sync_cursor={"history_cursor": "cursor-1"},
            raw_payload={"name": "investor-updates"},
        )
        SlackMessageArtifact.objects.create(
            organization=organization,
            connection=slack_connection,
            channel_id="C123",
            channel_name="investor-updates",
            slack_message_ts="1770000000.000100",
            thread_ts="1770000000.000100",
            posted_at=synced_at,
            text="Customer launch is ready.",
            cleaned_text="Customer launch is ready.",
            raw_payload={"text": "Customer launch is ready."},
        )
        SlackThreadArtifact.objects.create(
            organization=organization,
            connection=slack_connection,
            channel_id="C123",
            channel_name="investor-updates",
            thread_ts="1770000000.000100",
            source_message_ids=["slack:C123:1770000000.000100"],
            cleaned_text="Customer launch is ready.",
            message_payloads=[{"text": "Customer launch is ready."}],
        )
        linear_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=organization,
            access_token="linear-token",
            account_label="Acme Linear",
            status=ExternalServiceConnectionStatus.CONNECTED,
            sync_cursor={"startup_update_run_id": "startup-update-mixed-sources"},
            last_synced_at=synced_at,
        )
        LinearProjectSelection.objects.create(
            connection=linear_connection,
            user=self.user,
            organization=organization,
            linear_project_id="proj-1",
            project_name="Launch",
            selected=True,
            sync_cursor={"updated_after": "2026-04-01T00:00:00Z"},
            raw_payload={"name": "Launch"},
        )
        project = LinearProjectArtifact.objects.create(
            organization=organization,
            connection=linear_connection,
            linear_project_id="proj-1",
            name="Launch",
            description="Launch project context.",
            raw_payload={"id": "proj-1"},
        )
        LinearIssueArtifact.objects.create(
            organization=organization,
            connection=linear_connection,
            project=project,
            linear_issue_id="issue-1",
            identifier="ACME-1",
            title="Ship launch",
            raw_payload={"id": "issue-1"},
        )
        LinearProjectUpdateArtifact.objects.create(
            organization=organization,
            connection=linear_connection,
            project=project,
            linear_project_update_id="update-1",
            body="Launch is green.",
            raw_payload={"id": "update-1"},
        )
        notion_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.NOTION,
            user=self.user,
            organization=organization,
            access_token="notion-token",
            account_label="Acme Notion",
            status=ExternalServiceConnectionStatus.CONNECTED,
            sync_cursor={
                "startup_update_runs": {
                    "startup-update-mixed-sources": {
                        "pages": [{"notion_page_id": "page-1", "cleaned_text": "Board notes"}],
                        "classifications": {"page-1:main": {"relevance_label": "relevant"}},
                        "extracted_chunk_ids": ["page-1:main"],
                    }
                },
                "startup_update_index_partial": True,
                "workspace_cursor": "keep-non-run-metadata",
            },
            last_synced_at=synced_at,
        )
        run = ContentFactoryRun.objects.create(
            run_id="startup-update-mixed-sources",
            workflow="startup_monthly_update",
            domain=organization.domain,
            status=ContentFactoryRunStatus.RUNNING,
            run_request={
                "organization_id": organization.id,
                "binding_id": binding.id,
                "input_sources": ["slack", "linear", "notion"],
                "startup_memory": {"facts": [{"title": "Prior ask"}]},
                "external_context": {"slack": {"selected_channel_ids": ["C123"]}},
            },
            result={"draft": "contains Slack, Linear, and Notion context"},
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 5, 1),
            status="draft",
            structured_memo={"title": "May update"},
        )
        StartupEvent.objects.create(
            organization=organization,
            run=run,
            canonical_key="mixed-source-event",
            event_type="product_milestone",
            title="Launch shipped",
            month_bucket=date(2026, 5, 1),
            source_thread_ids=["slack:C123:1770000000.000100", "proj-1", "notion:page:page-1"],
        )
        StartupMetricObservation.objects.create(
            organization=organization,
            run=run,
            metric_key="launch_progress",
            metric_name="Launch Progress",
            value_text="90%",
            period_month=date(2026, 5, 1),
            source_provider=ExternalServiceProvider.LINEAR,
            source_record_ids=["proj-1"],
        )
        return run, slack_connection, linear_connection, notion_connection

    def test_gmail_disconnect_is_idempotent_when_not_connected(self):
        response = self.api_client.delete("/api/v1/integrations/gmail/connection", {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "not_connected")
        self.assertEqual(response.data["deleted"]["gmailMessages"], 0)

    @patch("startup_updates.data_deletion.requests.post")
    def test_gmail_disconnect_deletes_token_and_raw_gmail_but_keeps_derived_outputs(self, mock_revoke):
        mock_revoke.return_value.status_code = 200
        mock_revoke.return_value.text = ""
        organization, google_connection, run = self._create_gmail_startup_data()

        response = self.api_client.delete(
            "/api/v1/integrations/gmail/connection",
            {"deleteDerivedData": False, "reason": "user_request"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "disconnected")
        self.assertEqual(response.data["googleAccount"], "founder@gmail.com")
        self.assertTrue(response.data["googleRevocation"]["succeeded"])
        self.assertFalse(GoogleConnection.objects.filter(id=google_connection.id).exists())
        self.assertFalse(GmailMessageArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(GmailThreadArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(GmailAttachmentArtifact.objects.filter(organization=organization).exists())
        self.assertTrue(MonthlyUpdateDraft.objects.filter(run=run).exists())
        self.assertTrue(StartupEvent.objects.filter(run=run).exists())
        self.assertTrue(StartupMetricObservation.objects.filter(run=run).exists())
        self.assertTrue(StartupDataDeletionRequest.objects.filter(provider="gmail", delete_derived_data=False).exists())

    @patch("startup_updates.data_deletion.cancel_valley_run")
    @patch("startup_updates.data_deletion.requests.post")
    def test_gmail_disconnect_with_derived_delete_deletes_outputs_and_cancels_open_runs(self, mock_revoke, mock_cancel_valley):
        mock_revoke.return_value.status_code = 200
        mock_revoke.return_value.text = ""
        mock_cancel_valley.return_value = {
            "run_id": "startup-update-running",
            "revoke_requested": True,
            "revoke_succeeded": True,
            "revoked_job_ids": [],
            "missing_job_ids": [],
        }
        organization, google_connection, run = self._create_gmail_startup_data(status=ContentFactoryRunStatus.RUNNING)

        response = self.api_client.delete(
            "/api/v1/integrations/gmail/connection",
            {"deleteDerivedData": True, "reason": "user_request"},
            format="json",
        )

        run.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertFalse(GoogleConnection.objects.filter(id=google_connection.id).exists())
        self.assertFalse(MonthlyUpdateDraft.objects.filter(organization=organization).exists())
        self.assertFalse(StartupEvent.objects.filter(organization=organization).exists())
        self.assertFalse(StartupMetricObservation.objects.filter(organization=organization).exists())
        self.assertEqual(response.data["deleted"]["monthlyDrafts"], 1)
        mock_cancel_valley.assert_called_once_with(run.run_id)

    @patch("startup_updates.data_deletion.requests.post")
    def test_gmail_disconnect_returns_manual_revoke_warning_when_google_revoke_fails(self, mock_revoke):
        mock_revoke.return_value.status_code = 500
        mock_revoke.return_value.text = "server error"
        _organization, google_connection, _run = self._create_gmail_startup_data()

        response = self.api_client.delete(
            "/api/v1/integrations/gmail/connection",
            {"deleteDerivedData": False},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["googleRevocation"]["succeeded"])
        self.assertIn("Google Account permissions", response.data["googleRevocation"]["warning"])
        self.assertEqual(response.data["googlePermissionsUrl"], "https://myaccount.google.com/permissions")
        self.assertFalse(GoogleConnection.objects.filter(id=google_connection.id).exists())

    def test_startup_data_status_and_delete_internal_endpoints(self):
        organization, _google_connection, _run = self._create_gmail_startup_data()
        internal_client = APIClient()

        with self.settings(INTERNAL_API_KEY="internal-key"):
            status_response = internal_client.get(
                f"/api/v1/startups/{organization.id}/data/status",
                HTTP_X_API_KEY="internal-key",
            )
            delete_response = internal_client.delete(
                f"/api/v1/startups/{organization.id}/data",
                {
                    "requested_by_user_id": self.user.id,
                    "reason": "user_request",
                    "request_id": "delete-startup-acme",
                },
                format="json",
                HTTP_X_API_KEY="internal-key",
            )
            final_status_response = internal_client.get(
                f"/api/v1/startups/{organization.id}/data/status",
                HTTP_X_API_KEY="internal-key",
            )

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.data["deletion_status"], "active")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.data["deletion_status"], "deleted")
        self.assertEqual(final_status_response.data["deletion_status"], "deleted")
        self.assertFalse(GmailMessageArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(MonthlyUpdateDraft.objects.filter(organization=organization).exists())
        self.assertTrue(StartupDataDeletionRequest.objects.filter(request_id="delete-startup-acme").exists())

    @patch("startup_updates.data_deletion.cancel_valley_run")
    def test_startup_data_delete_clears_all_source_artifacts_and_scrubs_runs(self, mock_cancel_valley):
        mock_cancel_valley.return_value = {
            "run_id": "startup-update",
            "revoke_requested": True,
            "revoke_succeeded": True,
            "revoked_job_ids": [],
            "missing_job_ids": [],
        }
        organization, _google_connection, gmail_run = self._create_gmail_startup_data(
            status=ContentFactoryRunStatus.RUNNING
        )
        mixed_run, slack_connection, linear_connection, notion_connection = self._create_non_gmail_startup_data(
            organization
        )
        internal_client = APIClient()

        with self.settings(INTERNAL_API_KEY="internal-key"):
            response = internal_client.delete(
                f"/api/v1/startups/{organization.id}/data",
                {"requested_by_user_id": self.user.id, "reason": "user_request", "request_id": "delete-all-acme"},
                format="json",
                HTTP_X_API_KEY="internal-key",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deletion_status"], "deleted")
        self.assertFalse(GmailMessageArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(GmailThreadArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(GmailAttachmentArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(GmailSyncCursor.objects.filter(organization=organization).exists())
        self.assertFalse(SlackMessageArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(SlackThreadArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(SlackChannelSelection.objects.filter(organization=organization).exists())
        self.assertFalse(LinearProjectUpdateArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(LinearIssueArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(LinearProjectArtifact.objects.filter(organization=organization).exists())
        self.assertFalse(LinearProjectSelection.objects.filter(organization=organization).exists())
        self.assertFalse(MonthlyUpdateDraft.objects.filter(organization=organization).exists())
        self.assertFalse(StartupEvent.objects.filter(organization=organization).exists())
        self.assertFalse(StartupMetricObservation.objects.filter(organization=organization).exists())

        slack_connection.refresh_from_db()
        linear_connection.refresh_from_db()
        notion_connection.refresh_from_db()
        self.assertEqual(slack_connection.sync_cursor, {})
        self.assertIsNone(slack_connection.last_synced_at)
        self.assertEqual(linear_connection.sync_cursor, {})
        self.assertIsNone(linear_connection.last_synced_at)
        self.assertNotIn("startup_update_runs", notion_connection.sync_cursor)
        self.assertFalse(notion_connection.sync_cursor.get("startup_update_index_partial"))
        self.assertEqual(notion_connection.sync_cursor.get("workspace_cursor"), "keep-non-run-metadata")

        gmail_run.refresh_from_db()
        mixed_run.refresh_from_db()
        self.assertEqual(gmail_run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(mixed_run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertTrue(gmail_run.run_request["data_deleted"])
        self.assertTrue(mixed_run.run_request["data_deleted"])
        self.assertNotIn("startup_memory", mixed_run.run_request)
        self.assertNotIn("external_context", mixed_run.run_request)
        self.assertFalse(gmail_run.resume_available)
        self.assertFalse(mixed_run.resume_available)

        deleted = response.data["deleted"]
        self.assertEqual(deleted["slackMessages"], 1)
        self.assertEqual(deleted["slackThreads"], 1)
        self.assertEqual(deleted["slackChannelSelections"], 1)
        self.assertEqual(deleted["linearProjects"], 1)
        self.assertEqual(deleted["linearIssues"], 1)
        self.assertEqual(deleted["linearProjectUpdates"], 1)
        self.assertEqual(deleted["linearProjectSelections"], 1)
        self.assertEqual(deleted["notionRunStores"], 1)
        self.assertEqual(deleted["externalConnectionCursors"], 2)
        self.assertEqual(deleted["startupRunsScrubbed"], 2)
        cancelled_run_ids = {call.args[0] for call in mock_cancel_valley.call_args_list}
        self.assertEqual(cancelled_run_ids, {gmail_run.run_id, mixed_run.run_id})
        self.assertTrue(StartupDataDeletionRequest.objects.filter(request_id="delete-all-acme").exists())

    def test_connector_connect_builds_oauth_redirect_and_stores_state(self):
        cases = {
            "stripe": ("https://connect.stripe.com/oauth/authorize", "stripe"),
            "xero": ("https://login.xero.com/identity/connect/authorize", "xero"),
            "notion": ("https://api.notion.com/v1/oauth/authorize", "notion"),
            "google-drive": ("https://accounts.google.com/o/oauth2/v2/auth", "google_drive"),
            "slack": ("https://slack.com/oauth/v2/authorize", "slack"),
            "linear": ("https://linear.app/oauth/authorize", "linear"),
        }

        for slug, (expected_base, provider) in cases.items():
            response = self.client.get(
                f"/integrations/connect/{slug}",
                {"next": "http://localhost:5173/vibe-raising/connect-data?next=/vibe-raising/create-update"},
            )

            self.assertEqual(response.status_code, 302, slug)
            self.assertTrue(response.url.startswith(expected_base), response.url)
            session_state = self.client.session["connector_oauth_state"][provider]
            params = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            self.assertEqual(params["state"], [session_state["state"]])
            if slug == "stripe":
                self.assertEqual(params["response_type"], ["code"])
                self.assertEqual(params["client_id"], ["ca_test_1234567890"])
                self.assertEqual(params["redirect_uri"], ["http://localhost:8000/integrations/callback/stripe"])
                self.assertEqual(params["scope"], ["read_only"])
            if slug == "xero":
                self.assertEqual(params["response_type"], ["code"])
                self.assertEqual(params["client_id"], ["xero-client-real-123"])
                self.assertEqual(params["redirect_uri"], ["http://localhost:8000/integrations/callback/xero"])
                self.assertIn("offline_access", params["scope"][0])
                self.assertIn("accounting.invoices.read", params["scope"][0])
                self.assertIn("accounting.payments.read", params["scope"][0])
                self.assertNotIn("accounting.reports.profitandloss.read", params["scope"][0])
                self.assertNotIn("accounting.reports.balancesheet.read", params["scope"][0])
            if slug == "linear":
                self.assertEqual(params["response_type"], ["code"])
                self.assertEqual(params["client_id"], ["linear-client-id"])
                self.assertEqual(params["redirect_uri"], ["http://localhost:8000/integrations/callback/linear"])
                self.assertEqual(params["scope"], ["read"])
            self.assertEqual(
                session_state["next"],
                "http://localhost:5173/vibe-raising/connect-data?next=/vibe-raising/create-update",
            )

    @override_settings(
        XERO_OAUTH_SCOPES=[
            "offline_access",
            "accounting.invoices.read",
            "accounting.payments.read",
            "accounting.settings.read",
        ],
    )
    def test_xero_connect_requires_operational_scopes(self):
        response = self.client.get(
            "/integrations/connect/xero",
            {"next": "http://localhost:5173/vibe-raising/connect-data"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("accounting.contacts.read", response.content.decode())

    @override_settings(
        STRIPE_OAUTH_REDIRECT_URI="https://api.mlai.au/integrations/callback/stripe",
        DEFAULT_FRONTEND_URL="https://mlai.au",
        VIBE_RAISING_URL="https://mlai.au",
    )
    def test_stripe_connect_uses_production_callback_and_frontend_next(self):
        response = self.client.get(
            "/integrations/connect/stripe",
            {"next": "https://mlai.au/vibe-raising/connect-data?next=/vibe-raising/create-update"},
        )

        self.assertEqual(response.status_code, 302)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
        self.assertEqual(params["redirect_uri"], ["https://api.mlai.au/integrations/callback/stripe"])
        self.assertEqual(
            self.client.session["connector_oauth_state"]["stripe"]["next"],
            "https://mlai.au/vibe-raising/connect-data?next=/vibe-raising/create-update",
        )

    @override_settings(STRIPE_OAUTH_SCOPES=["read_write"])
    def test_stripe_connect_rejects_non_read_only_scope(self):
        response = self.client.get(
            "/integrations/connect/stripe",
            {"next": "http://localhost:5173/vibe-raising/connect-data"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"read_only", response.content)
        self.assertNotIn("connector_oauth_state", self.client.session)

        status_response = self.api_client.get("/api/v1/integrations/sources/status")
        sources = {source["key"]: source for source in status_response.data["sources"]}
        self.assertEqual(sources["stripe"]["status"], "unavailable")
        self.assertIn("read_only", sources["stripe"]["warning"])

    @override_settings(STRIPE_CONNECT_CLIENT_ID="", STRIPE_SECRET_KEY="")
    def test_stripe_status_is_unavailable_when_config_missing(self):
        response = self.api_client.get("/api/v1/integrations/sources/status")

        self.assertEqual(response.status_code, 200)
        sources = {source["key"]: source for source in response.data["sources"]}
        self.assertEqual(sources["stripe"]["status"], "unavailable")
        self.assertEqual(sources["stripe"]["selected"], False)
        self.assertIn("STRIPE_CONNECT_CLIENT_ID", sources["stripe"]["warning"])
        self.assertIn("STRIPE_SECRET_KEY", sources["stripe"]["warning"])

    @override_settings(STRIPE_CONNECT_CLIENT_ID="", STRIPE_SECRET_KEY="")
    def test_stripe_connect_returns_actionable_config_error_when_missing(self):
        response = self.client.get(
            "/integrations/connect/stripe",
            {"next": "http://localhost:5173/vibe-raising/connect-data"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"STRIPE_CONNECT_CLIENT_ID", response.content)
        self.assertIn(b"STRIPE_SECRET_KEY", response.content)
        self.assertNotIn("connector_oauth_state", self.client.session)

    @override_settings(STRIPE_CONNECT_CLIENT_ID="ca_...", STRIPE_SECRET_KEY="sk_test_...")
    def test_stripe_connect_rejects_placeholder_credentials(self):
        response = self.client.get(
            "/integrations/connect/stripe",
            {"next": "http://localhost:5173/vibe-raising/connect-data"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"valid Connect client ID", response.content)
        self.assertNotIn("connector_oauth_state", self.client.session)

        status_response = self.api_client.get("/api/v1/integrations/sources/status")
        sources = {source["key"]: source for source in status_response.data["sources"]}
        self.assertEqual(sources["stripe"]["status"], "unavailable")
        self.assertIn("valid Connect client ID", sources["stripe"]["warning"])

    def test_stripe_callback_stores_encrypted_connection_and_redirects_to_next(self):
        session = self.client.session
        session["connector_oauth_state"] = {
            "stripe": {
                "state": "stripe-state",
                "next": "http://localhost:5173/vibe-raising/connect-data",
            }
        }
        session.save()

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response(
                {
                    "access_token": "stripe-access",
                    "refresh_token": "stripe-refresh",
                    "scope": "read_only",
                    "stripe_user_id": "acct_123",
                    "livemode": False,
                }
            ),
        ):
            response = self.client.get(
                "/integrations/callback/stripe",
                {"state": "stripe-state", "code": "stripe-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "http://localhost:5173/vibe-raising/connect-data")
        connection = ExternalServiceConnection.objects.get(user=self.user, provider=ExternalServiceProvider.STRIPE)
        self.assertEqual(connection.access_token, "stripe-access")
        self.assertEqual(connection.refresh_token, "stripe-refresh")
        self.assertEqual(connection.external_account_id, "acct_123")

    def test_stripe_callback_rejects_invalid_state_without_storing_connection(self):
        session = self.client.session
        session["connector_oauth_state"] = {
            "stripe": {
                "state": "stripe-state",
                "next": "http://localhost:5173/vibe-raising/connect-data",
            }
        }
        session.save()

        response = self.client.get(
            "/integrations/callback/stripe",
            {"state": "wrong-state", "code": "stripe-code"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            ExternalServiceConnection.objects.filter(user=self.user, provider=ExternalServiceProvider.STRIPE).exists()
        )

    def test_stripe_callback_handles_oauth_denial_without_storing_connection(self):
        session = self.client.session
        session["connector_oauth_state"] = {
            "stripe": {
                "state": "stripe-state",
                "next": "http://localhost:5173/vibe-raising/connect-data",
            }
        }
        session.save()

        response = self.client.get(
            "/integrations/callback/stripe",
            {
                "state": "stripe-state",
                "error": "access_denied",
                "error_description": "The user denied your request.",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"The user denied your request.", response.content)
        self.assertFalse(
            ExternalServiceConnection.objects.filter(user=self.user, provider=ExternalServiceProvider.STRIPE).exists()
        )

    def test_xero_callback_fetches_connections_and_stores_tenant(self):
        session = self.client.session
        session["connector_oauth_state"] = {
            "xero": {
                "state": "xero-state",
                "next": "http://localhost:5173/vibe-raising/connect-data",
            }
        }
        session.save()

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response(
                {
                    "access_token": "xero-access",
                    "refresh_token": "xero-refresh",
                    "expires_in": 1800,
                    "scope": "offline_access accounting.transactions.read",
                }
            ),
        ), patch(
            "integrations.services.external_connectors.requests.get",
            return_value=_json_response(
                [
                    {
                        "tenantId": "tenant-123",
                        "tenantName": "Demo Company",
                    }
                ]
            ),
        ) as mock_get:
            response = self.client.get(
                "/integrations/callback/xero",
                {"state": "xero-state", "code": "xero-code"},
            )

        self.assertEqual(response.status_code, 302)
        mock_get.assert_called_once()
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Authorization"],
            "Bearer xero-access",
        )
        connection = ExternalServiceConnection.objects.get(user=self.user, provider=ExternalServiceProvider.XERO)
        self.assertEqual(connection.external_account_id, "tenant-123")
        self.assertEqual(connection.account_label, "Demo Company")

    def test_xero_callback_accepts_signed_state_without_session_state(self):
        connect_response = self.client.get(
            "/integrations/connect/xero",
            {"next": "http://localhost:5173/vibe-raising/connect-data?next=/vibe-raising/create-update"},
        )
        self.assertEqual(connect_response.status_code, 302)
        state = urllib.parse.parse_qs(urllib.parse.urlparse(connect_response.url).query)["state"][0]

        session = self.client.session
        session.pop("connector_oauth_state", None)
        session.save()

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response(
                {
                    "access_token": "xero-access",
                    "refresh_token": "xero-refresh",
                    "expires_in": 1800,
                    "scope": "offline_access accounting.invoices.read accounting.payments.read accounting.settings.read accounting.contacts.read",
                }
            ),
        ), patch(
            "integrations.services.external_connectors.requests.get",
            return_value=_json_response(
                [
                    {
                        "tenantId": "tenant-123",
                        "tenantName": "Demo Company",
                    }
                ]
            ),
        ):
            response = self.client.get(
                "/integrations/callback/xero",
                {"state": state, "code": "xero-code"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            "http://localhost:5173/vibe-raising/connect-data?next=/vibe-raising/create-update",
        )
        self.assertTrue(
            ExternalServiceConnection.objects.filter(
                user=self.user,
                provider=ExternalServiceProvider.XERO,
                external_account_id="tenant-123",
            ).exists()
        )

    def test_xero_sync_refreshes_token_and_upserts_revenue_records(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.XERO,
            access_token="expired-xero-access",
            refresh_token="old-xero-refresh",
            token_expires_at=timezone.now() - timedelta(minutes=5),
            external_account_id="tenant-123",
            account_label="Demo Company",
            sync_cursor={"if_modified_since": "2026-04-01T00:00:00+00:00"},
        )

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response(
                {
                    "access_token": "fresh-xero-access",
                    "refresh_token": "new-xero-refresh",
                    "expires_in": 1800,
                    "scope": "offline_access accounting.transactions.read accounting.settings.read accounting.contacts.read",
                }
            ),
        ) as mock_post, patch(
            "integrations.services.external_connectors.requests.get",
            side_effect=[
                _json_response(
                    {
                        "RepeatingInvoices": [
                            {
                                "RepeatingInvoiceID": "rep-1",
                                "Type": "ACCREC",
                                "Status": "AUTHORISED",
                                "SubTotal": "1200.00",
                                "CurrencyCode": "AUD",
                                "Contact": {"Name": "Retainer Co"},
                                "Schedule": {
                                    "Unit": "MONTHLY",
                                    "Period": 1,
                                    "NextScheduledDate": "2026-04-15",
                                },
                            }
                        ]
                    }
                ),
                _json_response(
                    {
                        "Invoices": [
                            {
                                "InvoiceID": "inv-1",
                                "InvoiceNumber": "INV-001",
                                "Type": "ACCREC",
                                "Status": "PAID",
                                "SubTotal": "500.00",
                                "Total": "550.00",
                                "CurrencyCode": "AUD",
                                "DateString": "2026-04-02",
                                "Contact": {"Name": "Customer Pty Ltd"},
                            }
                        ],
                        "pagination": {"page": 1, "pageCount": 1},
                    }
                ),
                _json_response(
                    {
                        "Payments": [
                            {
                                "PaymentID": "pay-1",
                                "Amount": "500.00",
                                "Date": "2026-04-03",
                                "Status": "AUTHORISED",
                                "Invoice": {
                                    "Type": "ACCREC",
                                    "InvoiceNumber": "INV-001",
                                    "CurrencyCode": "AUD",
                                    "Contact": {"Name": "Customer Pty Ltd"},
                                },
                            }
                        ],
                        "pagination": {"page": 1, "pageCount": 1},
                    }
                ),
            ],
        ) as mock_get:
            response = self.api_client.post(
                "/api/v1/integrations/financial/sync",
                {"providers": ["xero"]},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "synced")
        self.assertEqual(response.data["syncRuns"][0]["repeatingInvoicesSynced"], 1)
        self.assertEqual(response.data["syncRuns"][0]["invoicesSynced"], 1)
        self.assertEqual(response.data["syncRuns"][0]["paymentsSynced"], 1)
        self.assertFalse(response.data["syncRuns"][0]["hasReportScope"])
        self.assertFalse(response.data["syncRuns"][0]["needsReportReconnect"])
        self.assertFalse(response.data["syncRuns"][0]["canRequestReportScopes"])
        self.assertTrue(response.data["syncRuns"][0]["needsReportScopeConfiguration"])
        self.assertEqual(response.data["syncRuns"][0]["metricsPublishedCount"], 0)
        self.assertIn("report metrics are disabled", response.data["syncRuns"][0]["metricWarnings"][0])
        connection.refresh_from_db()
        self.assertEqual(connection.access_token, "fresh-xero-access")
        self.assertEqual(connection.refresh_token, "new-xero-refresh")
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.CONNECTED)
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(ExternalFinancialRecord.objects.filter(connection=connection).count(), 3)
        for call in mock_get.call_args_list:
            self.assertEqual(call.kwargs["headers"]["Xero-Tenant-Id"], "tenant-123")
            self.assertEqual(call.kwargs["headers"]["If-Modified-Since"], "2026-04-01T00:00:00+00:00")

        repeating_record = ExternalFinancialRecord.objects.get(
            connection=connection,
            record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
        )
        self.assertEqual(repeating_record.external_record_id, "rep-1")
        self.assertEqual(str(repeating_record.amount), "1200.00")
        invoice_record = ExternalFinancialRecord.objects.get(
            connection=connection,
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
        )
        self.assertEqual(invoice_record.external_record_id, "inv-1")
        self.assertEqual(invoice_record.merchant_name, "Customer Pty Ltd")
        payment_record = ExternalFinancialRecord.objects.get(
            connection=connection,
            record_type=ExternalFinancialRecord.RECORD_XERO_PAYMENT,
        )
        self.assertEqual(payment_record.external_record_id, "pay-1")

    def test_xero_sync_with_reports_scope_publishes_report_metrics(self):
        organization = Organization.objects.create(name="Acme", domain="acme.example")
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            access_token="xero-access",
            refresh_token="xero-refresh",
            token_expires_at=None,
            external_account_id="tenant-123",
            account_label="Demo Company",
            scopes=[
                "accounting.invoices.read",
                "accounting.payments.read",
                "accounting.reports.profitandloss.read",
                "accounting.reports.balancesheet.read",
            ],
        )

        def fake_get(url, **kwargs):
            if "RepeatingInvoices" in url:
                return _json_response({"RepeatingInvoices": []})
            if "Invoices" in url:
                return _json_response(
                    {
                        "Invoices": [
                            {
                                "InvoiceID": "inv-1",
                                "InvoiceNumber": "INV-001",
                                "Type": "ACCREC",
                                "Status": "PAID",
                                "SubTotal": "3800.00",
                                "CurrencyCode": "AUD",
                                "DateString": "2026-04-12",
                                "Contact": {"Name": "Customer Pty Ltd"},
                            }
                        ],
                        "pagination": {"page": 1, "pageCount": 1},
                    }
                )
            if "Payments" in url:
                return _json_response({"Payments": [], "pagination": {"page": 1, "pageCount": 1}})
            if "Reports/ProfitAndLoss" in url:
                reports = {
                    "2026-04-01": _xero_profit_and_loss_report(total_income="3800.00", total_expenses="4500.00", net_profit="(700.00)"),
                    "2026-03-01": _xero_profit_and_loss_report(total_income="2735.75", total_expenses="3200.00", net_profit="(464.25)"),
                    "2026-02-01": _xero_profit_and_loss_report(total_income="1900.00", total_expenses="1800.00", net_profit="100.00"),
                }
                return _json_response(reports[kwargs["params"]["fromDate"]])
            if "Reports/BalanceSheet" in url:
                return _json_response(_xero_balance_sheet_report(total_bank="9000.00"))
            raise AssertionError(f"Unexpected Xero URL {url}")

        with patch(
            "integrations.services.external_connectors.timezone.now",
            return_value=timezone.make_aware(datetime(2026, 4, 26, 12, 0, 0)),
        ), patch(
            "integrations.services.external_connectors.requests.get",
            side_effect=fake_get,
        ):
            response = self.api_client.post(
                "/api/v1/integrations/financial/sync",
                {"providers": ["xero"]},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        run = response.data["syncRuns"][0]
        self.assertTrue(run["hasReportScope"])
        self.assertFalse(run["needsReportReconnect"])
        self.assertGreaterEqual(run["metricsPublishedCount"], 7)
        self.assertTrue(
            StartupMetricObservation.objects.filter(
                organization=organization,
                source_provider=ExternalServiceProvider.XERO,
                metric_key="revenue",
                period_month=date(2026, 4, 1),
                value_text="AUD 3800.00",
            ).exists()
        )

    def test_xero_sync_refresh_failure_marks_connection_error(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.XERO,
            access_token="expired-xero-access",
            refresh_token="old-xero-refresh",
            token_expires_at=timezone.now() - timedelta(minutes=5),
            external_account_id="tenant-123",
            account_label="Demo Company",
        )

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response({"error": "invalid_grant", "error_description": "Refresh token expired"}),
        ):
            response = self.api_client.post(
                "/api/v1/integrations/financial/sync",
                {"providers": ["xero"]},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "error")
        connection.refresh_from_db()
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.ERROR)
        self.assertIn("Refresh token expired", connection.last_error)

    def test_xero_preview_and_invoices_endpoints_return_normalized_data(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.XERO,
            access_token="xero-access",
            refresh_token="xero-refresh",
            external_account_id="tenant-123",
            account_label="Demo Company",
            last_synced_at=timezone.now(),
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
            external_account_id="tenant-123",
            external_record_id="rep-1",
            amount="1200.00",
            currency="AUD",
            direction="credit",
            status="AUTHORISED",
            transaction_date="2026-04-15",
            description="Retainer Co",
            merchant_name="Retainer Co",
            raw_payload={"Schedule": {"Unit": "MONTHLY", "Period": 1}},
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
            external_account_id="tenant-123",
            external_record_id="inv-1",
            amount="500.00",
            currency="AUD",
            direction="credit",
            status="PAID",
            transaction_date="2026-04-02",
            description="INV-001 · Customer Pty Ltd",
            merchant_name="Customer Pty Ltd",
            raw_payload={"InvoiceNumber": "INV-001"},
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_PAYMENT,
            external_account_id="tenant-123",
            external_record_id="pay-1",
            amount="500.00",
            currency="AUD",
            direction="credit",
            status="AUTHORISED",
            transaction_date="2026-04-03",
            description="INV-001 · Customer Pty Ltd",
            merchant_name="Customer Pty Ltd",
        )

        preview_response = self.api_client.get(
            "/api/v1/integrations/financial/xero/preview",
            {"from": "2026-04-01", "to": "2026-04-30"},
        )
        invoices_response = self.api_client.get(
            "/api/v1/integrations/financial/xero/invoices",
            {"from": "2026-04-01", "to": "2026-04-30"},
        )

        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(preview_response.data["tenantLabel"], "Demo Company")
        self.assertEqual(preview_response.data["monthlyRecurringRevenue"], "1200.00")
        self.assertEqual(preview_response.data["cashCollected"], "500.00")
        self.assertFalse(preview_response.data["hasReportScope"])
        self.assertFalse(preview_response.data["needsReportReconnect"])
        self.assertFalse(preview_response.data["canRequestReportScopes"])
        self.assertTrue(preview_response.data["needsReportScopeConfiguration"])
        self.assertEqual(
            preview_response.data["requiredReportScopes"],
            ["accounting.reports.profitandloss.read", "accounting.reports.balancesheet.read"],
        )
        self.assertIn("report metrics are disabled", preview_response.data["warnings"][0])
        self.assertEqual(preview_response.data["recurringInvoices"][0]["externalRecordId"], "rep-1")
        self.assertEqual(preview_response.data["recentInvoices"][0]["invoiceNumber"], "INV-001")
        self.assertEqual(invoices_response.status_code, 200)
        self.assertEqual(invoices_response.data["invoices"][0]["externalRecordId"], "inv-1")

    @override_settings(
        XERO_OAUTH_SCOPES=[
            "offline_access",
            "accounting.invoices.read",
            "accounting.payments.read",
            "accounting.settings.read",
            "accounting.contacts.read",
            "accounting.reports.profitandloss.read",
            "accounting.reports.balancesheet.read",
        ],
    )
    def test_xero_preview_prompts_reconnect_when_report_scopes_are_configured(self):
        ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.XERO,
            access_token="xero-access",
            refresh_token="xero-refresh",
            external_account_id="tenant-123",
            account_label="Demo Company",
        )

        response = self.api_client.get("/api/v1/integrations/financial/xero/preview")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["hasReportScope"])
        self.assertTrue(response.data["canRequestReportScopes"])
        self.assertFalse(response.data["needsReportScopeConfiguration"])
        self.assertTrue(response.data["needsReportReconnect"])
        self.assertIn("Reconnect Xero", response.data["warnings"][0])

    def test_xero_preview_returns_json_when_storage_is_unavailable(self):
        with patch(
            "integrations.api_views_connectors.serialize_xero_preview",
            side_effect=DatabaseError("missing table"),
        ):
            response = self.api_client.get("/api/v1/integrations/financial/xero/preview")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "preview_storage_unavailable")
        self.assertIn("Run backend migrations", response.data["detail"])

    def test_gmail_preview_returns_disconnected_state(self):
        response = self.api_client.get("/api/v1/integrations/gmail/preview")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["accountLabel"])
        self.assertEqual(response.data["messages"], [])
        self.assertIn("Gmail is not connected.", response.data["warnings"])
        self.assertFalse(response.data["hasGmailScope"])
        self.assertFalse(response.data["needsGmailReconnect"])

    def test_gmail_preview_returns_cached_message_previews(self):
        organization = Organization.objects.create(name="Topline", domain="topline.com")
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="google-refresh",
            scope="gmail.readonly",
        )
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            is_default_for_gmail=True,
        )
        GmailSyncCursor.objects.create(
            organization=organization,
            google_connection=google_connection,
            last_message_internal_date=timezone.now(),
        )
        GmailMessageArtifact.objects.create(
            organization=organization,
            google_connection=google_connection,
            gmail_message_id="msg-1",
            gmail_thread_id="thread-1",
            internal_date=timezone.now(),
            subject="April revenue update",
            from_address="Investor <investor@example.com>",
            snippet="Great progress on revenue this month.",
            has_attachments=True,
            relevance_label=GmailRelevanceLabel.RELEVANT,
        )

        response = self.api_client.get("/api/v1/integrations/gmail/preview", {"limit": 5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["accountLabel"], "founder@gmail.com")
        self.assertEqual(response.data["totalCachedMessages"], 1)
        self.assertTrue(response.data["hasGmailScope"])
        self.assertFalse(response.data["needsGmailReconnect"])
        self.assertEqual(response.data["messages"][0]["subject"], "April revenue update")
        self.assertEqual(response.data["messages"][0]["hasAttachments"], True)
        self.assertEqual(response.data["messages"][0]["relevanceLabel"], GmailRelevanceLabel.RELEVANT)

    def test_gmail_preview_fetches_metadata_without_persisting_when_cache_is_empty(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="google-refresh",
            scope="gmail.readonly",
        )

        metadata = {
            "id": "msg-remote-1",
            "threadId": "thread-remote-1",
            "internalDate": "1775505600000",
            "snippet": "A short remote snippet",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Remote Gmail metadata"},
                    {"name": "From", "value": "Founder <founder@example.com>"},
                ],
                "parts": [{"filename": "report.pdf", "body": {"attachmentId": "att-1"}}],
            },
        }
        with patch("integrations.services.external_connectors.build_gmail_service", return_value=object()), patch(
            "integrations.services.external_connectors.list_message_page",
            return_value={"messages": [{"id": "msg-remote-1"}]},
        ), patch(
            "integrations.services.external_connectors.get_message_metadata",
            return_value=metadata,
        ):
            response = self.api_client.get("/api/v1/integrations/gmail/preview", {"limit": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["messages"][0]["subject"], "Remote Gmail metadata")
        self.assertEqual(response.data["messages"][0]["hasAttachments"], True)
        self.assertEqual(GmailMessageArtifact.objects.count(), 0)

    def test_gmail_preview_does_not_call_gmail_when_scope_missing(self):
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="google-refresh",
            scope="openid https://www.googleapis.com/auth/userinfo.email",
        )

        with patch("integrations.services.external_connectors.build_gmail_service") as mock_build:
            response = self.api_client.get("/api/v1/integrations/gmail/preview", {"limit": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["accountLabel"], "founder@gmail.com")
        self.assertEqual(response.data["messages"], [])
        self.assertFalse(response.data["hasGmailScope"])
        self.assertTrue(response.data["needsGmailReconnect"])
        self.assertIn("Reconnect Gmail to grant read access.", response.data["warnings"])
        mock_build.assert_not_called()

    def test_bank_feed_connect_creates_basiq_user_and_redirects_to_consent_ui(self):
        with patch(
            "integrations.services.external_connectors.requests.post",
            side_effect=[
                _json_response({"access_token": "server-token"}),
                _json_response({"id": "basiq-user-123"}),
                _json_response({"access_token": "client-token"}),
            ],
        ) as mock_post:
            response = self.client.get(
                "/integrations/connect/bank-feed",
                {"next": "http://localhost:5173/vibe-raising/connect-data"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://consent.basiq.io/home?"))
        params = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
        self.assertEqual(params["token"], ["client-token"])
        self.assertEqual(mock_post.call_count, 3)
        connection = ExternalServiceConnection.objects.get(user=self.user, provider=ExternalServiceProvider.BANK_FEED)
        self.assertEqual(connection.external_account_id, "basiq-user-123")
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.SYNCING)

    def test_bank_feed_connect_reuses_existing_basiq_user_for_additional_bank(self):
        ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="basiq-user-123",
            account_label="Basiq bank feed",
            provider_metadata={"basiq_user": {"id": "basiq-user-123"}},
            status=ExternalServiceConnectionStatus.CONNECTED,
        )

        with patch(
            "integrations.services.external_connectors.requests.post",
            side_effect=[
                _json_response({"access_token": "server-token"}),
                _json_response({"access_token": "client-token"}),
            ],
        ) as mock_post:
            response = self.client.get(
                "/integrations/connect/bank-feed",
                {"next": "http://localhost:5173/vibe-raising/connect-data"},
            )

        self.assertEqual(response.status_code, 302)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
        self.assertEqual(params["token"], ["client-token"])
        self.assertEqual(params["action"], ["connect"])
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(
            ExternalServiceConnection.objects.filter(
                user=self.user,
                provider=ExternalServiceProvider.BANK_FEED,
            ).count(),
            1,
        )

    def test_bank_feed_callback_stores_job_ids_and_marks_syncing(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="basiq-user-123",
            account_label="Basiq bank feed",
            status=ExternalServiceConnectionStatus.SYNCING,
        )
        session = self.client.session
        session["connector_oauth_state"] = {
            "bank_feed": {
                "state": "bank-state",
                "next": "http://localhost:5173/vibe-raising/connect-data",
                "basiq_user_id": "basiq-user-123",
            }
        }
        session.save()

        response = self.client.get(
            "/integrations/callback/bank-feed",
            {"state": "bank-state", "jobIds": "job-1,job-2"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "http://localhost:5173/vibe-raising/connect-data")
        connection.refresh_from_db()
        self.assertEqual(connection.provider_metadata["job_ids"], ["job-1", "job-2"])
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.SYNCING)

    def test_bank_feed_sync_fetches_accounts_and_posted_transactions_idempotently(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="basiq-user-123",
            account_label="Basiq bank feed",
            provider_metadata={"job_ids": ["job-1"]},
            status=ExternalServiceConnectionStatus.SYNCING,
        )

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response({"access_token": "server-token"}),
        ), patch(
            "integrations.services.external_connectors.requests.get",
            side_effect=[
                _json_response({"id": "job-1", "status": "completed"}),
                _json_response(
                    {
                        "data": [
                            {
                                "id": "acc-1",
                                "name": "Business Transaction",
                                "type": "transaction",
                                "status": "available",
                                "currency": "AUD",
                                "balance": {"current": "1250.30", "available": "1200.10"},
                                "institution": {"id": "inst-1", "name": "Demo Bank"},
                            }
                        ]
                    }
                ),
                _json_response(
                    {
                        "data": [
                            {
                                "id": "txn-1",
                                "account": {"id": "acc-1"},
                                "status": "posted",
                                "postDate": "2026-04-01T00:00:00Z",
                                "transactionDate": "2026-04-01",
                                "description": "Customer payment",
                                "amount": "250.00",
                                "currency": "AUD",
                                "direction": "credit",
                                "merchant": {"name": "Customer Pty Ltd"},
                                "category": "Sales",
                                "class": "payment",
                            },
                            {
                                "id": "txn-pending",
                                "account": {"id": "acc-1"},
                                "status": "pending",
                                "amount": "50.00",
                            },
                        ]
                    }
                ),
            ],
        ):
            first_response = self.api_client.post(
                "/api/v1/integrations/financial/sync",
                {"providers": ["bank_feed"]},
                format="json",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(first_response.data["status"], "synced")
        connection.refresh_from_db()
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.CONNECTED)
        self.assertIsNotNone(connection.last_synced_at)
        self.assertEqual(FinancialAccount.objects.filter(connection=connection).count(), 1)
        self.assertEqual(ExternalFinancialRecord.objects.filter(connection=connection).count(), 1)

        account = FinancialAccount.objects.get(connection=connection)
        self.assertEqual(account.external_account_id, "acc-1")
        self.assertEqual(account.institution_name, "Demo Bank")

        record = ExternalFinancialRecord.objects.get(connection=connection)
        self.assertEqual(record.external_record_id, "txn-1")
        self.assertEqual(record.financial_account, account)
        self.assertEqual(str(record.amount), "250.00")
        self.assertEqual(record.status, "posted")

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response({"access_token": "server-token"}),
        ), patch(
            "integrations.services.external_connectors.requests.get",
            side_effect=[
                _json_response({"id": "job-1", "status": "completed"}),
                _json_response({"data": [account.raw_payload]}),
                _json_response({"data": [record.raw_payload]}),
            ],
        ):
            second_response = self.api_client.post(
                "/api/v1/integrations/financial/sync",
                {"providers": ["bank_feed"]},
                format="json",
            )

        self.assertEqual(second_response.status_code, 202)
        self.assertEqual(FinancialAccount.objects.filter(connection=connection).count(), 1)
        self.assertEqual(ExternalFinancialRecord.objects.filter(connection=connection).count(), 1)

    def test_bank_feed_sync_failed_job_marks_connection_error(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="basiq-user-123",
            account_label="Basiq bank feed",
            provider_metadata={"job_ids": ["job-1"]},
            status=ExternalServiceConnectionStatus.SYNCING,
        )

        with patch(
            "integrations.services.external_connectors.requests.post",
            return_value=_json_response({"access_token": "server-token"}),
        ), patch(
            "integrations.services.external_connectors.requests.get",
            return_value=_json_response({"id": "job-1", "status": "failed", "message": "Bank login failed"}),
        ):
            response = self.api_client.post(
                "/api/v1/integrations/financial/sync",
                {"providers": ["bank_feed"]},
                format="json",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["status"], "error")
        connection.refresh_from_db()
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.ERROR)
        self.assertIn("Bank login failed", connection.last_error)

    def test_bank_feed_accounts_and_transactions_endpoints_return_normalized_data(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="basiq-user-123",
            account_label="Basiq bank feed",
        )
        account = FinancialAccount.objects.create(
            user=self.user,
            connection=connection,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="acc-1",
            account_label="Business Transaction",
            institution_name="Demo Bank",
            account_type="transaction",
            currency="AUD",
            balance="1250.30",
            available_funds="1200.10",
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            connection=connection,
            financial_account=account,
            provider=ExternalServiceProvider.BANK_FEED,
            external_account_id="acc-1",
            external_record_id="txn-1",
            amount="250.00",
            currency="AUD",
            direction="credit",
            status="posted",
            transaction_date="2026-04-01",
            description="Customer payment",
            merchant_name="Customer Pty Ltd",
            category="Sales",
        )

        accounts_response = self.api_client.get("/api/v1/integrations/financial/bank-feed/accounts")
        transactions_response = self.api_client.get(
            "/api/v1/integrations/financial/bank-feed/transactions",
            {"accountId": account.id, "from": "2026-04-01", "to": "2026-04-30"},
        )

        self.assertEqual(accounts_response.status_code, 200)
        self.assertEqual(accounts_response.data["accounts"][0]["accountLabel"], "Business Transaction")
        self.assertEqual(accounts_response.data["accounts"][0]["balance"], "1250.30")
        self.assertEqual(transactions_response.status_code, 200)
        self.assertEqual(transactions_response.data["transactions"][0]["externalTransactionId"], "txn-1")
        self.assertEqual(transactions_response.data["transactions"][0]["merchantName"], "Customer Pty Ltd")

    def test_financial_sync_and_disconnect_connection(self):
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.STRIPE,
            access_token="stripe-access",
            refresh_token="stripe-refresh",
            external_account_id="acct_123",
            account_label="acct_123",
        )

        sync_response = self.api_client.post("/api/v1/integrations/financial/sync", {}, format="json")

        self.assertEqual(sync_response.status_code, 202)
        connection.refresh_from_db()
        self.assertIsNotNone(connection.last_synced_at)

        delete_response = self.api_client.delete(f"/api/v1/integrations/financial/connections/{connection.id}")

        self.assertEqual(delete_response.status_code, 200)
        connection.refresh_from_db()
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.DISCONNECTED)
        self.assertEqual(connection.access_token, "")

    def test_xero_metric_publishing_creates_source_backed_observations(self):
        organization = Organization.objects.create(name="Acme", domain="acme.example")
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-123",
            account_label="Acme Xero",
            scopes=["accounting.reports.read"],
        )

        ExternalFinancialRecord.objects.create(
            user=self.user,
            organization=organization,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
            external_account_id="tenant-123",
            external_record_id="repeat-prev",
            currency="AUD",
            amount="1000.00",
            status="AUTHORISED",
            transaction_date=date(2026, 3, 1),
            description="March recurring invoice",
            raw_payload={"Schedule": {"Unit": "MONTHLY", "Period": 1, "StartDate": "2026-03-01", "EndDate": "2026-03-31"}},
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            organization=organization,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE,
            external_account_id="tenant-123",
            external_record_id="repeat-current",
            currency="AUD",
            amount="1200.00",
            status="AUTHORISED",
            transaction_date=date(2026, 4, 1),
            description="April recurring invoice",
            raw_payload={"Schedule": {"Unit": "MONTHLY", "Period": 1, "StartDate": "2026-04-01"}},
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            organization=organization,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
            external_account_id="tenant-123",
            external_record_id="invoice-current",
            currency="AUD",
            amount="2500.00",
            status="PAID",
            transaction_date=date(2026, 4, 15),
            description="April sales invoice",
            merchant_name="Acme Customer",
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            organization=organization,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_PAYMENT,
            external_account_id="tenant-123",
            external_record_id="payment-current",
            currency="AUD",
            amount="2400.00",
            status="AUTHORISED",
            transaction_date=date(2026, 4, 20),
            description="April payment",
            merchant_name="Acme Customer",
        )
        StartupMetricObservation.objects.create(
            organization=organization,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="burnRate",
            metric_name="Burn rate",
            value_text="AUD 999.00",
            value_number=Decimal("999.00"),
            unit="AUD",
            period_month=date(2026, 2, 1),
            confidence=1.0,
            source_metadata={"report_name": "ProfitAndLoss"},
        )

        def fake_report(_connection, report_name, *, params=None):
            if report_name == "ProfitAndLoss":
                reports = {
                    "2026-04-01": _xero_profit_and_loss_report(
                        total_income="4000.00",
                        cost_of_sales="500.00",
                        operating_expenses="5000.00",
                        net_profit="(1500.00)",
                    ),
                    "2026-03-01": _xero_profit_and_loss_report(
                        total_income="3000.00",
                        cost_of_sales="500.00",
                        operating_expenses="3500.00",
                        net_profit="(1000.00)",
                    ),
                    "2026-02-01": _xero_profit_and_loss_report(
                        total_income="2000.00",
                        total_expenses="1500.00",
                        net_profit="500.00",
                    ),
                }
                return reports[params["fromDate"]]
            if report_name == "BalanceSheet":
                return _xero_balance_sheet_report(total_bank="9000.00")
            raise AssertionError(f"Unexpected report {report_name}")

        with patch("integrations.services.external_connectors.fetch_xero_accounting_report", side_effect=fake_report):
            summary = publish_xero_metric_observations(
                organization=organization,
                run=None,
                start_date=date(2026, 3, 1),
                end_date=date(2026, 4, 30),
            )

        self.assertGreaterEqual(summary["published_metric_count"], 15)
        metrics = {
            metric.metric_key: metric
            for metric in StartupMetricObservation.objects.filter(
                organization=organization,
                source_provider=ExternalServiceProvider.XERO,
                period_month=date(2026, 4, 1),
            )
        }
        self.assertEqual(metrics["mrr"].value_text, "AUD 1200.00")
        self.assertEqual(metrics["revenue"].value_text, "AUD 4000.00")
        self.assertEqual(metrics["revenueGrowthRate"].value_text, "33.33%")
        self.assertEqual(metrics["burnRate"].value_text, "AUD 1500.00")
        self.assertEqual(metrics["runway"].value_text, "7.2 months")
        self.assertEqual(metrics["monthlyCosts"].value_text, "AUD 5500.00")
        self.assertEqual(metrics["operatingExpenses"].value_text, "AUD 5000.00")
        self.assertEqual(metrics["costOfSales"].value_text, "AUD 500.00")
        self.assertEqual(metrics["invoiceRevenue"].value_text, "AUD 2500.00")
        self.assertEqual(metrics["cashCollected"].value_text, "AUD 2400.00")
        self.assertEqual(metrics["customerCount"].value_text, "1")
        march_revenue = StartupMetricObservation.objects.get(
            organization=organization,
            source_provider=ExternalServiceProvider.XERO,
            period_month=date(2026, 3, 1),
            metric_key="revenue",
        )
        february_revenue = StartupMetricObservation.objects.get(
            organization=organization,
            source_provider=ExternalServiceProvider.XERO,
            period_month=date(2026, 2, 1),
            metric_key="revenue",
        )
        self.assertEqual(march_revenue.value_text, "AUD 3000.00")
        self.assertEqual(february_revenue.value_text, "AUD 2000.00")
        self.assertFalse(
            StartupMetricObservation.objects.filter(
                organization=organization,
                source_provider=ExternalServiceProvider.XERO,
                period_month=date(2026, 2, 1),
                metric_key="burnRate",
            ).exists()
        )
        self.assertEqual(metrics["mrr"].source_record_ids, ["repeat-current"])
        self.assertEqual(metrics["mrr"].source_metadata["source_metric"], "xero_repeating_invoice_mrr")
        self.assertEqual(metrics["revenue"].source_metadata["report_name"], "ProfitAndLoss")
        self.assertEqual(metrics["monthlyCosts"].source_metadata["report_name"], "ProfitAndLoss")
        self.assertEqual(
            metrics["monthlyCosts"].source_metadata["calculation_basis"],
            "cost_of_sales_plus_operating_expenses_when_available_otherwise_total_expenses",
        )
        self.assertEqual(
            metrics["monthlyCosts"].source_metadata["component_labels"],
            ["Total Cost of Sales", "Total Operating Expenses"],
        )
        self.assertEqual(metrics["runway"].source_metadata["report_name"], "BalanceSheet")

    def test_xero_metric_publishing_profitable_month_omits_burn_and_keeps_monthly_costs(self):
        organization = Organization.objects.create(name="Acme", domain="acme.example")
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-123",
            account_label="Acme Xero",
            scopes=["accounting.reports.read"],
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            organization=organization,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
            external_account_id="tenant-123",
            external_record_id="invoice-current",
            currency="AUD",
            amount="5000.00",
            status="PAID",
            transaction_date=date(2026, 4, 15),
            description="April sales invoice",
            merchant_name="Acme Customer",
        )

        def fake_report(_connection, report_name, *, params=None):
            if report_name == "ProfitAndLoss":
                reports = {
                    "2026-04-01": _xero_profit_and_loss_report(
                        total_income="5000.00",
                        total_expenses="4000.00",
                        net_profit="1000.00",
                    ),
                    "2026-03-01": _xero_profit_and_loss_report(
                        total_income="4000.00",
                        total_expenses="3000.00",
                        net_profit="1000.00",
                    ),
                    "2026-02-01": _xero_profit_and_loss_report(
                        total_income="3000.00",
                        total_expenses="2500.00",
                        net_profit="500.00",
                    ),
                }
                return reports[params["fromDate"]]
            if report_name == "BalanceSheet":
                return _xero_balance_sheet_report(total_bank="12000.00")
            raise AssertionError(f"Unexpected report {report_name}")

        with patch("integrations.services.external_connectors.fetch_xero_accounting_report", side_effect=fake_report):
            publish_xero_metric_observations(
                organization=organization,
                run=None,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 30),
            )

        metrics = {
            metric.metric_key: metric
            for metric in StartupMetricObservation.objects.filter(
                organization=organization,
                source_provider=ExternalServiceProvider.XERO,
                period_month=date(2026, 4, 1),
            )
        }
        self.assertEqual(metrics["monthlyCosts"].value_text, "AUD 4000.00")
        self.assertEqual(metrics["operatingExpenses"].value_text, "AUD 4000.00")
        self.assertNotIn("burnRate", metrics)

    def test_xero_metric_publishing_without_reports_scope_keeps_operational_metrics(self):
        organization = Organization.objects.create(name="Acme", domain="acme.example")
        connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-123",
            account_label="Acme Xero",
            scopes=["accounting.transactions.read"],
        )
        ExternalFinancialRecord.objects.create(
            user=self.user,
            organization=organization,
            connection=connection,
            provider=ExternalServiceProvider.XERO,
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
            external_account_id="tenant-123",
            external_record_id="invoice-current",
            currency="AUD",
            amount="2500.00",
            status="PAID",
            transaction_date=date(2026, 4, 15),
            description="April sales invoice",
            merchant_name="Acme Customer",
        )

        with patch("integrations.services.external_connectors.fetch_xero_accounting_report") as mock_report:
            summary = publish_xero_metric_observations(
                organization=organization,
                run=None,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 30),
            )

        mock_report.assert_not_called()
        self.assertTrue(any("reconnect Xero" in warning for warning in summary["warnings"]))
        keys = set(
            StartupMetricObservation.objects.filter(
                organization=organization,
                source_provider=ExternalServiceProvider.XERO,
                period_month=date(2026, 4, 1),
            ).values_list("metric_key", flat=True)
        )
        self.assertIn("invoiceRevenue", keys)
        self.assertNotIn("revenue", keys)
