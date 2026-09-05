from datetime import date
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    ReconciliationProfile,
    StripePayoutReconciliation,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
    _stripe_statement_binding,
    build_xero_preview,
    post_xero_bank_transaction,
)
from integrations.services.xero_statement_reconciliation import (
    import_xero_statement_lines,
)
from organizations.models import Organization


User = get_user_model()


def _response(payload):
    return Mock(
        **{
            "json.return_value": payload,
            "raise_for_status.return_value": None,
        }
    )


class StripeStatementBindingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        user = User.objects.create_user(email="finance@example.com", slack_id="UFIN")
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-1",
            scopes=["accounting.banktransactions"],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.connection,
            xero_bank_account_id="profile-default-bank",
            revenue_account_code="200",
            fee_account_code="404",
            refund_account_code="200",
            revenue_tax_type="OUTPUT",
            fee_tax_type="INPUT",
            refund_tax_type="OUTPUT",
        )
        self.payout = StripePayoutReconciliation.objects.create(
            organization=self.organization,
            payout_id="po-bound",
            arrival_date=date(2026, 8, 14),
            currency="AUD",
            amount_cents=12110,
            report_payload={"deposit_cents": 12110, "revenue_groups": []},
        )
        self.line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-actual",
            expected_count=1,
            complete_scan=True,
            lines=[
                {
                    "statement_line_id": "line-stripe",
                    "date": "14 Aug 2026",
                    "narration": "STRIPE PAYOUT po-bound",
                    "direction": "credit",
                    "amount": "121.10",
                    "currency": "AUD",
                }
            ],
        )[0]
        self.binding = {
            "statement_line_id": self.line.statement_line_id,
            "bank_account_id": self.line.bank_account_id,
            "statement_source_hash": self.line.source_hash,
        }

    @patch(
        "integrations.services.xero_statement_reconciliation."
        "validate_current_statement_line_capture"
    )
    def test_bound_preview_uses_statement_rows_bank_account(self, validate_capture):
        validate_capture.return_value = Mock(capture_id="capture-1")

        preview = build_xero_preview(self.payout, **self.binding)

        self.assertEqual(
            preview["xero_payload"]["BankAccount"], {"AccountID": "bank-actual"}
        )
        self.assertEqual(preview["statement_binding"], self.binding)
        validate_capture.assert_called_once_with(
            self.line,
            expected_bank_account_id="bank-actual",
            expected_source_hash=self.line.source_hash,
            selection=None,
        )

    @patch(
        "integrations.services.xero_statement_reconciliation."
        "validate_current_statement_line_capture"
    )
    def test_binding_rejects_non_credit_or_green_row(self, validate_capture):
        validate_capture.return_value = Mock(capture_id="capture-1")
        self.line.direction = "debit"
        self.line.ui_mode = self.line.UI_GREEN_MATCH
        self.line.save(update_fields=["direction", "ui_mode"])

        with self.assertRaises(ReconciliationValidationError) as caught:
            _stripe_statement_binding(self.payout, **self.binding)

        joined = " ".join(caught.exception.errors)
        self.assertIn("must bind to a credit", joined)
        self.assertIn("already has a green match", joined)

    @patch(
        "integrations.services.xero_statement_reconciliation."
        "validate_current_statement_line_capture"
    )
    def test_binding_rejects_statement_row_from_a_different_date(
        self, validate_capture
    ):
        validate_capture.return_value = Mock(capture_id="capture-1")
        self.line.transaction_date = date(2026, 8, 13)
        self.line.save(update_fields=["transaction_date"])

        with self.assertRaises(ReconciliationValidationError) as caught:
            _stripe_statement_binding(self.payout, **self.binding)

        self.assertIn(
            "not the Stripe payout arrival date", " ".join(caught.exception.errors)
        )

    @patch(
        "integrations.services.xero_statement_reconciliation."
        "validate_current_statement_line_capture"
    )
    def test_preview_hash_binds_the_exact_statement_row(self, validate_capture):
        validate_capture.return_value = Mock(capture_id="capture-1")
        first = build_xero_preview(self.payout, **self.binding)

        self.line.statement_line_id = "line-stripe-alternate"
        self.line.source_hash = "b" * 64
        self.line.save(update_fields=["statement_line_id", "source_hash"])
        alternate_binding = {
            "statement_line_id": self.line.statement_line_id,
            "bank_account_id": self.line.bank_account_id,
            "statement_source_hash": self.line.source_hash,
        }
        second = build_xero_preview(self.payout, **alternate_binding)

        self.assertEqual(first["xero_payload"], second["xero_payload"])
        self.assertNotEqual(first["statement_binding"], second["statement_binding"])
        self.assertNotEqual(first["payload_hash"], second["payload_hash"])

    def test_post_revalidates_binding_immediately_before_bank_transaction_put(self):
        preview = {
            "ready": True,
            "errors": [],
            "payload_hash": "a" * 64,
            "xero_payload": {
                "Type": "RECEIVE",
                "Reference": "po-bound",
                "BankAccount": {"AccountID": "bank-actual"},
                "LineItems": [],
            },
        }
        initial_selection = Mock(name="initial-selection")
        fresh_selection = Mock(name="fresh-selection")
        with patch(
            "integrations.services.xero_reconciliation.build_xero_preview",
            return_value=preview,
        ), patch(
            "integrations.services.xero_reconciliation.ensure_xero_tracking_options"
        ), patch(
            "integrations.services.xero_reconciliation._stripe_statement_binding",
            side_effect=[
                self.line,
                ReconciliationValidationError(
                    "Stripe payout statement binding is not current."
                ),
            ],
        ) as validate_binding, patch(
            "integrations.services.xero_statement_reconciliation."
            "select_current_statement_capture",
            side_effect=[initial_selection, fresh_selection],
        ) as select_capture, patch(
            "integrations.services.xero_reconciliation.http_client.get",
            return_value=_response({"BankTransactions": []}),
        ), patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaisesMessage(
                ReconciliationValidationError,
                "statement binding is not current",
            ):
                post_xero_bank_transaction(
                    self.payout,
                    approved_by_slack_id="UFIN",
                    expected_payload_hash="a" * 64,
                    **self.binding,
                )

        self.assertEqual(validate_binding.call_count, 2)
        self.assertEqual(select_capture.call_count, 2)
        self.assertIs(
            validate_binding.call_args_list[-1].kwargs["statement_capture_selection"],
            fresh_selection,
        )
        put_mock.assert_not_called()

    def test_tracking_option_resolution_requires_a_new_hash_before_posting(self):
        old_preview = {
            "ready": True,
            "errors": [],
            "payload_hash": "a" * 64,
            "xero_payload": {"BankAccount": {"AccountID": "bank-actual"}},
        }
        resolved_preview = {
            **old_preview,
            "payload_hash": "b" * 64,
        }
        with patch(
            "integrations.services.xero_reconciliation.build_xero_preview",
            side_effect=[old_preview, resolved_preview],
        ), patch(
            "integrations.services.xero_reconciliation.ensure_xero_tracking_options"
        ), patch(
            "integrations.services.xero_reconciliation._stripe_statement_binding",
            return_value=self.line,
        ), patch(
            "integrations.services.xero_statement_reconciliation."
            "select_current_statement_capture",
            return_value=Mock(name="capture-selection"),
        ), patch(
            "integrations.services.xero_reconciliation.http_client.get"
        ) as get_mock, patch(
            "integrations.services.xero_reconciliation.http_client.put"
        ) as put_mock:
            with self.assertRaises(ReconciliationValidationError) as caught:
                post_xero_bank_transaction(
                    self.payout,
                    approved_by_slack_id="UFIN",
                    expected_payload_hash="a" * 64,
                    **self.binding,
                )

        self.assertEqual(
            caught.exception.errors,
            ["tracking_options_resolved_repreview_required"],
        )
        get_mock.assert_not_called()
        put_mock.assert_not_called()

    def test_posted_payout_is_recovered_only_for_its_stored_statement_binding(self):
        self.payout.xero_bank_transaction_id = "xero-bank-1"
        self.payout.preview_payload = {"statement_binding": self.binding}
        self.payout.save(
            update_fields=["xero_bank_transaction_id", "preview_payload", "updated_at"]
        )

        unbound_preview = build_xero_preview(self.payout)
        self.assertIsNone(unbound_preview["statement_binding"])
        self.payout.refresh_from_db()
        self.assertEqual(
            self.payout.preview_payload["statement_binding"], self.binding
        )

        recovered = post_xero_bank_transaction(
            self.payout,
            approved_by_slack_id="UFIN",
            expected_payload_hash="a" * 64,
            **self.binding,
        )

        self.assertEqual(recovered.xero_bank_transaction_id, "xero-bank-1")
        with self.assertRaisesMessage(
            ReconciliationValidationError,
            "different statement binding",
        ):
            post_xero_bank_transaction(
                self.payout,
                approved_by_slack_id="UFIN",
                expected_payload_hash="a" * 64,
                **{**self.binding, "bank_account_id": "another-bank"},
            )
