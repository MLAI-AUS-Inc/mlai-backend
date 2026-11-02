"""Actual original-debit proof for the terminal continuation fence."""
from copy import deepcopy
from django.test import TransactionTestCase, override_settings
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import run_terminal_settlement as terminal
from .run_ownership import (RECONCILIATION_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, owner_receipt, wire_identity)
from . import tests_run_dispatch_settlement as fixtures
from .tests_run_event_authority import FLAGS
from .website_contract import evidence_digest


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
