import base64
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from organizations.models import Organization
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnection,
    ExternalServiceProvider,
    ReconciliationProfile,
    XeroStatementSuggestion,
)
from integrations.services.xero_reconciliation import (
    ReconciliationValidationError,
    XeroPostingError,
)
from integrations.services.xero_bill_intake import (
    attach_reconciliation_document,
    build_reconciliation_bill_preview,
    create_reconciliation_bill,
)
from integrations.services.xero_statement_reconciliation import (
    ALLOWED_STATEMENT_EVIDENCE_PROVIDERS,
    _serialize_evidence,
    import_xero_statement_lines,
)
from integrations.services.xero_statement_posting import (
    _ensure_bill_tracking,
    resolve_xero_tracking_assignment,
)
from roo.models import PointsAdmin


User = get_user_model()

FULL_SCOPES = [
    "offline_access",
    "accounting.transactions",
    "accounting.banktransactions",
    "accounting.payments",
    "accounting.attachments",
    "accounting.contacts.read",
]


def _response(payload):
    return Mock(**{"json.return_value": payload, "raise_for_status.return_value": None})


def _bill_payload(**overrides):
    payload = {
        "contact_name": "Linear Orbit",
        "invoice_number": "RVVBQKKP-0012",
        "issue_date": "2026-07-20",
        "due_date": "2026-08-03",
        "currency": "AUD",
        "total": "529.08",
        "description": "Software subscription",
        "source": {"gmail_message_id": "gm-123", "document_id": 7},
    }
    payload.update(overrides)
    return payload


class XeroBillIntakeServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="finance@example.com", slack_id="UFIN")
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-1",
            scopes=list(FULL_SCOPES),
        )
        self.profile = ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.connection,
            xero_bank_account_id="bank-1",
        )

    def test_preview_requires_contact_invoice_and_date(self):
        preview = build_reconciliation_bill_preview(self.organization, payload={})
        self.assertFalse(preview["ready"])
        joined = " ".join(preview["errors"])
        self.assertIn("contact_name", joined)
        self.assertIn("invoice_number", joined)
        self.assertIn("issue_date", joined)

    def test_mandatory_tracking_is_added_to_every_bill_line(self):
        self.profile.require_statement_tracking = True
        self.profile.default_project_tracking_option_name = "MLAI core"
        self.profile.default_project_tracking_option_id = "project-core"
        self.profile.project_tracking_category_id = "project-category"
        self.profile.save(update_fields=[
            "require_statement_tracking",
            "default_project_tracking_option_name",
            "default_project_tracking_option_id",
            "project_tracking_category_id",
            "updated_at",
        ])
        payload = _bill_payload(
            total="12.34",
            line_amounts=[
                {"description": "Venue", "amount": "10.00", "account_code": "429", "tax_type": "INPUT"},
                {"description": "Catering", "amount": "2.34", "account_code": "401", "tax_type": "INPUT"},
            ],
            effective_tracking={
                "allocation_mode": "mlai_core",
                "kind": "project",
                "option_name": "MLAI core",
                "option_id": "project-core",
                "default": True,
            },
        )

        preview = build_reconciliation_bill_preview(self.organization, payload=payload)

        self.assertTrue(preview["ready"])
        for line in preview["xero_payload"]["LineItems"]:
            self.assertEqual(len(line["Tracking"]), 1)
            self.assertEqual(line["Tracking"][0]["Option"], "MLAI core")

    @patch("integrations.services.xero_statement_posting.http_client.post")
    def test_existing_bill_tracking_update_preserves_line_financial_fields(self, post):
        response = Mock()
        response.json.return_value = {"Invoices": [{"InvoiceID": "bill-1"}]}
        response.raise_for_status.return_value = None
        post.return_value = response
        bill = {
            "InvoiceID": "bill-1",
            "Type": "ACCPAY",
            "Status": "AUTHORISED",
            "Date": "2026-08-01",
            "DueDate": "2026-08-14",
            "LineAmountTypes": "Inclusive",
            "Contact": {"ContactID": "contact-1"},
            "LineItems": [{
                "LineItemID": "line-1",
                "Description": "Venue hire",
                "Quantity": 1,
                "UnitAmount": 100.0,
                "AccountCode": "429",
                "TaxType": "INPUT",
            }],
        }
        tracking = {
            "TrackingCategoryID": "project-category",
            "Name": "Project Name",
            "TrackingOptionID": "project-core",
            "Option": "MLAI core",
        }

        _ensure_bill_tracking(self.connection, bill=bill, tracking=tracking)

        sent = post.call_args.kwargs["json"]["Invoices"][0]["LineItems"][0]
        self.assertEqual(sent["LineItemID"], "line-1")
        self.assertEqual(sent["UnitAmount"], 100.0)
        self.assertEqual(sent["AccountCode"], "429")
        self.assertEqual(sent["TaxType"], "INPUT")
        self.assertEqual(sent["Tracking"], [tracking])

    def test_existing_bill_conflicting_tracking_fails_without_write(self):
        bill = {
            "InvoiceID": "bill-1",
            "LineItems": [{
                "LineItemID": "line-1",
                "Description": "Venue hire",
                "Quantity": 1,
                "UnitAmount": 100.0,
                "AccountCode": "429",
                "TaxType": "INPUT",
                "Tracking": [{
                    "TrackingCategoryID": "event-category",
                    "TrackingOptionID": "event-other",
                    "Option": "Other Event",
                }],
            }],
        }
        with self.assertRaisesMessage(XeroPostingError, "conflicts"):
            _ensure_bill_tracking(
                self.connection,
                bill=bill,
                tracking={
                    "TrackingCategoryID": "project-category",
                    "Name": "Project Name",
                    "TrackingOptionID": "project-core",
                    "Option": "MLAI core",
                },
            )

    @patch("integrations.services.xero_statement_posting.http_client.put")
    @patch("integrations.services.xero_statement_posting.http_client.get")
    def test_missing_mlai_core_is_created_only_when_resolving_approved_write(self, get, put):
        self.connection.scopes = [*self.connection.scopes, "accounting.settings"]
        self.connection.save(update_fields=["scopes", "updated_at"])
        self.profile.default_project_tracking_option_name = "MLAI core"
        self.profile.project_tracking_category_id = "project-category"
        self.profile.save(update_fields=[
            "default_project_tracking_option_name",
            "project_tracking_category_id",
            "updated_at",
        ])
        categories = Mock()
        categories.json.return_value = {
            "TrackingCategories": [{
                "TrackingCategoryID": "project-category",
                "Status": "ACTIVE",
                "Options": [],
            }]
        }
        categories.raise_for_status.return_value = None
        get.return_value = categories
        created = Mock()
        created.json.return_value = {
            "Options": [{"TrackingOptionID": "project-core", "Name": "MLAI core"}]
        }
        created.raise_for_status.return_value = None
        put.return_value = created

        resolved = resolve_xero_tracking_assignment(
            self.connection,
            self.profile,
            {
                "allocation_mode": "mlai_core",
                "kind": "project",
                "category_id": "project-category",
                "category_name": "Project Name",
                "option_id": "",
                "option_name": "MLAI core",
                "default": True,
            },
        )

        self.assertEqual(resolved[0]["TrackingOptionID"], "project-core")
        put.assert_called_once()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.default_project_tracking_option_id, "project-core")

    def test_preview_downgrades_authorised_without_account_fields(self):
        preview = build_reconciliation_bill_preview(
            self.organization, payload=_bill_payload(status="AUTHORISED")
        )
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["status"], "DRAFT")
        self.assertTrue(any("Downgraded to DRAFT" in item for item in preview["warnings"]))
        self.assertEqual(preview["xero_payload"]["Status"], "DRAFT")

    def test_preview_preserves_extracted_lines_and_financial_metadata(self):
        preview = build_reconciliation_bill_preview(
            self.organization,
            payload=_bill_payload(
                total="110.00",
                subtotal="100.00",
                tax_amount="10.00",
                amount_due="110.00",
                line_amounts=[
                    {
                        "description": "Venue hire",
                        "quantity": "2",
                        "unit_amount": "50.00",
                        "amount": "100.00",
                        "account_code": "412",
                        "tax_type": "INPUT",
                    },
                    {
                        "description": "Booking fee",
                        "amount": "10.00",
                        "account_code": "412",
                        "tax_type": "INPUT",
                    },
                ],
                source={"document_id": 7, "source_sha256": "a" * 64},
            ),
        )

        self.assertTrue(preview["ready"])
        self.assertEqual(preview["xero_payload"]["LineItems"][0]["Quantity"], 2.0)
        self.assertEqual(preview["xero_payload"]["LineItems"][0]["UnitAmount"], 50.0)
        self.assertEqual(preview["document_metadata"]["subtotal"], "100.00")
        self.assertEqual(preview["document_metadata"]["tax_amount"], "10.00")
        self.assertEqual(preview["document_metadata"]["source"]["source_sha256"], "a" * 64)

    def test_preview_strips_control_characters_from_invoice_number(self):
        preview = build_reconciliation_bill_preview(
            self.organization,
            payload=_bill_payload(invoice_number="RJANGUWO\x000005"),
        )
        self.assertEqual(preview["invoice_number"], "RJANGUWO0005")
        self.assertEqual(preview["xero_payload"]["InvoiceNumber"], "RJANGUWO0005")

    def test_preview_requires_invoice_write_scope(self):
        self.connection.scopes = ["accounting.banktransactions"]
        self.connection.save(update_fields=["scopes"])
        preview = build_reconciliation_bill_preview(self.organization, payload=_bill_payload())
        self.assertFalse(preview["ready"])
        self.assertTrue(any("accounting.transactions" in item for item in preview["errors"]))
        with self.assertRaises(ReconciliationValidationError):
            create_reconciliation_bill(
                self.organization, payload=_bill_payload(), requested_by_slack_id="UADMIN"
            )

    def test_create_bill_posts_draft_and_reports_reference(self):
        created_row = {
            "InvoiceID": "inv-123",
            "InvoiceNumber": "RVVBQKKP-0012",
            "Status": "DRAFT",
            "Contact": {"Name": "Linear Orbit"},
            "Total": 529.08,
            "AmountDue": 529.08,
            "CurrencyCode": "AUD",
            "DateString": "2026-07-20T00:00:00",
            "DueDateString": "2026-08-03T00:00:00",
            "LineItems": [{"Description": "Software subscription"}],
        }
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            side_effect=[_response({"Invoices": []}), _response({"Contacts": []})],
        ) as get_mock, patch(
            "integrations.services.xero_bill_intake.http_client.put",
            return_value=_response({"Invoices": [created_row]}),
        ) as put_mock:
            result = create_reconciliation_bill(
                self.organization, payload=_bill_payload(), requested_by_slack_id="UADMIN"
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["bill"]["xero_invoice_id"], "inv-123")
        self.assertEqual(result["bill"]["status"], "DRAFT")
        self.assertEqual(get_mock.call_count, 2)
        sent = put_mock.call_args.kwargs["json"]["Invoices"][0]
        self.assertEqual(sent["Type"], "ACCPAY")
        self.assertEqual(sent["InvoiceNumber"], "RVVBQKKP-0012")
        self.assertEqual(sent["Contact"], {"Name": "Linear Orbit"})
        self.assertEqual(sent["Reference"], "treasurer-inbox:gm-123")
        headers = put_mock.call_args.kwargs["headers"]
        self.assertTrue(headers["Idempotency-Key"].startswith("mlai-bill-"))
        self.assertFalse(ExternalFinancialRecord.objects.exists())

    def test_create_bill_reuses_existing_contact_id(self):
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            side_effect=[
                _response({"Invoices": []}),
                _response({"Contacts": [{"ContactID": "c-9", "Name": "Linear Orbit"}]}),
            ],
        ), patch(
            "integrations.services.xero_bill_intake.http_client.put",
            return_value=_response({"Invoices": [{"InvoiceID": "inv-1", "Status": "DRAFT"}]}),
        ) as put_mock:
            create_reconciliation_bill(
                self.organization, payload=_bill_payload(), requested_by_slack_id="UADMIN"
            )
        sent = put_mock.call_args.kwargs["json"]["Invoices"][0]
        self.assertEqual(sent["Contact"], {"ContactID": "c-9"})

    def test_create_bill_idempotent_for_same_contact_and_total(self):
        existing = {
            "InvoiceID": "inv-existing",
            "InvoiceNumber": "RVVBQKKP-0012",
            "Status": "AUTHORISED",
            "Contact": {"Name": "linear orbit"},
            "Total": 529.08,
            "AmountDue": 529.08,
            "CurrencyCode": "AUD",
            "DateString": "2026-07-20T00:00:00",
        }
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            return_value=_response({"Invoices": [existing]}),
        ), patch("integrations.services.xero_bill_intake.http_client.put") as put_mock:
            result = create_reconciliation_bill(
                self.organization, payload=_bill_payload(), requested_by_slack_id="UADMIN"
            )
        self.assertFalse(result["created"])
        self.assertEqual(result["bill"]["xero_invoice_id"], "inv-existing")
        put_mock.assert_not_called()
        # The authorised bill enters the local mirror so bill matching sees it.
        record = ExternalFinancialRecord.objects.get()
        self.assertEqual(record.record_type, ExternalFinancialRecord.RECORD_XERO_BILL)
        self.assertEqual(record.amount, Decimal("529.08"))

    def test_create_bill_conflicting_total_raises(self):
        existing = {
            "InvoiceID": "inv-existing",
            "Status": "AUTHORISED",
            "Contact": {"Name": "Linear Orbit"},
            "Total": 100.00,
        }
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            return_value=_response({"Invoices": [existing]}),
        ), patch("integrations.services.xero_bill_intake.http_client.put") as put_mock:
            with self.assertRaises(XeroPostingError):
                create_reconciliation_bill(
                    self.organization, payload=_bill_payload(), requested_by_slack_id="UADMIN"
                )
        put_mock.assert_not_called()

    def test_create_authorised_bill_mirrors_external_record(self):
        created_row = {
            "InvoiceID": "inv-77",
            "InvoiceNumber": "AA-1",
            "Status": "AUTHORISED",
            "Contact": {"Name": "Aaron AI"},
            "Total": 1137.50,
            "AmountDue": 1137.50,
            "CurrencyCode": "AUD",
            "DateString": "2026-07-18T00:00:00",
            "LineItems": [{"Description": "AI consulting"}],
        }
        payload = _bill_payload(
            contact_name="Aaron AI",
            invoice_number="AA-1",
            total="1137.50",
            status="AUTHORISED",
            account_code="405",
            tax_type="INPUT",
        )
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            side_effect=[
                _response({"Invoices": []}),
                _response({"Contacts": []}),
                _response({"Accounts": [{"Code": "405", "Status": "ACTIVE"}]}),
                _response({
                    "TaxRates": [{
                        "Name": "GST on Expenses",
                        "TaxType": "INPUT",
                        "Status": "ACTIVE",
                        "CanApplyToExpenses": True,
                    }]
                }),
            ],
        ), patch(
            "integrations.services.xero_bill_intake.http_client.put",
            return_value=_response({"Invoices": [created_row]}),
        ) as put_mock:
            result = create_reconciliation_bill(
                self.organization, payload=payload, requested_by_slack_id="UADMIN"
            )

        self.assertTrue(result["created"])
        sent = put_mock.call_args.kwargs["json"]["Invoices"][0]
        self.assertEqual(sent["Status"], "AUTHORISED")
        self.assertEqual(sent["LineItems"][0]["AccountCode"], "405")
        record = ExternalFinancialRecord.objects.get()
        self.assertEqual(record.record_type, ExternalFinancialRecord.RECORD_XERO_BILL)
        self.assertEqual(record.amount, Decimal("1137.50"))
        self.assertEqual(record.direction, "debit")
        self.assertEqual(record.merchant_name, "Aaron AI")
        self.assertEqual(
            record.raw_payload["_mlai_document_metadata"]["total"],
            "1137.50",
        )

    def test_authorised_bill_requires_current_xero_account_and_tax(self):
        payload = _bill_payload(
            status="AUTHORISED",
            account_code="missing",
            tax_type="INPUT",
        )
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            side_effect=[
                _response({"Invoices": []}),
                _response({"Contacts": []}),
                _response({"Accounts": []}),
                _response({"TaxRates": []}),
            ],
        ), patch("integrations.services.xero_bill_intake.http_client.put") as put_mock:
            with self.assertRaisesMessage(XeroPostingError, "one active account"):
                create_reconciliation_bill(
                    self.organization,
                    payload=payload,
                    requested_by_slack_id="UADMIN",
                )
        put_mock.assert_not_called()

    def test_attach_document_puts_raw_content(self):
        content = base64.b64encode(b"%PDF-1.4 fake").decode()
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            return_value=_response({"Attachments": []}),
        ), patch(
            "integrations.services.xero_bill_intake.http_client.put",
            return_value=_response({
                "Attachments": [{"AttachmentID": "att-1", "FileName": "invoice.pdf", "ContentLength": 13}]
            }),
        ) as put_mock:
            result = attach_reconciliation_document(
                self.organization,
                payload={
                    "xero_entity_type": "invoice",
                    "xero_id": "11111111-2222-3333-4444-555555555555",
                    "filename": "invoice.pdf",
                    "content_base64": content,
                    "size_bytes": 13,
                    "content_sha256": "932d2676c1e461ba50d559bba416fbc6af8da1f74309ae81370c615223d0e349",
                    "confirm": True,
                },
                requested_by_slack_id="UADMIN",
            )
        self.assertTrue(result["created"])
        self.assertEqual(result["attachment"]["attachment_id"], "att-1")
        url = put_mock.call_args.args[0]
        self.assertIn("/Invoices/11111111-2222-3333-4444-555555555555/Attachments/invoice.pdf", url)
        self.assertEqual(put_mock.call_args.kwargs["data"], b"%PDF-1.4 fake")
        self.assertEqual(put_mock.call_args.kwargs["headers"]["Content-Type"], "application/pdf")
        self.assertTrue(
            put_mock.call_args.kwargs["headers"]["Idempotency-Key"].startswith(
                "mlai-attachment-"
            )
        )
        self.assertEqual(
            result["attachment"]["content_sha256"],
            "932d2676c1e461ba50d559bba416fbc6af8da1f74309ae81370c615223d0e349",
        )

    def test_attach_document_idempotent_by_filename(self):
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            return_value=_response({
                "Attachments": [{"AttachmentID": "att-1", "FileName": "Invoice.PDF", "ContentLength": 1}]
            }),
        ), patch("integrations.services.xero_bill_intake.http_client.put") as put_mock:
            result = attach_reconciliation_document(
                self.organization,
                payload={
                    "xero_entity_type": "invoice",
                    "xero_id": "11111111-2222-3333-4444-555555555555",
                    "filename": "invoice.pdf",
                    "content_base64": base64.b64encode(b"x").decode(),
                    "size_bytes": 1,
                    "content_sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
                },
                requested_by_slack_id="UADMIN",
            )
        self.assertFalse(result["created"])
        put_mock.assert_not_called()

    def test_attach_document_blocks_same_filename_with_different_size(self):
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            return_value=_response({
                "Attachments": [{
                    "AttachmentID": "att-existing",
                    "FileName": "invoice.pdf",
                    "ContentLength": 99,
                }]
            }),
        ), patch("integrations.services.xero_bill_intake.http_client.put") as put_mock:
            with self.assertRaisesMessage(
                ReconciliationValidationError,
                "different content",
            ):
                attach_reconciliation_document(
                    self.organization,
                    payload={
                        "xero_entity_type": "invoice",
                        "xero_id": "11111111-2222-3333-4444-555555555555",
                        "filename": "invoice.pdf",
                        "content_base64": base64.b64encode(b"x").decode(),
                        "size_bytes": 1,
                        "content_sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
                    },
                    requested_by_slack_id="UADMIN",
                )
        put_mock.assert_not_called()

    @override_settings(XERO_ATTACHMENT_MAX_BYTES=8)
    def test_attach_document_validates_input(self):
        oversized = base64.b64encode(b"123456789").decode()
        with self.assertRaises(ReconciliationValidationError) as caught:
            attach_reconciliation_document(
                self.organization,
                payload={
                    "xero_entity_type": "receipt",
                    "xero_id": "not-a-guid!",
                    "filename": "invoice",
                    "content_base64": oversized,
                    "size_bytes": 9,
                    "content_sha256": "15e2b0d3c33891ebb0f1ef609ec419420c20e320ce94c65fbc8c3312448eb225",
                },
                requested_by_slack_id="UADMIN",
            )
        joined = " ".join(caught.exception.errors)
        self.assertIn("xero_entity_type", joined)
        self.assertIn("xero_id", joined)
        self.assertIn("filename", joined)
        self.assertIn("limit", joined)

    def test_attach_document_requires_attachments_scope(self):
        self.connection.scopes = ["accounting.transactions"]
        self.connection.save(update_fields=["scopes"])
        with self.assertRaises(ReconciliationValidationError) as caught:
            attach_reconciliation_document(
                self.organization,
                payload={
                    "xero_entity_type": "invoice",
                    "xero_id": "11111111-2222-3333-4444-555555555555",
                    "filename": "invoice.pdf",
                    "content_base64": base64.b64encode(b"x").decode(),
                    "size_bytes": 1,
                    "content_sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
                },
                requested_by_slack_id="UADMIN",
            )
        self.assertTrue(any("accounting.attachments" in item for item in caught.exception.errors))

    def test_document_evidence_provider_is_accepted(self):
        self.assertIn("document", ALLOWED_STATEMENT_EVIDENCE_PROVIDERS)
        entries = _serialize_evidence([
            {"source_provider": "document", "source_record_id": "42", "summary": "Invoice AA-1"},
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_provider"], "document")


class XeroBillIntakeApiTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="agent@example.com", slack_id="UAGENT")
        PointsAdmin.objects.create(slack_user_id="UADMIN", role="admin", is_active=True)
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-1",
            scopes=list(FULL_SCOPES),
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.connection,
            xero_bank_account_id="bank-1",
        )

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_draft_bill_requires_confirm(self, _permission):
        response = self.client.post(
            reverse("reconciliation_draft_bills"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", **_bill_payload()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("confirm", response.data["error"])

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_draft_bill_dry_run_returns_preview(self, _permission):
        response = self.client.post(
            reverse("reconciliation_draft_bills"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au", "dry_run": True, **_bill_payload()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["dry_run"])
        self.assertTrue(response.data["ready"])
        self.assertEqual(response.data["xero_payload"]["Type"], "ACCPAY")

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_draft_bill_creates_bill(self, _permission):
        created_row = {
            "InvoiceID": "inv-123",
            "InvoiceNumber": "RVVBQKKP-0012",
            "Status": "DRAFT",
            "Contact": {"Name": "Linear Orbit"},
            "Total": 529.08,
            "AmountDue": 529.08,
        }
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            side_effect=[_response({"Invoices": []}), _response({"Contacts": []})],
        ), patch(
            "integrations.services.xero_bill_intake.http_client.put",
            return_value=_response({"Invoices": [created_row]}),
        ):
            response = self.client.post(
                reverse("reconciliation_draft_bills"),
                {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True, **_bill_payload()},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["created"])
        self.assertEqual(response.data["bill"]["xero_invoice_id"], "inv-123")

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_attachment_endpoint_creates(self, _permission):
        with patch(
            "integrations.services.xero_bill_intake.http_client.get",
            return_value=_response({"Attachments": []}),
        ), patch(
            "integrations.services.xero_bill_intake.http_client.put",
            return_value=_response({
                "Attachments": [{"AttachmentID": "att-9", "FileName": "invoice.pdf", "ContentLength": 4}]
            }),
        ):
            response = self.client.post(
                reverse("reconciliation_xero_attachments"),
                {
                    "slack_user_id": "UADMIN",
                    "domain": "mlai.au",
                    "confirm": True,
                    "xero_entity_type": "invoice",
                    "xero_id": "11111111-2222-3333-4444-555555555555",
                    "filename": "invoice.pdf",
                    "content_base64": base64.b64encode(b"%PDF").decode(),
                    "size_bytes": 4,
                    "content_sha256": "315d429b7714cedb6ad04ac31240145257692630457f3c88253c5beceac76027",
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["attachment"]["attachment_id"], "att-9")

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_execute_view_returns_posting_reference(self, _permission):
        line = import_xero_statement_lines(
            organization=self.organization,
            bank_account_id="bank-1",
            expected_count=1,
            requested_by="UADMIN",
            lines=[{
                "statement_line_id": "line-1",
                "date": "20 Jul 2026",
                "narration": "Transfer To CONTRACTOR ONE",
                "direction": "debit",
                "amount": "845.00",
            }],
        )[0]
        suggestion = XeroStatementSuggestion.objects.create(
            organization=self.organization,
            statement_line=line,
            run_id="run-1",
            proposed_action=XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
            contact_name="Contractor One",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="INPUT",
            description="Contractor work.",
            confidence=0.99,
            source_hash=line.source_hash,
            evidence=[{"source_provider": "xero_ui", "source_record_id": "line-1"}],
        )
        posting = Mock(
            id=7,
            operation="bank_transaction",
            status="match_ready",
            xero_bank_transaction_id="bt-1",
            xero_payment_id="",
            xero_bill_id="",
        )
        with patch(
            "integrations.api_views_reconciliation.execute_statement_posting",
            return_value=posting,
        ):
            response = self.client.post(
                reverse("reconciliation_statement_suggestion_execute", args=[suggestion.id]),
                {"slack_user_id": "UADMIN", "domain": "mlai.au", "confirm": True},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["posting"]["xero_bank_transaction_id"], "bt-1")
        self.assertEqual(response.data["posting"]["id"], 7)


class GranularScopeTests(TestCase):
    """This org connects with Xero's granular scopes (see XERO_OAUTH_SCOPES):
    accounting.invoices must satisfy the bill-creation gate just like the
    classic accounting.transactions scope."""

    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="granular@example.com", slack_id="UGRAN")
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.XERO,
            user=self.user,
            organization=self.organization,
            access_token="access-token",
            external_account_id="tenant-1",
            scopes=[
                "offline_access",
                "accounting.invoices",
                "accounting.banktransactions",
                "accounting.payments",
                "accounting.attachments",
                "accounting.contacts.read",
            ],
        )
        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_connection=self.connection,
            xero_bank_account_id="bank-1",
        )

    def test_granular_invoice_scope_satisfies_bill_preview(self):
        preview = build_reconciliation_bill_preview(self.organization, payload=_bill_payload())
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["errors"], [])

    def test_missing_granular_scope_still_reports_reconnect(self):
        self.connection.scopes = ["accounting.banktransactions", "accounting.payments"]
        self.connection.save(update_fields=["scopes"])
        preview = build_reconciliation_bill_preview(self.organization, payload=_bill_payload())
        self.assertFalse(preview["ready"])
        self.assertTrue(any("accounting.transactions" in item for item in preview["errors"]))


class OperationalScopeCoverageTests(TestCase):
    """Granular write scopes must satisfy their .read requirements so a
    write-capable XERO_OAUTH_SCOPES config can still build the connect URL."""

    def test_write_scopes_cover_read_requirements(self):
        from integrations.services.xero_scopes import xero_missing_operational_scopes

        write_only = (
            "offline_access accounting.invoices accounting.payments "
            "accounting.settings accounting.settings.read accounting.contacts.read "
            "accounting.banktransactions accounting.attachments"
        )
        self.assertEqual(xero_missing_operational_scopes(write_only), ())

    def test_genuinely_missing_scopes_still_reported(self):
        from integrations.services.xero_scopes import xero_missing_operational_scopes

        self.assertEqual(
            xero_missing_operational_scopes("offline_access accounting.invoices"),
            ("accounting.payments.read", "accounting.settings.read", "accounting.contacts.read"),
        )
