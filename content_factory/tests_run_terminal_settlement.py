"""Actual original-debit proof for the terminal continuation fence."""
from copy import deepcopy
from unittest.mock import patch
from django.test import TransactionTestCase, override_settings
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import run_terminal_settlement as terminal
from .run_ownership import (RECONCILIATION_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, owner_receipt, wire_identity)
from . import tests_run_dispatch_settlement as fixtures
from .tests_run_event_authority import FLAGS
from .website_contract import evidence_digest
from .founder_journey_testing import ALL_P1_FLAGS_ON


@override_settings(**FLAGS)
class TerminalSettlementFenceTests(TransactionTestCase):
    setUp = fixtures.DispatchSettlementTests.setUp
    paid = fixtures.DispatchSettlementTests.paid

    def closed(self, run):
        body = {'schema_version': 1, 'run_id': run.run_id, 'status': 'closed_no_effects',
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run),
            'event_cursor': 3, 'closed_at': self.now.isoformat()}
        body['receipt_sha256'] = terminal.receipt_digest(body)
        return body

    def retain(self, run, spend, effect):
        closed = self.closed(run)
        state = {'schema_version': 1, 'state': 'refunded', 'run_id': run.run_id,
            'admission_digest': evidence_digest(wire_identity(owner_receipt(run))),
            'execution_id': current_execution_ticket(run)['execution_id'], 'closure_sha256': closed['receipt_sha256'],
            'original_ledger_id': spend.pk, 'effect_id': effect.pk, 'at': self.now.isoformat()}
        state['settlement_sha256'] = terminal.receipt_digest(state, field='settlement_sha256')
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, RECONCILIATION_RESULT_KEY: {
            terminal.TERMINAL_CLOSURES_KEY: {state['execution_id']: closed}, terminal.TERMINAL_SETTLEMENT_KEY: state}})
        run.refresh_from_db()
        return state

    def test_absent_settlement_never_closes_current_original_obligation(self):
        run, _, _ = self.paid()
        self.assertFalse(terminal.terminal_execution_closed(run))

    def test_actual_same_original_full_refund_and_exact_current_receipt_close_execution_flags_off(self):
        run, _, spend = self.paid()
        effect, _ = PointsService.refund(user=spend.user, delta=12, source='CONTENT_FACTORY',
            description='Synthetic retained original refund', idempotency_key='terminal-fixture-refund', created_by_slack_id='system', original_spend_key=spend.idempotency_key)
        self.retain(run, spend, effect)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            self.assertTrue(terminal.terminal_execution_closed(run))
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_no_actual_refund_cannot_be_fabricated_by_private_effect_id(self):
        run, _, spend = self.paid()
        self.retain(run, spend, spend)
        with self.assertRaises(terminal.TerminalSettlementPending):
            terminal.terminal_execution_closed(run)

    def test_corrupted_settlement_or_changed_current_ticket_refuses_instead_of_reopening(self):
        run, _, spend = self.paid()
        effect, _ = PointsService.refund(user=spend.user, delta=12, source='CONTENT_FACTORY',
            description='Synthetic retained original refund', idempotency_key='terminal-fixture-refund', created_by_slack_id='system', original_spend_key=spend.idempotency_key)
        self.retain(run, spend, effect)
        saved = deepcopy(run.result)
        for mutate in [lambda raw: raw[RECONCILIATION_RESULT_KEY][terminal.TERMINAL_SETTLEMENT_KEY].update(execution_id='different'),
                lambda raw: raw[RECONCILIATION_RESULT_KEY][terminal.TERMINAL_CLOSURES_KEY].clear()]:
            raw = deepcopy(saved); mutate(raw)
            run.result = raw
            with self.assertRaises(terminal.TerminalSettlementPending):
                terminal.terminal_execution_closed(run)

    def test_closed_receipt_needs_exact_typed_scope_cursor_and_digest(self):
        run, _, _ = self.paid()
        self.assertEqual(terminal.validate_terminal_receipt(run, self.closed(run)), self.closed(run))
        for field, value in [('event_cursor', True), ('event_cursor', 0), ('run_id', 'another'),
                ('status', 'failed'), ('receipt_sha256', 'f' * 64), ('closed_at', '2026-11-03')]:
            raw = self.closed(run); raw[field] = value
            with self.subTest(field=field), self.assertRaises(terminal.TerminalSettlementPending):
                terminal.validate_terminal_receipt(run, raw)


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, CONTENT_FACTORY_URL='https://terminal.fixture.invalid',
                   CONTENT_FACTORY_API_KEY='synthetic-terminal-key', ARTICLES_RUN_SDK_CONTROL_ENABLED=False)
