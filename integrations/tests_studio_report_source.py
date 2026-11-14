"""Synthetic source refresh tests. No database, provider access or credentials."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

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
    def test_expense_receipts_are_cash_without_hour_credits(self):
        rows = [invoice(), invoice('i2', 'EXPENSE', 'p2', amount=125)]
        rows[0]['LineItems'] = [{'Description': 'Studio subscription November', 'LineAmount': 5000}]
        rows[1]['LineItems'] = [{'Description': 'Software reimbursement', 'LineAmount': 125}]
        get = Mock(side_effect=lambda path: {'Payments': [payment(path.rsplit('/', 1)[1],
            'i2' if path.endswith('p2') else 'i1', 125 if path.endswith('p2') else 5000)]})
        result = refresh_finance(snapshot(), 'UMARK', rows, get, CUTOFF,
                                 credit_description_prefix='Studio subscription')
        data = result['clients']['UMARK']
        self.assertEqual(data['payments'][0]['amount_cents'], 512500)
        self.assertEqual(data['payments'][0]['credit_amount_cents'], 500000)
        self.assertFalse(data['invoices'][1]['credit_eligible'])
        rows[0]['LineItems'].append({'Description': 'Mixed expense', 'LineAmount': 10})
        result = refresh_finance(snapshot(), 'UMARK', rows, get, CUTOFF,
                                 credit_description_prefix='Studio subscription')
        self.assertEqual(result['clients']['UMARK']['payments'][0]['credit_amount_cents'], 0)

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
    def test_latest_finance_keeps_work_dates_and_unknown_mail_visible(self):
        self.manifest['hours_complete_through'] = '2026-10-11T00:00:00Z'
        self.path.write_text(json.dumps(self.manifest))
        self.request['mode'] = 'latest'
        result = self.run_prepare(mail=Mock(return_value={'reviewed', 'new'}))
        self.assertEqual(result['status'], 'partial')
        data = result['snapshot']['clients']['UMARK']
        self.assertEqual(data['source_freshness']['hours_complete_through'], '2026-10-11T00:00:00+00:00')
        self.assertEqual(data['source_freshness']['unreviewed_invoice_count'], 1)
        self.assertEqual(data['as_of'], NOW.isoformat())
        self.assertEqual(json.loads(self.path.read_text()), self.manifest)

    def test_latest_cannot_hide_finance_or_mail_failure_or_future_cutoff(self):
        self.request['mode'] = 'latest'
        cases = [dict(get=Mock(side_effect=RuntimeError('private')), mail=Mock(return_value={'reviewed'}), now=NOW),
                 dict(get=Mock(return_value={'Invoices': []}), mail=Mock(side_effect=RuntimeError('private')), now=NOW)]
        for args in cases:
            result = prepare(self.scope, self.request, **args)
            self.assertEqual(result['status'], 'waiting_data')
            self.assertNotIn('snapshot', result)
            self.assertNotIn('private', json.dumps(result))
        self.request['cutoff'] = (NOW + timedelta(hours=1)).isoformat()
        result = self.run_prepare()
        self.assertEqual(result['status'], 'waiting_data')
        self.assertNotIn('snapshot', result)

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

    def test_rate_limit_preserves_retry_after_and_blocks_other_cutoffs_until_reset(self):
        self.scope['review_directory'] = str(Path(self.directory.name) / 'review')
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '21600'
        get = Mock(side_effect=requests.HTTPError('private provider detail', response=response))
        with patch('integrations.services.studio_report_source.time.monotonic', side_effect=[100, 220]):
            result = prepare(self.scope, self.request, get=get, mail=Mock(return_value={'reviewed'}), now=NOW)
        self.assertEqual(result['status'], 'waiting_data')
        self.assertIn('finance_rate_limited', result['missing'])
        self.assertEqual(result['retry_at'], (NOW + timedelta(hours=6, minutes=2)).isoformat())
        self.assertNotIn('snapshot', result)
        self.assertNotIn('private provider detail', json.dumps(result))
        get.reset_mock()
        later = prepare(self.scope, {**self.request, 'cutoff': NOW.isoformat()}, get=get,
                        mail=Mock(return_value={'reviewed'}), now=NOW + timedelta(minutes=6))
        get.assert_not_called()
        self.assertEqual(later['retry_at'], result['retry_at'])
        self.assertIn('finance_rate_limited', later['missing'])
        # A subsequent process can read again after the persisted deadline.
        get.side_effect = None
        get.return_value = {'Invoices': []}
        recovered = prepare(self.scope, self.request, get=get, mail=Mock(return_value={'reviewed'}),
                            now=NOW + timedelta(hours=6, minutes=2, seconds=1))
        self.assertEqual(recovered['status'], 'ready')
        self.assertNotIn('retry_at', recovered)
        for path in Path(self.scope['review_directory']).glob('finance-cooldown-*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rate_limit_http_date_and_invalid_headers_have_safe_backoff(self):
        from .services.studio_report_source import finance_retry_at
        response = requests.Response()
        response.status_code = 429
        error = requests.HTTPError(response=response)
        response.headers['Retry-After'] = 'Fri, 23 Oct 2026 12:01:00 GMT'
        self.assertEqual(finance_retry_at(error, NOW), NOW + timedelta(hours=6))
        for value in ['', 'not-a-date', '-10', '99999999999999999999999999999']:
            response.headers['Retry-After'] = value
            self.assertGreaterEqual(finance_retry_at(error, NOW), NOW + timedelta(minutes=5))
        response.status_code = 401
        self.assertIsNone(finance_retry_at(error, NOW))


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
              patch('integrations.services.external_connectors.get_user_model') as users,
              patch('integrations.services.external_connectors.ExternalServiceConnection.objects') as manager,
              patch('integrations.services.external_connectors._refresh_xero_connection_unlocked',
                    side_effect=ConnectorOAuthError('synthetic-reconnect'))):
            manager.select_for_update.return_value.get.return_value = locked
            with self.assertRaises(ConnectorOAuthError):
                _refresh_xero_connection(Mock(pk=1, user_id=7))
            atomic.return_value.__exit__.assert_called_once_with(None, None, None)
            users.return_value.objects.select_for_update.return_value.get.assert_called_once_with(pk=7)

    def test_concurrent_refresh_rechecks_locked_credentials(self):
        from .services.external_connectors import _refresh_xero_connection
        from django.utils import timezone as django_timezone
        stale = Mock(pk=1, user_id=7)
        locked = Mock(access_token='synthetic-fresh', token_expires_at=django_timezone.now() + timedelta(hours=1))
        with (patch('integrations.services.external_connectors.transaction.atomic'),
              patch('integrations.services.external_connectors.get_user_model') as users,
              patch('integrations.services.external_connectors.ExternalServiceConnection.objects') as manager,
              patch('integrations.services.external_connectors._refresh_xero_connection_unlocked') as rotate):
            manager.select_for_update.return_value.get.return_value = locked
            self.assertEqual(_refresh_xero_connection(stale), 'synthetic-fresh')
            rotate.assert_not_called()
            manager.select_for_update.assert_called_once()
            stale.refresh_from_db.assert_called_once()
            users.return_value.objects.select_for_update.return_value.get.assert_called_once_with(pk=7)


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



class SubmittedWorkTests(SimpleTestCase):
    def setUp(self):
        self.scope = {'project_ids':['project'], 'submissions':{
            'projects':{'project':'Example'}, 'since':CUTOFF,
            'builders':{'builder':{'supplier':'Person', 'senders':['person@example.org']}}}}
        self.issue = {'identifier':'TEST-17','project':{'id':'project'},'assignee':{'id':'builder'},
            'state':{'type':'completed'},'updatedAt':CUTOFF,'completedAt':CUTOFF,
            'description':'## Invoice 17 — work\n**Work date:** 19–20 October 2026\n**Logged time:** 90 minutes'}
        self.invoice = ('INVOICE # 17\nBill To: Date: Oct 21, 2026\n[Studio] Example - completed work\n'
                        'TEST-17 - Updated form 1.5 $30.00 $45.00\n19-20 October 2026\nTotal hours: 1h 30m')

    def run_collection(self, texts, data=None):
        from .services.studio_submissions import collect
        data = data or snapshot()
        service = Mock()
        service.users.return_value.messages.return_value.get.side_effect = lambda **kw: Mock(
            execute=lambda: {'id':kw['id'],'internalDate':'1000'})
        with patch('integrations.services.studio_submissions.linear_issues',return_value=[self.issue]), \
             patch('integrations.services.studio_submissions.sender_of',return_value='person@example.org'), \
             patch('integrations.services.studio_submissions.pdf_attachment',side_effect=lambda _,m:texts[m['id']]):
            result = collect(self.scope,data,'UMARK',set(texts),NOW,service)
        return data,result

    def test_linear_then_duplicated_invoice_then_review(self):
        data,_=self.run_collection({})
        self.assertEqual(data['clients']['UMARK']['hours'][0]['submission_source'],'linear')
        data,(handled,errors)=self.run_collection({'a':self.invoice,'b':self.invoice})
        rows=data['clients']['UMARK']['hours']
        self.assertEqual((len(rows),rows[0]['hour_units'],rows[0]['submission_source']),(1,1500000,'invoice'))
        self.assertEqual((handled,errors),({'a','b'},0))
        self.run_collection({'a':self.invoice},data)
        self.assertEqual(len(rows),1)

    def test_conflict_is_not_added(self):
        data,(_,errors)=self.run_collection({'a':self.invoice.replace('1.5 $','2 $').replace('1h 30m','2h')})
        self.assertEqual((data['clients']['UMARK']['hours'],errors),([],1))

    def test_unnumbered_ticket_must_match_invoice_hours_and_dates(self):
        self.issue['description'] = self.issue['description'].replace('## Invoice 17 — work\n', '')
        for invoice in (
            self.invoice.replace('1.5 $', '2 $').replace('1h 30m', '2h'),
            self.invoice.replace('19-20 October', '18-20 October'),
        ):
            with self.subTest(invoice=invoice):
                data, (handled, errors) = self.run_collection({'a': invoice})
                self.assertEqual(data['clients']['UMARK']['hours'], [])
                self.assertEqual(handled, set())
                self.assertGreater(errors, 0)
        data, (handled, errors) = self.run_collection({'a': self.invoice})
        self.assertEqual((len(data['clients']['UMARK']['hours']), handled, errors), (1, {'a'}, 0))

    def test_aggregate_invoice_must_reconcile_unnumbered_work(self):
        self.issue['description'] = self.issue['description'].replace('## Invoice 17 — work\n', '')
        template = 'no. web12 – 21st October 2026\nMLAI Studio - Example $120\n{} hours @ $60/hour (19–20 Oct)'
        data, (handled, errors) = self.run_collection({'a': template.format('2')})
        self.assertEqual(data['clients']['UMARK']['hours'], [])
        self.assertEqual(handled, set())
        self.assertGreater(errors, 0)
        data, (handled, errors) = self.run_collection({'a': template.format('1.5')})
        self.assertEqual((len(data['clients']['UMARK']['hours']), handled, errors), (1, {'a'}, 0))

    def test_aggregate_invoice_layout_and_scope(self):
        from .services.studio_submissions import invoice_rows
        text='no. web12 – 6th November 2026\nMLAI Studio - Example $120\n2 hours @ $60/hour (30 Oct – 6 Nov)'
        self.assertEqual(invoice_rows(text,{'p':'Example'})[2],[('p',None,'2026-10-30','2026-11-06',2000000)])
        with self.assertRaises(ValueError):
            invoice_rows(text+'\nMLAI Studio - Elsewhere $50',{'p':'Example'})

    def test_strict_backlog_skips_xero_and_latest_shares_cache(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'work.json'
            path.write_text(json.dumps({'snapshot':snapshot(),'reviewed_at':CUTOFF,'hours_complete_through':CUTOFF,
                                        'reviewed_gmail_ids':[]}))
            self.scope.update(work_manifest=str(path),linear_organization='linear-org',gmail_credentials='unused',
                              gmail_query='test',xero_contact_id='contact',review_directory=d)
            req={'owner':'UMARK','team':'T123','cutoff':NOW.isoformat(),'nonce':'a'*64}
            get=Mock(return_value={'Invoices':[]})
            mail=Mock(return_value=set())
            result=prepare(self.scope,req,get=get,mail=mail,now=NOW)
            self.assertEqual(result['missing'],['hours_coverage_missing'])
            get.assert_not_called()
            req['mode']='latest'
            with patch('integrations.services.studio_submissions.linear_issues',return_value=[]):
                prepare(self.scope,req,get=get,mail=mail,now=NOW)
                get.reset_mock()
                later=NOW+timedelta(minutes=2)
                result=prepare(self.scope,{**req,'cutoff':later.isoformat()},get=get,mail=mail,now=later)
                get.assert_not_called()
                self.assertEqual(result['checks']['payments']['checked_at'],NOW.isoformat())
                response=requests.Response()
                response.status_code=429
                get.side_effect=requests.HTTPError(response=response)
                later+=timedelta(minutes=20)
                result=prepare(self.scope,{**req,'cutoff':later.isoformat()},get=get,mail=mail,now=later)
                self.assertIn('finance_rate_limited',result['missing'])
                self.assertEqual(result['snapshot']['clients']['UMARK']['source_freshness']['finance_checked_at'],NOW.isoformat())
