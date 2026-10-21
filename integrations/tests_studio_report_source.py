"""Synthetic source refresh tests. No database, provider access or credentials."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from .api_views_studio_reports import StudioReportPrepareView
from .services.studio_report_source import SourceIncomplete, all_invoices, prepare, refresh_finance
from .services.studio_report_source import mailbox_ids

NOW = datetime(2026, 10, 23, 6, 1, tzinfo=timezone.utc)
CUTOFF = "2026-10-23T17:00:00+11:00"


def invoice(identity="i1", number="INV-1", payment="p1", amount=5000, status="PAID"):
    return {"InvoiceID": identity, "InvoiceNumber": number, "Contact": {"ContactID": "contact"},
        "DateString": "2026-10-01", "CurrencyCode": "AUD", "Type": "ACCREC", "Status": status,
        "Total": amount, "AmountPaid": amount if payment else 0,
        "Payments": [{"PaymentID": payment, "Amount": amount, "BatchPaymentID": "batch"}] if payment else []}


def payment(identity="p1", invoice_id="i1", amount=5000):
    return {"PaymentID": identity, "Invoice": {"InvoiceID": invoice_id}, "Amount": amount,
        "Status": "AUTHORISED", "PaymentType": "ACCRECPAYMENT", "Date": "2026-10-14", "IsReconciled": True}


def snapshot():
    return {"team": "T123", "organization": "linear-org", "evidence": {}, "clients": {"UMARK": {
        "project_ids": ["project"], "invoices": [], "payments": [], "contractor_invoices": [],
        "hours": [], "coverage": {"start": "2026-06-01", "through": "2026-10-11"},
        "as_of": "2026-10-11T00:00:00Z"}}}


class FinanceTests(SimpleTestCase):
    def test_legacy_bank_receipt_is_verified_and_reversal_removes_credits(self):
        value = snapshot()
        data = value['clients']['UMARK']
        data['invoices'] = [{'id': 'external', 'number': 'external', 'date': '2026-06-01',
                             'status': 'paid', 'amount_cents': 500000, 'paid_cents': 500000}]
        data['payments'] = [{'id': 'bank', 'bank_transaction_id': 'bank', 'date': '2026-07-02',
            'amount_cents': 500000, 'allocations': [{'invoice_id': 'external', 'amount_cents': 500000}],
            'invoice_ids': ['external'], 'evidence_ids': ['reviewed']}]
        bank = {'BankTransactionID': 'bank', 'Type': 'RECEIVE', 'Status': 'AUTHORISED', 'Total': 5000,
                'Date': '2026-07-02', 'CurrencyCode': 'AUD', 'IsReconciled': True,
                'Contact': {'ContactID': 'contact'}}
        get = Mock(return_value={'BankTransactions': [bank]})
        first = refresh_finance(value, 'UMARK', [], get, CUTOFF, ['external'], 'contact')
        self.assertEqual(first['clients']['UMARK']['payments'][0]['amount_cents'], 500000)
        self.assertEqual(refresh_finance(first, 'UMARK', [], get, CUTOFF, ['external'], 'contact'), first)
        bank['Status'] = 'DELETED'
        reversed_value = refresh_finance(first, 'UMARK', [], get, CUTOFF, ['external'], 'contact')
        self.assertEqual(reversed_value['clients']['UMARK']['payments'], [])
        self.assertEqual(reversed_value['clients']['UMARK']['invoices'][0]['paid_cents'], 0)

    def test_batches_count_cash_once_and_new_payments_replace_old_allocations(self):
        rows = [invoice(), invoice("i2", "INV-2", "p2")]
        get = Mock(side_effect=lambda path: {"Payments": [payment(path.rsplit("/", 1)[1],
            "i2" if path.endswith("p2") else "i1")]})
        first = refresh_finance(snapshot(), "UMARK", rows, get, CUTOFF)
        data = first["clients"]["UMARK"]
        self.assertEqual(len(data["payments"]), 1)
        self.assertEqual(data["payments"][0]["amount_cents"], 1000000)
        second = refresh_finance(first, "UMARK", rows, get, CUTOFF)
        self.assertEqual(second, first)
        self.assertEqual(sum(row["paid_cents"] for row in data["invoices"]), 1000000)

    def test_reversed_payment_is_removed_instead_of_carried_forward(self):
        get = Mock(return_value={"Payments": [payment()]})
        old = refresh_finance(snapshot(), "UMARK", [invoice()], get, CUTOFF)
        revised = invoice(payment=None, status="AUTHORISED")
        updated = refresh_finance(old, "UMARK", [revised], get, CUTOFF)
        self.assertEqual(updated["clients"]["UMARK"]["payments"], [])
        self.assertEqual(updated["clients"]["UMARK"]["invoices"][0]["paid_cents"], 0)

    def test_mismatched_payment_and_credit_note_fail_closed(self):
        get = Mock(return_value={"Payments": [payment(invoice_id="other")]})
        with self.assertRaisesRegex(SourceIncomplete, "payment_invoice_mismatch"):
            refresh_finance(snapshot(), "UMARK", [invoice()], get, CUTOFF)
        row = invoice(payment=None, status="AUTHORISED")
        row["AmountCredited"] = 100
        with self.assertRaisesRegex(SourceIncomplete, "cash_allocation_requires_review"):
            refresh_finance(snapshot(), "UMARK", [row], get, CUTOFF)

    def test_missing_previously_mapped_invoice_is_not_zero(self):
        old = snapshot()
        old["clients"]["UMARK"]["invoices"] = [{"id": "missing", "number": "INV-OLD"}]
        with self.assertRaisesRegex(SourceIncomplete, "previous_invoice_missing"):
            refresh_finance(old, "UMARK", [], Mock(), CUTOFF)

    def test_pagination_and_cross_client_scope(self):
        get = Mock(side_effect=[{"Invoices": [invoice()], "pagination": {"pageCount": 2, "itemCount": 2}},
                                {"Invoices": [invoice("i2", "INV-2")], "pagination": {"pageCount": 2, "itemCount": 2}}])
        self.assertEqual(len(all_invoices(get, "contact")), 2)
        with self.assertRaisesRegex(SourceIncomplete, "invoice_identity_mismatch"):
            all_invoices(Mock(return_value={"Invoices": [invoice()]}), "other")
        with self.assertRaisesRegex(SourceIncomplete, "invoice_pagination_incomplete"):
            all_invoices(Mock(return_value={"Invoices": [], "pagination": {"pageCount": 2}}), "contact")


class PreparationTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "work.json"
        self.manifest = {"snapshot": snapshot(), "hours_complete_through": CUTOFF,
                         "reviewed_at": CUTOFF, "reviewed_gmail_ids": ["reviewed"]}
        self.path.write_text(json.dumps(self.manifest))
        self.scope = {"work_manifest": str(self.path), "linear_organization": "linear-org", "project_ids": ["project"],
                      "gmail_credentials": "/unused", "gmail_query": "scoped", "xero_contact_id": "contact"}
        self.request = {"owner": "UMARK", "team": "T123", "cutoff": CUTOFF, "nonce": "a" * 64}

    def run_prepare(self, **kwargs):
        return prepare(self.scope, self.request, get=Mock(return_value={"Invoices": []}),
                       mail=kwargs.pop("mail", Mock(return_value={"reviewed"})), now=kwargs.pop("now", NOW), **kwargs)

    def test_ready_only_after_all_streams_close(self):
        result = self.run_prepare()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(set(result["checks"]), {"hours", "contractor_invoices", "client_invoices", "payments"})
        self.assertEqual(result["snapshot"]["clients"]["UMARK"]["coverage"]["through"], "2026-10-23")
        self.assertEqual(self.manifest["snapshot"]["clients"]["UMARK"]["as_of"], "2026-10-11T00:00:00Z")

    def test_successful_finance_read_does_not_freshen_missing_hours(self):
        self.manifest["hours_complete_through"] = "2026-10-11T00:00:00Z"
        self.path.write_text(json.dumps(self.manifest))
        result = self.run_prepare()
        self.assertEqual(result["status"], "waiting_data")
        self.assertIn("hours_coverage_missing", result["missing"])
        self.assertNotIn("snapshot", result)

    def test_unknown_invoice_or_mail_failure_waits(self):
        for mail in [Mock(return_value={"reviewed", "new-invoice"}), Mock(side_effect=RuntimeError("secret"))]:
            result = self.run_prepare(mail=mail)
            self.assertEqual(result["status"], "waiting_data")
            self.assertNotIn("snapshot", result)
            self.assertNotIn("secret", json.dumps(result))

    def test_unreviewed_invoice_ids_are_kept_only_in_private_receipt(self):
        self.scope['review_directory'] = str(Path(self.directory.name) / 'review')
        result = self.run_prepare(mail=Mock(return_value={'reviewed', 'new-private-message'}))
        self.assertNotIn('new-private-message', json.dumps(result))
        saved = next(Path(self.scope['review_directory']).glob('*.json'))
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(saved.read_text())['unreviewed_gmail_ids'], ['new-private-message'])

    def test_warmup_cannot_pass_closing_barrier(self):
        self.manifest.update(hours_complete_through="2026-10-23T16:30:00+11:00", reviewed_at="2026-10-23T16:30:00+11:00")
        self.path.write_text(json.dumps(self.manifest))
        result = self.run_prepare(now=datetime(2026, 10, 23, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "waiting_data")
        self.assertIn("closing_refresh_required", result["missing"])


class EndpointTests(SimpleTestCase):
    def test_authenticated_wrong_owner_rejected_before_provider_reads(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'clients.json'
            config.write_text(json.dumps({'UMARK': {'team': 'T123'}}))
            with (override_settings(STUDIO_REPORT_SOURCE_ENABLED=True, STUDIO_REPORT_SOURCE_KEY='k' * 32,
                                    STUDIO_REPORT_SOURCE_CONFIG=str(config)),
                  patch('integrations.api_views_studio_reports.prepare') as collector):
                response = StudioReportPrepareView.as_view()(APIRequestFactory().post('/', {
                    'owner': 'UOTHER', 'team': 'T123', 'cutoff': CUTOFF, 'nonce': 'a' * 64}, format='json',
                    HTTP_X_STUDIO_SOURCE_KEY='k' * 32))
                self.assertEqual(response.status_code, 400)
                collector.assert_not_called()

    def test_authenticated_export_uses_fixed_connection_and_signed_binding(self):
        import hashlib
        import hmac
        result = {'owner': 'UMARK', 'cutoff': CUTOFF, 'nonce': 'a' * 64, 'status': 'waiting_data'}
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'clients.json'
            config.write_text(json.dumps({'UMARK': {'team': 'T123', 'xero_connection_id': 2, 'organization_id': 7}}))
            with (override_settings(STUDIO_REPORT_SOURCE_ENABLED=True, STUDIO_REPORT_SOURCE_KEY='k' * 32,
                                    STUDIO_REPORT_SOURCE_CONFIG=str(config)),
                  patch('integrations.api_views_studio_reports.prepare', return_value=result),
                  patch('django.db.transaction.atomic'),
                  patch('integrations.models.ExternalServiceConnection.objects') as manager):
                response = StudioReportPrepareView.as_view()(APIRequestFactory().post('/', {
                    'owner': 'UMARK', 'team': 'T123', 'cutoff': CUTOFF, 'nonce': 'a' * 64}, format='json',
                    HTTP_X_STUDIO_SOURCE_KEY='k' * 32))
                self.assertEqual(response.status_code, 200)
                manager.select_for_update.return_value.get.assert_called_once_with(id=2, organization_id=7, provider='xero')
                signature = hmac.new(('k' * 32).encode(), json.dumps(result, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
                self.assertEqual(response.data['signature'], signature)

    @override_settings(STUDIO_REPORT_SOURCE_ENABLED=False, STUDIO_REPORT_SOURCE_KEY="k" * 32)
    def test_disabled_endpoint_rejects_correct_key(self):
        response = StudioReportPrepareView.as_view()(APIRequestFactory().post("/", {}, format="json",
                                                     HTTP_X_STUDIO_SOURCE_KEY="k" * 32))
        self.assertEqual(response.status_code, 403)

    @override_settings(STUDIO_REPORT_SOURCE_ENABLED=True, STUDIO_REPORT_SOURCE_KEY="k" * 32, ROO_API_KEY="k" * 32)
    def test_general_roo_key_cannot_be_reused_for_finance_export(self):
        response = StudioReportPrepareView.as_view()(APIRequestFactory().post("/", {}, format="json",
                                                     HTTP_X_STUDIO_SOURCE_KEY="k" * 32))
        self.assertEqual(response.status_code, 403)

    @override_settings(STUDIO_REPORT_SOURCE_ENABLED=True, STUDIO_REPORT_SOURCE_KEY="k" * 32)
    def test_wrong_key_is_denied_before_configuration_or_provider_reads(self):
        response = StudioReportPrepareView.as_view()(APIRequestFactory().post("/", {}, format="json",
                                                     HTTP_X_STUDIO_SOURCE_KEY="wrong"))
        self.assertEqual(response.status_code, 403)


class TokenOwnershipTests(SimpleTestCase):
    def test_refresh_failure_commits_reauthorisation_status_before_raising(self):
        from .services.external_connectors import ConnectorOAuthError, _refresh_xero_connection
        from django.utils import timezone as django_timezone
        locked = Mock(access_token='old', token_expires_at=django_timezone.now() - timedelta(hours=1))
        with (patch('integrations.services.external_connectors.transaction.atomic') as atomic,
              patch('integrations.services.external_connectors.ExternalServiceConnection.objects') as manager,
              patch('integrations.services.external_connectors._refresh_xero_connection_unlocked',
                    side_effect=ConnectorOAuthError('synthetic-reconnect'))):
            manager.select_for_update.return_value.get.return_value = locked
            with self.assertRaises(ConnectorOAuthError):
                _refresh_xero_connection(Mock(pk=1))
            atomic.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_concurrent_refresh_rechecks_locked_credentials(self):
        from .services.external_connectors import _refresh_xero_connection
        from django.utils import timezone as django_timezone
        stale = Mock(pk=1)
        locked = Mock(access_token='synthetic-fresh', token_expires_at=django_timezone.now() + timedelta(hours=1))
        with (patch('integrations.services.external_connectors.transaction.atomic'),
              patch('integrations.services.external_connectors.ExternalServiceConnection.objects') as manager,
              patch('integrations.services.external_connectors._refresh_xero_connection_unlocked') as rotate):
            manager.select_for_update.return_value.get.return_value = locked
            self.assertEqual(_refresh_xero_connection(stale), 'synthetic-fresh')
            rotate.assert_not_called()
            manager.select_for_update.assert_called_once()
            stale.refresh_from_db.assert_called_once()


class MailboxTests(SimpleTestCase):
    def test_full_mail_query_is_paged_and_identity_checked(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'gmail.json'
            path.write_text(json.dumps({'email': 'treasury@example.invalid', 'client_id': 'synthetic',
                'client_secret': 'synthetic', 'refresh_token': 'synthetic'}))
            service = Mock()
            users = service.users.return_value
            users.getProfile.return_value.execute.return_value = {'emailAddress': 'treasury@example.invalid'}
            messages = users.messages.return_value
            messages.list.return_value.execute.side_effect = [
                {'messages': [{'id': 'one'}], 'nextPageToken': 'page2'}, {'messages': [{'id': 'two'}]}]
            with patch('googleapiclient.discovery.build', return_value=service):
                self.assertEqual(mailbox_ids(str(path), 'scoped-query'), {'one', 'two'})
                self.assertEqual(messages.list.call_count, 2)
                self.assertEqual(messages.list.call_args.kwargs['pageToken'], 'page2')
                self.assertTrue(messages.list.call_args.kwargs['includeSpamTrash'])
                users.getProfile.return_value.execute.return_value = {'emailAddress': 'other@example.invalid'}
                with self.assertRaisesRegex(SourceIncomplete, 'mailbox_identity_mismatch'):
                    mailbox_ids(str(path), 'scoped-query')