class ActualPairedTerminalSettlementTests(TransactionTestCase):
    def sequence(self, model, value):
        from django.db import connection
        table = model._meta.db_table
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute('SELECT setval(pg_get_serial_sequence(%s, %s), %s, false)', [table, 'id', value])
            elif connection.vendor == 'sqlite':
                cursor.execute('UPDATE sqlite_sequence SET seq=%s WHERE name=%s', [value - 1, table])
                if not cursor.rowcount:
                    cursor.execute('INSERT INTO sqlite_sequence(name, seq) VALUES (%s, %s)', [table, value - 1])
            else:
                self.fail('Fixture requires a supported disposable PostgreSQL/SQLite sequence constructor.')

    def restore(self, kind, *, ingest=True):
        import json
        from pathlib import Path
        from django.contrib.auth import get_user_model
        from organizations.models import Organization
        from roo.models import Ledger
        from .self_heal_testing import make_p2_org
        from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
        from .run_backend_facts import create_job_facts
        from integrations.services.article_generation import _ledger_reference_id
        value = json.loads((Path(__file__).parent / 'testdata/r3' / ('terminal_actual_' + kind + '.json')).read_text())
        request = value['original_backend_fixture']['run_request']
        admission = request[WIRE_REQUEST_KEY]
        self.sequence(Organization, int(admission['organization_id']))
        self.sequence(get_user_model(), int(admission['principal']['user_id']))
        fixture = make_p2_org(admission['domain'])
        self.org, self.payer, self.fixture = fixture.organization, fixture.owner, fixture
        self.assertEqual((str(fixture.organization.pk), str(fixture.owner.pk)),
                         (admission['organization_id'], admission['principal']['user_id']))
        raw = {'domain': fixture.organization.domain, 'client_request_id': admission['dispatch_key'],
            'topic': request['topic'], 'delivery_mode': 'content_only', 'delivery_mode_confirmed': True,
            'expected_cost_points': request['expected_cost_points']}
        if kind == 'free':
            from .first_article_entitlement import reserve_entitlement, entitled_authorization
            self.sequence(Ledger, int(request['roo_points_ledger_id']))
            grant = reserve_entitlement(organization=fixture.organization, user=fixture.owner,
                client_request_id=admission['dispatch_key'], data=raw, config=fixture.config)
            self.assertIsNotNone(grant)
            self.assertEqual(str(grant.pk), request['roo_points_ledger_id'])
            raw.update(entitled_authorization(domain=fixture.organization.domain, action='article_generation', ledger=grant))
            original = grant
        else:
            self.sequence(Ledger, int(request['roo_points_ledger_id']) - 1)
            PointsService.credit_purchased_topup(user=fixture.owner, delta=100,
                description='Actual paired synthetic funding', idempotency_key='actual-paid-terminal-funding')
            original, _ = PointsService.spend(user=fixture.owner, delta=12, source='CONTENT_FACTORY',
                description='Actual paired original article charge', created_by_slack_id=admission['principal']['actor_id'],
                idempotency_key='content_factory:charge:' + admission['dispatch_key'],
                reference_type='CONTENT_FACTORY', reference_id=_ledger_reference_id(admission['dispatch_key']))
            raw.update(free_domain=False, roo_points_action='article_generation', roo_points_ledger_id=str(original.pk),
                roo_points_cost=12, roo_points_billing_user_id=fixture.owner.pk, roo_points_billing_status='charged', roo_points_authorized=True)
        run = bind_admitted_run(reserve_admitted_run(organization=fixture.organization,
            principal=user_principal(fixture.owner.pk, admission['principal']['actor_id']), workflow=admission['workflow'], payload=raw), value['original_backend_fixture']['run_id'])
        # These assertions prove constructor-derived identity, rather than
        # substituting this database's ids into the producer's wire/digests.
        self.assertEqual(run.run_request, request)
        self.assertEqual(wire_identity(owner_receipt(run)), admission)
        self.assertEqual(current_execution_ticket(run), request[WIRE_EXECUTION_KEY])
        create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id=run.slack_user_id, status='error',
            client_request_id=admission['dispatch_key'], request_meta=run.run_request,
            billing_status='charged' if kind == 'paid' else 'entitled', billing_ledger_id=original.pk,
            billing_amount=12 if kind == 'paid' else 0)
        from .run_event_authority import receive_authoritative_run_events
        for raw in value['events_response']['batches'] if ingest else []:
            reply = receive_authoritative_run_events(raw)
            self.assertEqual(reply.status_code, 200, reply.data)
        run.refresh_from_db()
        return value, run, original

    def response(self, body, status=200):
        import requests
        import json
        response = requests.Response(); response.status_code = status
        response._content = json.dumps(body).encode(); response._content_consumed = True
        return response

    def exercise(self, kind):
        from unittest.mock import patch
        from django.db import connection
        from django.utils import timezone
        from roo.models import Ledger
        from .run_reconciliation import reconcile_authoritative_run
        value, run, original = self.restore(kind)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        money_before = Ledger.objects.count()
        def post(url, *, json, **kwargs):
            self.assertFalse(connection.in_atomic_block)

            self.assertEqual(json, value['cancel_request'])
            return self.response(value['cancel_response'])
        def get(url, *, params, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(url.endswith('/' + run.run_id + '/events'))
            self.assertEqual(params, {'after_seq': 0, 'limit': 100})
            return self.response(value['events_response'])
        with patch.object(terminal.requests, 'post', side_effect=post) as cancel, patch.object(terminal.requests, 'get', side_effect=get) as history:
            receipt = terminal.settle_authoritative_terminal(run.run_id)
            self.assertEqual(receipt['state'], 'refunded' if kind == 'paid' else 'released', receipt)
            self.assertEqual((cancel.call_count, history.call_count), (1, 1))
            replay = terminal.settle_authoritative_terminal(run.run_id)
            self.assertEqual(replay, receipt)
            self.assertEqual((cancel.call_count, history.call_count), (1, 1))
        run.refresh_from_db()
        for field in ('run_request', 'status', 'current_step', 'approval_state', 'created_at', 'updated_at', 'last_event_emitted_at', 'reconciled_at'):
            self.assertEqual(getattr(run, field), before[field], field)
        self.assertTrue(terminal.terminal_execution_closed(run))
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY][terminal.TERMINAL_CLOSURES_KEY][receipt['execution_id']],
                         value['cancel_response']['terminal_settlement_v1'])
        if kind == 'paid':
            self.assertEqual(Ledger.objects.filter(refund_of=original).count(), 1)
            effect = Ledger.objects.get(refund_of=original)
            self.assertEqual((effect.user_id, effect.delta_microroo), (original.user_id, -original.delta_microroo))
        else:
            from .run_dispatch_settlement import _free_obligation_state
            self.assertEqual(_free_obligation_state(run), 'released')
            self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertEqual(Ledger.objects.count(), money_before + 1)

    def test_actual_free_pg_admission_and_actual_cf_closed_journal_release_original_grant(self):
        self.exercise('free')

    def test_actual_paid_pg_admission_and_actual_cf_closed_journal_refund_original_spend(self):
        self.exercise('paid')

    def synthetic(self, status='failed', code=None, kind='paid'):
        from django.utils import timezone
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        value, run, original = self.restore(kind, ingest=False)
        rows = [{'status': 'running'}, {'type': 'step_started', 'step': 'write', 'attempt': 1},
                {'type': 'step_done', 'step': 'write', 'attempt': 1, 'status': 'failed'}]
        if code:
            rows.append({'type': 'failure', 'failure': {'code': code, 'stage': 'write'}})
        rows.append({'status': status})
        envelope = batch(run, rows)
        reply = receive_authoritative_run_events(envelope)
        self.assertEqual(reply.status_code, 200, reply.data)
        run.refresh_from_db()
        closed = {'schema_version': 1, 'run_id': run.run_id, 'status': 'closed_no_effects',
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run),
            'event_cursor': len(rows), 'closed_at': timezone.now().isoformat()}
        closed['receipt_sha256'] = terminal.receipt_digest(closed)
        page = {'schema_version': 1, 'run_id': run.run_id, 'batches': [envelope],
            'next_cursor': len(rows), 'has_more': False}
        return run, original, closed, page

    def transports(self, closed, page):
        from contextlib import ExitStack
        from unittest.mock import patch
        stack = ExitStack()
        post = stack.enter_context(patch.object(terminal.requests, 'post', return_value=self.response({'terminal_settlement_v1': closed})))
        get = stack.enter_context(patch.object(terminal.requests, 'get', return_value=self.response(page)))
        return stack, post, get

    def test_denied_execution_uses_closed_proof_and_keeps_original_typed_denied_history(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic('denied')
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'refunded', result)
        run.refresh_from_db()
        self.assertEqual((run.status, run.approval_state, run.updated_at),
            (before['status'], before['approval_state'], before['updated_at']))
        self.assertEqual(Ledger.objects.filter(refund_of=original).count(), 1)

    def test_blocked_waits_for_original_charge_clock_and_reads_do_not_reset_it(self):
        from datetime import timedelta
        from django.utils import timezone
        from roo.models import Ledger
        run, original, closed, page = self.synthetic('blocked')
        now = timezone.now()
        stack, post, get = self.transports(closed, page)
        with stack:
            held = terminal.settle_authoritative_terminal(run.run_id, now=now)
            self.assertEqual(held, {'state': 'pending', 'reason': 'terminal_not_due'})
            post.assert_not_called(); get.assert_not_called()
            Ledger.objects.filter(pk=original.pk).update(created_at=now - timedelta(hours=49))
            result = terminal.settle_authoritative_terminal(run.run_id, now=now + timedelta(minutes=3))
        self.assertEqual(result['state'], 'refunded', result)
        run.refresh_from_db()
        self.assertEqual(run.created_at + timedelta(hours=48) > now, True)
        self.assertEqual(Ledger.objects.filter(refund_of=original).count(), 1)

    def test_typed_budget_exhaustion_can_close_blocked_before_wall_deadline(self):
        run, original, closed, page = self.synthetic('blocked', 'RUN_BUDGET_EXHAUSTED')
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'refunded', result)

    def test_more_history_or_a_reserved_sequence_gap_holds_money_without_compacting(self):
        from datetime import timedelta
        from django.utils import timezone
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        page['has_more'] = True
        stack, post, get = self.transports(closed, page)
        with stack:
            held = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(held['state'], 'pending', held)
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())
        self.assertIn(held['reason'], {'terminal_history_not_drained', 'terminal_proof_pending'})
        # The exact retained close receipt is reused, without another cancel.
        page['has_more'] = False
        page['batches'] = []
        with terminal.transaction.atomic():
            fresh = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            meta = terminal._meta(fresh); meta['has_more'] = False
            terminal._save_meta(fresh, meta)
        with patch.object(terminal.requests, 'get', return_value=self.response(page)), \
                patch.object(terminal.requests, 'post') as again:
            result = terminal.settle_authoritative_terminal(run.run_id, now=timezone.now() + timedelta(minutes=3))
        self.assertEqual(result['state'], 'refunded', result)
        again.assert_not_called()

    def test_reserved_global_sequence_gap_cannot_become_a_terminal_settlement(self):
        from roo.models import Ledger
        from .run_ownership import EVENT_RESULT_KEY
        run, original, closed, page = self.synthetic()
        # A recoverable transport page can contain a reserved sequence gap;
        # accepting that page must not invent missing financial evidence.
        for envelope in page['batches']:
            envelope['events'] = [row for row in envelope['events'] if row['seq'] != 2]
        saved = deepcopy(run.result)
        ledger = saved[EVENT_RESULT_KEY]
        ledger['events'] = {key: item for key, item in ledger['events'].items() if item['event']['seq'] != 2}
        ContentFactoryRun.objects.filter(pk=run.pk).update(result=saved)
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result, {'state': 'pending', 'reason': 'terminal_journal_gap'})
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())

    def test_wrong_ticket_closure_and_unknown_publication_never_reach_a_refund(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        closed[WIRE_EXECUTION_KEY]['execution_id'] = 'foreign-ticket'
        closed['receipt_sha256'] = terminal.receipt_digest(closed)
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'pending', result)
        get.assert_not_called()
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())

    def test_unknown_publication_intent_holds_without_even_issuing_close_http(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result,
            'publication_evidence_v1': {'version': 1, 'purpose': 'article', 'state': 'unknown'}})
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'pending', result)
        post.assert_not_called(); get.assert_not_called()
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())

    def test_actual_scheduler_owns_independent_terminal_slice_before_legacy(self):
        from core.management.commands import run_scheduled_discovery as command
        from io import StringIO
        from unittest.mock import patch
        with patch.object(command, 'run_runners', return_value=({}, [])) as run:
            command.Command(stdout=StringIO()).handle()
        rows = run.call_args.args[0]
        names = [name for name, _ in rows]
        self.assertLess(names.index('authoritative_run_reconciliation'), names.index('authoritative_terminal_settlement'))
        self.assertLess(names.index('authoritative_terminal_settlement'), names.index('content_factory_reconciliation'))
        self.assertIs(dict(rows)['authoritative_terminal_settlement'], terminal.run_terminal_settlement_sweep)

    def child_history(self, source, original, *, status='failed'):
        from django.utils import timezone
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        self.now = timezone.now()
        child = fixtures.DispatchSettlementTests.child(self, source, original)
        rows = [{'status': 'running'}, {'type': 'step_started', 'step': 'write', 'attempt': 1}]
        if status != 'running':
            rows += [{'type': 'step_done', 'step': 'write', 'attempt': 1, 'status': 'failed'}, {'status': status}]
        envelope = batch(child, rows)
        self.assertEqual(receive_authoritative_run_events(envelope).status_code, 200)
        child.refresh_from_db()
        closed = {'schema_version': 1, 'run_id': child.run_id, 'status': 'closed_no_effects',
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(child)), WIRE_EXECUTION_KEY: current_execution_ticket(child),
            'event_cursor': len(rows), 'closed_at': self.now.isoformat()}
        closed['receipt_sha256'] = terminal.receipt_digest(closed)
        page = {'schema_version': 1, 'run_id': child.run_id, 'batches': [envelope], 'next_cursor': len(rows), 'has_more': False}
        return child, closed, page

    def test_paid_distinct_child_key_waits_until_whole_original_charge_family_is_closed(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        child, child_closed, child_page = self.child_history(run, original)
        stack, post, get = self.transports(closed, page)
        with stack:
            held = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(held, {'state': 'pending', 'reason': 'terminal_original_family_not_closed'})
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())
        stack, post, get = self.transports(child_closed, child_page)
        with stack:
            result = terminal.settle_authoritative_terminal(child.run_id)
        self.assertEqual(result['state'], 'refunded', result)
        effect = Ledger.objects.get(refund_of=original)
        self.assertNotEqual(child.run_request['client_request_id'], run.run_request['client_request_id'])
        self.assertEqual((effect.user_id, effect.delta_microroo), (original.user_id, -original.delta_microroo))
        run.refresh_from_db(); child.refresh_from_db()
        self.assertTrue(terminal.terminal_execution_closed(run))
        self.assertTrue(terminal.terminal_execution_closed(child))

    def test_free_original_grant_and_distinct_child_both_need_their_own_exact_closure(self):
        from .run_dispatch_settlement import _free_obligation_state
        from roo.models import Ledger
        run, original, closed, page = self.synthetic(kind='free')
        child, child_closed, child_page = self.child_history(run, original)
        stack, post, get = self.transports(closed, page)
        with stack:
            held = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(held['state'], 'pending', held)
        self.assertEqual(_free_obligation_state(run), 'reserved')
        stack, post, get = self.transports(child_closed, child_page)
        with stack:
            result = terminal.settle_authoritative_terminal(child.run_id)
        self.assertEqual(result['state'], 'released', result)
        run.refresh_from_db(); child.refresh_from_db()
        self.assertEqual((_free_obligation_state(run), _free_obligation_state(child)), ('released', 'released'))
        self.assertFalse(Ledger.objects.filter(kind='REFUND').exists())
        self.assertTrue(terminal.terminal_execution_closed(run))
        self.assertTrue(terminal.terminal_execution_closed(child))

    def test_active_sibling_preserves_original_spend_and_held_closure_receipt(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        self.child_history(run, original, status='running')
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'pending', result)
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())
        run.refresh_from_db()
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY][terminal.TERMINAL_CLOSURES_KEY][closed[WIRE_EXECUTION_KEY]['execution_id']], closed)

    def verify_delivered_body(self, run):
        import hashlib
        from django.utils import timezone
        from .run_artifact_observations import _digest
        from .run_verified_delivery import project_verified_delivery
        body, revision = '# Retained value\n\nThe independently readable original article.', 'a' * 64
        payload = {'format': 'markdown', 'body': body, 'revision': revision,
                   'article_sha256': hashlib.sha256(body.encode()).hexdigest()}
        record = {'schema_version': 1, 'run_id': run.run_id, 'kind': 'article_body_v1',
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run),
            'payload': payload, 'payload_sha256': _digest(payload), 'source_sha256': 'b' * 64,
            'observed_at': timezone.now().isoformat()}
        record['evidence_sha256'] = _digest(record)
        evidence = {'schema_version': 1, 'run_id': run.run_id, WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)),
                    WIRE_EXECUTION_KEY: current_execution_ticket(run), 'evidence': [record]}
        review = {'revision': revision, 'previewPending': False, 'articleExport': {'version': 1, 'status': 'ready',
            'runId': run.run_id, 'revision': revision, 'format': 'markdown', 'markdown': body, 'metadata': {}, 'media': []}}
        with patch.object(terminal.requests, 'get', side_effect=lambda url, **kw: self.response(evidence if url.endswith('/evidence') else review)):
            project_verified_delivery(run.run_id)
        run.refresh_from_db()

    def test_verified_earned_body_is_kept_and_never_cancelled_or_retrospectively_refunded(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        self.verify_delivered_body(run)
        stack, post, get = self.transports(closed, page)
        with stack:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'kept', result)
        post.assert_not_called(); get.assert_not_called()
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())

    def test_verified_delivery_callback_winning_during_close_keeps_benefit_and_credit_held(self):
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        def post(*args, **kwargs):
            self.verify_delivered_body(run)
            return self.response({'terminal_settlement_v1': closed})
        with patch.object(terminal.requests, 'post', side_effect=post), \
                patch.object(terminal.requests, 'get', return_value=self.response(page)):
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'pending', result)
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())
        from .run_verified_delivery import has_verified_delivery
        self.assertTrue(has_verified_delivery(ContentFactoryRun.objects.get(pk=run.pk)))

    def test_replaced_claim_or_current_ticket_during_close_cannot_authorize_money(self):
        from roo.models import Ledger
        from .tests_run_event_authority import advance_ticket
        run, original, closed, page = self.synthetic()
        def post(*args, **kwargs):
            advance_ticket(ContentFactoryRun.objects.get(pk=run.pk))
            return self.response({'terminal_settlement_v1': closed})
        with patch.object(terminal.requests, 'post', side_effect=post), patch.object(terminal.requests, 'get') as history:
            result = terminal.settle_authoritative_terminal(run.run_id)
        self.assertEqual(result['state'], 'pending', result)
        history.assert_not_called()
        self.assertFalse(Ledger.objects.filter(refund_of=original).exists())

    def test_two_concurrent_consumers_have_one_close_admission_and_one_original_refund(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import close_old_connections
        from roo.models import Ledger
        run, original, closed, page = self.synthetic()
        entered, release = Event(), Event()
        def post(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.response({'terminal_settlement_v1': closed})
        def invoke():
            close_old_connections()
            try:
                return terminal.settle_authoritative_terminal(run.run_id)
            finally:
                close_old_connections()
        with patch.object(terminal.requests, 'post', side_effect=post) as cancel, \
                patch.object(terminal.requests, 'get', side_effect=lambda *a, **kw: self.response(page)), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke)
            self.assertTrue(entered.wait(5))
            try:
                other = pool.submit(invoke).result(timeout=5)
                self.assertEqual(other, {'state': 'pending', 'reason': 'terminal_claim_active'})
            finally:
                release.set()
            result = first.result(timeout=10)
        self.assertEqual(result['state'], 'refunded', result)
        self.assertEqual(cancel.call_count, 1)
        self.assertEqual(Ledger.objects.filter(refund_of=original).count(), 1)

    def test_terminal_transport_and_grant_scope_never_run_inside_sql_transaction(self):
        from django.db import transaction
        run, original, closed, page = self.synthetic()
        stack, post, get = self.transports(closed, page)
        with stack, transaction.atomic(), self.assertRaisesRegex(terminal.TerminalSettlementPending, 'terminal_http_under_database_lock'):
            terminal.settle_authoritative_terminal(run.run_id)
        post.assert_not_called(); get.assert_not_called()

    def test_invalid_canonical_receipt_and_malformed_meta_are_neutrally_throttled_without_starving_valid_owner(self):
        from datetime import timedelta
        from django.utils import timezone
        from .run_event_authority import receive_authoritative_run_events
        from .run_ownership import OWNER_RESULT_KEY, TERMINAL_TRANSPORT_RESULT_KEY, public_run_value
        from .tests_run_event_authority import batch
        run, original, closed, page = self.synthetic()
        self.now = timezone.now()
        invalid, _, invalid_spend = fixtures.DispatchSettlementTests.paid(self)
        self.assertEqual(receive_authoritative_run_events(batch(invalid, [{'status': 'running'}, {'status': 'failed'}])).status_code, 200)
        invalid.refresh_from_db()
        result = deepcopy(invalid.result)
        result[OWNER_RESULT_KEY]['identity']['organization_id'] = 'not-the-canonical-organization'
        result[RECONCILIATION_RESULT_KEY] = 'malformed-retained-reconciliation-metadata'
        ContentFactoryRun.objects.filter(pk=invalid.pk).update(result=result, created_at=self.now - timedelta(days=5))
        before = ContentFactoryRun.objects.values().get(pk=invalid.pk)
        legacy = ContentFactoryRun.objects.create(run_id='historical-terminal', domain=self.org.domain, organization=self.org,
            workflow='article_generation', status='failed', run_request={'roo_points_ledger_id': 'unknown'})
        legacy_before = ContentFactoryRun.objects.values().get(pk=legacy.pk)
        stack, post, get = self.transports(closed, page)
        with stack:
            first = terminal.run_terminal_settlement_sweep(limit=1, now=self.now)
            self.assertEqual((first['checked'], first['pending']), (1, 1))
            post.assert_not_called(); get.assert_not_called()
            invalid.refresh_from_db()
            self.assertEqual(invalid.result[RECONCILIATION_RESULT_KEY], before['result'][RECONCILIATION_RESULT_KEY])
            self.assertEqual(invalid.result[OWNER_RESULT_KEY], before['result'][OWNER_RESULT_KEY])
            for field, value in before.items():
                if field != 'result':
                    self.assertEqual(getattr(invalid, field), value, field)
            self.assertNotIn(TERMINAL_TRANSPORT_RESULT_KEY, public_run_value(invalid.result))
            self.assertEqual(ContentFactoryRun.objects.values().get(pk=legacy.pk), legacy_before)
            second = terminal.run_terminal_settlement_sweep(limit=1, now=self.now + timedelta(seconds=1))
        self.assertEqual((second['checked'], second['refunded']), (1, 1), second)
        from roo.models import Ledger
        self.assertEqual(Ledger.objects.filter(refund_of=original).count(), 1)
        self.assertFalse(Ledger.objects.filter(refund_of=invalid_spend).exists())
