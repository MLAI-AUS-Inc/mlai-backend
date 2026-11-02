"""Settle a started original obligation only after durable CF effect closure.

Terminal lifecycle is observational, not a no-effects proof. The paired CF
closure permanently fences its exact ticket and attests all retained benefit
history; the backend still drains and validates the complete original family.
All amounts and bucket restoration remain in the existing charge controller.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import hmac
import re

from .run_event_authority import canonical_bytes
from .run_ownership import (RECONCILIATION_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, owner_receipt, require_event_execution, require_event_identity, wire_identity)

TERMINAL_CLOSURES_KEY = 'terminal_closures'
TERMINAL_SETTLEMENT_KEY = 'terminal_settlement'
BLOCKED_WALL = timedelta(hours=48)
MAX_TERMINAL_BYTES = 131_072
MAX_TERMINAL_SECONDS = 15
_RECEIPT_KEYS = frozenset({'schema_version', 'run_id', 'status', WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    'event_cursor', 'closed_at', 'receipt_sha256'})
_STATE_KEYS = frozenset({'schema_version', 'state', 'run_id', 'admission_digest', 'execution_id',
    'closure_sha256', 'original_ledger_id', 'effect_id', 'at', 'settlement_sha256'})
_HASH = re.compile(r'[a-f0-9]{64}\Z')


class TerminalSettlementPending(ValueError):
    """The original obligation stays held until complete current proof exists."""


def receipt_digest(value, *, field='receipt_sha256'):
    """Hash exact paired JSON bytes, without stripping private wire echoes."""
    return hashlib.sha256(canonical_bytes({key: item for key, item in value.items() if key != field})).hexdigest()


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if type(value) is str and 1 <= len(value) <= 40 else None
        return parsed if parsed is not None and parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def validate_terminal_receipt(run, payload):
    """Validate an authenticated CF close acknowledgement, never a status hint.

    The configured CF service connection authenticates this receipt. Its hash
    fixes immutable bytes; it is not a signature or independent authorization.
    A positive receipt alone does not authorize any financial effect.
    """
    if (not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS
            or type(payload.get('schema_version')) is not int or payload['schema_version'] != 1
            or payload.get('run_id') != run.run_id or payload.get('status') != 'closed_no_effects'
            or type(payload.get('event_cursor')) is not int or not 1 <= payload['event_cursor'] < 2**53
            or _timestamp(payload.get('closed_at')) is None
            or type(payload.get('receipt_sha256')) is not str or _HASH.fullmatch(payload['receipt_sha256']) is None):
        raise TerminalSettlementPending('terminal_closure_invalid')
    try:
        require_event_identity(run, payload)
        require_event_execution(run, payload, current=True)
        valid = hmac.compare_digest(receipt_digest(payload), payload['receipt_sha256'])
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise TerminalSettlementPending('terminal_closure_invalid') from exc
    if not valid:
        raise TerminalSettlementPending('terminal_closure_digest_mismatch')
    return deepcopy(payload)


def _meta(run):
    result = getattr(run, 'result', None)
    value = result.get(RECONCILIATION_RESULT_KEY) if isinstance(result, dict) else None
    return deepcopy(value) if isinstance(value, dict) else {}


def terminal_execution_closed(run):
    """Refuse continuation of a current obligation already refunded/released.

    Callers granting work must hold their fresh canonical Run lock. Malformed
    private settlement state raises rather than silently reopening execution.
    This read grants no refund, release, new ticket or retrospective unpublish.
    """
    from .run_dispatch_settlement import _free_obligation_state, _original_request_intact, _spend
    from .website_contract import evidence_digest
    state = _meta(run).get(TERMINAL_SETTLEMENT_KEY)
    if state is None:
        return False
    if (not isinstance(state, dict) or set(state) != _STATE_KEYS
            or type(state.get('schema_version')) is not int or state['schema_version'] != 1
            or state.get('state') not in {'refunded', 'released', 'kept', 'none'}
            or state.get('run_id') != run.run_id or _timestamp(state.get('at')) is None
            or type(state.get('settlement_sha256')) is not str or _HASH.fullmatch(state['settlement_sha256']) is None
            or not hmac.compare_digest(receipt_digest(state, field='settlement_sha256'), state['settlement_sha256'])
            or not _original_request_intact(run)
            or state.get('admission_digest') != evidence_digest(wire_identity(owner_receipt(run)))
            or state.get('execution_id') != current_execution_ticket(run)['execution_id']):
        raise TerminalSettlementPending('terminal_settlement_history_invalid')
    if state['state'] not in {'refunded', 'released'}:
        return False
    if (type(state.get('original_ledger_id')) is not int or state['original_ledger_id'] <= 0
            or type(state.get('closure_sha256')) is not str or _HASH.fullmatch(state['closure_sha256']) is None):
        raise TerminalSettlementPending('terminal_settlement_history_invalid')
    closures = _meta(run).get(TERMINAL_CLOSURES_KEY)
    if not isinstance(closures, dict):
        raise TerminalSettlementPending('terminal_settlement_history_invalid')
    closed = validate_terminal_receipt(run, closures.get(state['execution_id']))
    if state.get('closure_sha256') != closed['receipt_sha256']:
        raise TerminalSettlementPending('terminal_settlement_history_invalid')
    if state['state'] == 'released':
        from .run_dispatch_settlement import _free_original
        if (state.get('effect_id') is not None or _spend(run) is not None or _free_obligation_state(run) != 'released'
                or str(state['original_ledger_id']) != str(_free_original(run).run_request.get('roo_points_ledger_id'))):
            raise TerminalSettlementPending('terminal_settlement_release_unconfirmed')
    else:
        from roo.models import Ledger
        spend = _spend(run)
        if type(state.get('effect_id')) is not int or state['effect_id'] <= 0:
            raise TerminalSettlementPending('terminal_settlement_refund_unconfirmed')
        effect = Ledger.objects.filter(pk=state.get('effect_id'), kind='REFUND').first()
        if (spend is None or state.get('original_ledger_id') != spend.pk or effect is None
                or effect.refund_of_id != spend.pk or effect.user_id != spend.user_id
                or effect.source != spend.source or effect.delta_microroo != -spend.delta_microroo):
            raise TerminalSettlementPending('terminal_settlement_refund_unconfirmed')
    return True
