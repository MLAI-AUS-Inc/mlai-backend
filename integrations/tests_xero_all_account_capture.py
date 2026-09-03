from datetime import date
import hashlib
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from integrations.api_views_reconciliation import _reconciliation_run_retry_error
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    ReconciliationProfile,
    XeroStatementLineSnapshot,
    XeroStatementPosting,
    XeroStatementSuggestion,
)
from integrations.services.xero_statement_posting import _posting_payload
from integrations.services.xero_statement_reconciliation import (
    STATEMENT_CAPTURE_SOURCE_BROWSER,
    STATEMENT_CAPTURE_SOURCE_CSV,
    StatementCaptureValidationError,
    import_xero_statement_lines,
    select_current_statement_capture,
    validate_current_statement_line_capture,
)
from organizations.models import Organization
from roo.models import PointsAdmin
from workflow_runs.models import ContentFactoryRun


User = get_user_model()


class AllAccountCaptureMixin:
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="capture@example.com", slack_id="UCAPTURE")
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-1",
            account_label="MLAI Tenant",
            scopes=["accounting.banktransactions", "accounting.payments"],
        )
        self.profile = ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.connection,
            xero_bank_account_id="bank-a",
        )

    @staticmethod
    def xero_accounts():
        return [
            {"AccountID": "bank-a", "Name": "Operating", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "bank-b", "Name": "Savings", "Type": "BANK", "Status": "ACTIVE"},
        ]

    def capture_metadata(
        self,
        *,
        capture_id: str,
        bank_account_id: str,
        position: int,
        source: str = STATEMENT_CAPTURE_SOURCE_BROWSER,
        complete: bool = True,
        account_source_sha256: str = "",
    ):
        account_ids = ["bank-a", "bank-b"]
        names = {"bank-a": "Operating", "bank-b": "Savings"}
        metadata = {
            "schema_version": 2,
            "capture_source": source,
            "capture_id": capture_id,
            "scan_id": f"{capture_id}-{bank_account_id}",
            "account_source_sha256": account_source_sha256 or ("a" if position == 1 else "b") * 64,
            "report_format": (
                "xero-uncoded-lines-grouped-v1"
                if source == STATEMENT_CAPTURE_SOURCE_CSV
                else "xero_bank_reconciliation_dom"
            ),
            "tenant_id": "tenant-1",
            "organisation_name": "MLAI Tenant",
            "bank_account_label": names[bank_account_id],
            "account_position": position,
            "account_count": 2,
            "active_bank_account_ids": account_ids,
            "all_accounts_requested": True,
            "full_organisation_coverage_confirmed": complete,
            "date_range_confirmed": True,
            "derived_complete": complete,
            "blocking_reasons": [] if complete else ["capture interrupted"],
        }
        if source == STATEMENT_CAPTURE_SOURCE_CSV:
            metadata.update({
                "source_sha256": "c" * 64,
                "period_start": "2026-07-01",
                "period_end": "2026-08-31",
            })
        return metadata

    def import_capture(self, capture_id="capture-1"):
        lines = []
        for position, bank_account_id in enumerate(("bank-a", "bank-b"), start=1):
            raw_lines = [{
                "statement_line_id": f"line-{capture_id}-{position}",
                "date": "20 Jul 2026",
                "narration": f"Vendor {position}",
                "direction": "debit",
                "amount": f"{position}.00",
            }]
            account_source_sha256 = hashlib.sha256(
                json.dumps(
                    raw_lines,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            lines.append(import_xero_statement_lines(
                organization=self.organization,
                bank_account_id=bank_account_id,
                expected_count=1,
                complete_scan=True,
                requested_by="UADMIN",
                capture_metadata=self.capture_metadata(
                    capture_id=capture_id,
                    bank_account_id=bank_account_id,
                    position=position,
                    account_source_sha256=account_source_sha256,
                ),
                lines=raw_lines,
            )[0])
        return lines

    def import_empty_capture(self, capture_id="empty-capture"):
        account_source_sha256 = hashlib.sha256(b"[]").hexdigest()
        for position, bank_account_id in enumerate(("bank-a", "bank-b"), start=1):
            import_xero_statement_lines(
                organization=self.organization,
                bank_account_id=bank_account_id,
                expected_count=0,
                complete_scan=True,
                requested_by="UADMIN",
                capture_metadata=self.capture_metadata(
                    capture_id=capture_id,
                    bank_account_id=bank_account_id,
                    position=position,
                    account_source_sha256=account_source_sha256,
                ),
                lines=[],
            )


class AllAccountCaptureServiceTests(AllAccountCaptureMixin, TestCase):
    def test_browser_capture_may_omit_period_but_csv_may_not(self):
        raw_lines = [{
            "statement_line_id": "browser-line",
            "date": "20 Jul 2026",
            "narration": "Vendor",
            "direction": "debit",
            "amount": "1.00",
        }]
        account_source_sha256 = hashlib.sha256(
            json.dumps(
                raw_lines,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        browser = self.capture_metadata(
            capture_id="browser-without-period",
            bank_account_id="bank-a",
            position=1,
            account_source_sha256=account_source_sha256,
        )
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-a",
            expected_count=1,
            complete_scan=True,
            capture_metadata=browser,
            lines=raw_lines,
        )[0]
        self.assertEqual(line.last_scan.capture_metadata["period_start"], "")
        self.assertEqual(
            line.last_scan.capture_metadata["capture_source"],
            STATEMENT_CAPTURE_SOURCE_BROWSER,
        )

        csv_metadata = self.capture_metadata(
            capture_id="csv-without-period",
            bank_account_id="bank-a",
            position=1,
            source=STATEMENT_CAPTURE_SOURCE_CSV,
        )
        csv_metadata.pop("period_start")
        csv_metadata.pop("period_end")
        with self.assertRaisesMessage(ValueError, "require period_start and period_end"):
            import_xero_statement_lines(
                organization=self.organization,
                bank_account_id="bank-a",
                expected_count=0,
                complete_scan=True,
                capture_metadata=csv_metadata,
                lines=[],
            )

    @patch("integrations.api_views_reconciliation.fetch_active_xero_bank_accounts")
    def test_selects_every_account_and_validates_exact_line_membership(self, fetch_accounts):
        fetch_accounts.return_value = self.xero_accounts()
        lines = self.import_capture()

        selection = select_current_statement_capture(self.organization)

        self.assertTrue(selection.all_account_capture)
        self.assertEqual(len(selection.scan_ids), 2)
        self.assertEqual(len(selection.capture_fingerprint), 64)
        self.assertEqual(
            [account["bank_account_id"] for account in selection.active_bank_accounts],
            ["bank-a", "bank-b"],
        )
        validated = validate_current_statement_line_capture(
            lines[1],
            expected_bank_account_id="bank-b",
            expected_source_hash=lines[1].source_hash,
            selection=selection,
        )
        self.assertEqual(validated.capture_id, "capture-1")
        with self.assertRaises(StatementCaptureValidationError):
            validate_current_statement_line_capture(
                lines[1],
                expected_bank_account_id="bank-a",
                selection=selection,
            )
        self.assertEqual(fetch_accounts.call_count, 1)

    @patch("integrations.services.xero_reconciliation.fetch_xero_accounts")
    def test_newer_partial_batch_fails_closed_instead_of_reusing_old_capture(self, fetch_accounts):
        fetch_accounts.return_value = self.xero_accounts()
        self.import_capture("complete-before-partial")
        import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-a",
            expected_count=None,
            complete_scan=False,
            capture_metadata=self.capture_metadata(
                capture_id="partial-latest",
                bank_account_id="bank-a",
                position=1,
                complete=False,
            ),
            lines=[],
        )

        selection = select_current_statement_capture(self.organization)

        self.assertFalse(selection.all_account_capture)
        self.assertTrue(any("partial" in blocker for blocker in selection.blockers))

    def test_posting_payload_uses_captured_bank_account_not_profile_default(self):
        line = XeroStatementLineSnapshot.objects.create(
            organization=self.organization,
            bank_account_id="bank-b",
            statement_line_id="posting-bank-b",
            transaction_date=date(2026, 7, 20),
            narration="Vendor",
            direction=XeroStatementLineSnapshot.DIRECTION_DEBIT,
            amount="10.00",
            source_hash="d" * 64,
        )
        suggestion = XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=line,
            proposed_action=XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
            contact_name="Vendor",
            account_code="404",
            tax_type="BASEXCLUDED",
            description="Expense",
            source_hash=line.source_hash,
        )

        payload = _posting_payload(
            suggestion=suggestion,
            profile=self.profile,
            operation=XeroStatementPosting.OPERATION_BANK_TRANSACTION,
        )

        self.assertEqual(payload["BankAccount"]["AccountID"], "bank-b")


class AllAccountCaptureApiTests(AllAccountCaptureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        PointsAdmin.objects.create(slack_user_id="UADMIN", role="admin", is_active=True)

    @patch("integrations.services.xero_reconciliation.fetch_xero_accounts")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_bank_account_catalog_returns_only_live_active_banks(self, _permission, fetch_accounts):
        fetch_accounts.return_value = [
            {"bank_account_id": "bank-a", "name": "Operating"},
            {"bank_account_id": "bank-b", "name": "Savings"},
        ]

        response = self.client.get(
            reverse("reconciliation_bank_accounts"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {
            "schema_version": 1,
            "tenant_id": "tenant-1",
            "organisation_name": "MLAI Tenant",
            "accounts": [
                {"bank_account_id": "bank-a", "name": "Operating"},
                {"bank_account_id": "bank-b", "name": "Savings"},
            ],
        })

    @patch("integrations.api_views_reconciliation.fetch_active_xero_bank_accounts")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_bank_account_catalog_allows_duplicate_names_with_unique_ids(self, _permission, fetch_accounts):
        fetch_accounts.return_value = [
            {"bank_account_id": "bank-a", "name": "Operating"},
            {"bank_account_id": "bank-b", "name": "Operating"},
        ]

        response = self.client.get(
            reverse("reconciliation_bank_accounts"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [account["bank_account_id"] for account in response.data["accounts"]],
            ["bank-a", "bank-b"],
        )

    @patch("integrations.services.xero_reconciliation.fetch_xero_accounts")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_readiness_and_run_use_exact_all_account_scan_set(self, _permission, fetch_accounts):
        fetch_accounts.return_value = self.xero_accounts()
        lines = self.import_capture()

        readiness = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        started = self.client.post(
            reverse("reconciliation_agent_runs"),
            {
                "slack_user_id": "UADMIN",
                "domain": "mlai.au",
                "analysis_mode": "external_agent",
            },
            format="json",
        )

        self.assertEqual(readiness.status_code, status.HTTP_200_OK)
        self.assertTrue(readiness.data["all_account_capture"])
        self.assertEqual(len(readiness.data["selected_statement_scan_ids"]), 2)
        self.assertEqual(started.status_code, status.HTTP_201_CREATED)
        self.assertEqual(started.data["statement_scan_ids"], readiness.data["selected_statement_scan_ids"])
        run = ContentFactoryRun.objects.get(run_id=started.data["run_id"])
        self.assertEqual(
            set(run.run_request["requested_statement_line_ids"]),
            {line.statement_line_id for line in lines},
        )
        self.assertEqual(
            run.run_request["statement_capture_fingerprint"],
            readiness.data["statement_capture"]["capture_fingerprint"],
        )
        self.assertEqual(
            _reconciliation_run_retry_error(organization=self.organization, run=run),
            "",
        )

    @patch("integrations.services.xero_reconciliation.fetch_xero_accounts")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_empty_complete_capture_is_ready_noop_not_a_retry_blocker(self, _permission, fetch_accounts):
        fetch_accounts.return_value = self.xero_accounts()
        self.import_empty_capture()

        readiness = self.client.get(
            reverse("reconciliation_readiness"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )

        self.assertEqual(readiness.status_code, status.HTTP_200_OK)
        self.assertTrue(readiness.data["all_account_capture"])
        self.assertTrue(readiness.data["ready_to_start"])
        self.assertEqual(readiness.data["latest_statement_scan"]["candidate_count"], 0)
        self.assertEqual(readiness.data["blockers"], [])
        self.assertEqual(
            readiness.data["recommended_next_action"],
            "No unreconciled statement candidates need action.",
        )
