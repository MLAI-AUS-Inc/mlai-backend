"""Settle a started original obligation only after durable CF effect closure.

Terminal lifecycle is observational, not a no-effects proof. The paired CF
closure permanently fences its exact ticket and attests all retained benefit
history; the backend still drains and validates the complete original family.
All amounts and bucket restoration remain in the existing charge controller.
"""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from urllib.parse import quote
from uuid import uuid4
import json
import requests
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone
from datetime import datetime, timedelta
import hashlib
import hmac
import re

from .run_event_authority import canonical_bytes
from .run_ownership import (RECONCILIATION_RESULT_KEY, TERMINAL_TRANSPORT_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
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
    topic = _topic_obligation(run)
    if (not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS | ({'kind'} if topic else set())
            or topic and payload.get('kind') != 'topic_selection_v1'
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
    if isinstance(result, dict) and RECONCILIATION_RESULT_KEY in result and not isinstance(value, dict):
        raise TerminalSettlementPending('terminal_reconciliation_metadata_invalid')
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
        # A sibling can settle the same original obligation. Refusing reuse
        # needs the real immutable financial effect, not a fabricated local
        # closure/status receipt on every family member.
        if owner_receipt(run) is None:
            return False
        if not _original_request_intact(run):
            raise TerminalSettlementPending('terminal_original_request_changed')
        spend = _spend(run)
        if spend is not None:
            from roo.models import Ledger
            refunds = list(Ledger.objects.filter(refund_of_id=spend.pk).order_by('pk')[:2])
            if not refunds:
                return False
            if (len(refunds) != 1 or refunds[0].kind != 'REFUND' or refunds[0].user_id != spend.user_id
                    or refunds[0].source != spend.source or refunds[0].delta_microroo != -spend.delta_microroo):
                raise TerminalSettlementPending('terminal_original_refund_history_ambiguous')
            return True
        return _free_obligation_state(run) == 'released'
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


# Transport/claim bookkeeping is private backend state and never source progress.

CLAIM_SECONDS = 60
RETRY_SECONDS = 120
MAX_FAMILY = 100
MAX_SWEEP_SECONDS = 45
_terminal_authority = ContextVar('run_terminal_refund_authority', default=None)


def _save_meta(run, meta):
    from workflow_runs.models import ContentFactoryRun
    ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, RECONCILIATION_RESULT_KEY: meta})
    run.result = {**run.result, RECONCILIATION_RESULT_KEY: meta}


def _transport(run, now, outcome, *, delay=RETRY_SECONDS):
    """Write only neutral cadence; never reconstruct a malformed authority."""
    from workflow_runs.models import ContentFactoryRun
    value = {'schema_version': 1, 'last_attempt_at': now.isoformat(),
             'next_at': (now + timedelta(seconds=delay)).isoformat(),
             'last_outcome': outcome if re.fullmatch(r'[a-z_]{1,80}', outcome) else 'terminal_proof_pending'}
    ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, TERMINAL_TRANSPORT_RESULT_KEY: value})
    run.result = {**run.result, TERMINAL_TRANSPORT_RESULT_KEY: value}


def _locked(run):
    from .run_dispatch_settlement import _locked_run
    current = _locked_run(run.pk, run.organization_id)
    echo = {WIRE_REQUEST_KEY: wire_identity(owner_receipt(current)), WIRE_EXECUTION_KEY: current_execution_ticket(current)}
    require_event_identity(current, echo)
    require_event_execution(current, echo, current=True)
    return current


def _topic_obligation(run):
    """Identify the actual original research charge, never an article absence."""
    from .billing_lineage import TOPIC_CHARGE_PREFIX
    from .first_article_entitlement import TOPIC_ACTIONS
    from .run_dispatch_settlement import _spend
    from .run_topic_delivery import topic_kind
    saved = run.run_request or {}
    if topic_kind(run) is None or saved.get('roo_points_action') not in TOPIC_ACTIONS:
        return False
    spend = _spend(run)
    return spend is None or spend.idempotency_key.startswith(TOPIC_CHARGE_PREFIX)


def _benefit(run):
    """Historical topic choices and unresolved saved value cannot become credit."""
    from .no_delivery_refunds import _direct_delivery
    if _direct_delivery(run):
        return True
    if not _topic_obligation(run):
        return False
    from .run_topic_delivery import has_verified_topic_delivery
    return has_verified_topic_delivery(run)


def _topic_uncertainty(run):
    if not _topic_obligation(run):
        return False
    from .run_topic_delivery import has_verified_topic_delivery
    from .run_article_charge import article_charge_receipt
    selection = getattr(run, 'selection_data', None)
    selection = selection if isinstance(selection, dict) else {}
    result = run.result or {}
    return bool(not has_verified_topic_delivery(run) and (selection.get('options') or selection.get('suggested_islands')
        or result.get('suggested_islands') or article_charge_receipt(run)))


def _obligation(run):
    from .billing_lineage import ARTICLE_CHARGE_PREFIX, TOPIC_CHARGE_PREFIX
    from .first_article_entitlement import TOPIC_ACTIONS, entitled_request
    from .run_dispatch_settlement import _spend, _free_obligation_state
    spend = _spend(run)
    if spend is not None:
        return (spend, None) if (spend.idempotency_key.startswith(ARTICLE_CHARGE_PREFIX)
            or _topic_obligation(run) and spend.idempotency_key.startswith(TOPIC_CHARGE_PREFIX)) else (None, None)
    request = run.run_request or {}
    free = (_free_obligation_state(run) if entitled_request(request)
            and (request.get('roo_points_action') not in TOPIC_ACTIONS or _topic_obligation(run)) else None)
    return None, free


def _research_carrier(run, ledger_id):
    from .run_article_charge import article_charge_receipt
    from .billing_lineage import original_spend_for_run
    charge = article_charge_receipt(run)
    if charge is None or charge['ledger_id'] != ledger_id:
        return False
    initial = original_spend_for_run(run)
    from .first_article_entitlement import TOPIC_ACTIONS, entitled_request
    if (run.run_request or {}).get('roo_points_action') not in TOPIC_ACTIONS:
        return False
    if initial is not None:
        return initial.pk != ledger_id
    # An original zero-point topic grant is also a distinct obligation. Its
    # exact company/key/ledger proof must remain reserved; it is never the
    # subsequently approved ARTICLE spend being settled by this family.
    from .run_dispatch_settlement import _free_obligation_state
    return (entitled_request(run.run_request) and str(run.run_request.get('roo_points_ledger_id')) != str(ledger_id)
        and _free_obligation_state(run) == 'reserved')


def _terminal_projection(run):
    from .run_ownership import EVENT_RESULT_KEY
    from .run_status_contract import TERMINAL
    ledger = (run.result or {}).get(EVENT_RESULT_KEY)
    projection = ledger.get('projection') if isinstance(ledger, dict) else None
    if (not isinstance(projection, dict) or projection.get('execution_id') != current_execution_ticket(run)['execution_id']
            or projection.get('status') not in TERMINAL or run.status != projection['status']):
        raise TerminalSettlementPending('terminal_current_projection_missing')
    return projection


def _due(run, now):
    projection = _terminal_projection(run)
    if projection['status'] != 'blocked':
        return True
    failure = projection.get('failure') or {}
    if failure.get('code') in {'RUN_BUDGET_EXHAUSTED', 'ONBOARDING_MODEL_BUDGET_EXHAUSTED'}:
        return True
    from .run_dispatch_settlement import _free_original
    from roo.models import Ledger
    spend, free = _obligation(run)
    if spend is not None:
        started = spend.created_at
    elif free is not None:
        original = _free_original(run)
        grant = Ledger.objects.filter(pk=(original.run_request or {}).get('roo_points_ledger_id')).first()
        started = grant.created_at if grant is not None else None
    else:
        return False
    return started is not None and now >= started + BLOCKED_WALL


def _closed(run):
    closures = _meta(run).get(TERMINAL_CLOSURES_KEY, {})
    if not isinstance(closures, dict):
        raise TerminalSettlementPending('terminal_closure_history_invalid')
    raw = closures.get(current_execution_ticket(run)['execution_id'])
    return validate_terminal_receipt(run, raw) if raw is not None else None


def _journal(run, closed):
    """Prove every globally numbered event through the permanent close cursor."""
    from .run_ownership import EVENT_RESULT_KEY
    from .run_event_authority import validate_authoritative_envelope
    meta = _meta(run)
    cursor = closed['event_cursor']
    if (type(meta.get('cursor')) is not int or meta['cursor'] != cursor
            or meta.get('has_more') is not False or meta.get('claim') is not None):
        raise TerminalSettlementPending('terminal_history_not_drained')
    ledger = (run.result or {}).get(EVENT_RESULT_KEY)
    events = ledger.get('events') if isinstance(ledger, dict) else None
    if not isinstance(events, dict) or len(events) != cursor:
        raise TerminalSettlementPending('terminal_journal_gap')
    sequences = set()
    for key, record in events.items():
        if not isinstance(record, dict) or not isinstance(record.get('event'), dict):
            raise TerminalSettlementPending('terminal_journal_invalid')
        raw = {'event': 'run_events_v2', 'schema_version': 1, 'run_id': run.run_id,
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: record.get('execution'),
            'events': [record['event']]}
        validated = validate_authoritative_envelope(raw)
        require_event_identity(run, raw)
        require_event_execution(run, raw, generation=validated.execution['generation'])
        expected_key = json.dumps([validated.execution['execution_id'], record['event']['generation'], record['event']['seq']], separators=(',', ':'))
        if (key != expected_key or record.get('execution_id') != validated.execution['execution_id']
                or record.get('digest') != validated.events[0]['digest']):
            raise TerminalSettlementPending('terminal_journal_invalid')
        seq = record['event']['seq']
        if seq in sequences or not 1 <= seq <= cursor:
            raise TerminalSettlementPending('terminal_journal_global_order_invalid')
        sequences.add(seq)
    # Cardinality + bounded distinct positive sequence numbers proves exactly
    # 1..cursor without allocating an attacker-sized range or compacting gaps.
    _terminal_projection(run)


def terminal_closed_without_effects(run):
    """Read retained exact closure for the existing free-family release guard."""
    from .run_dispatch_settlement import _original_request_intact
    from .run_reconciliation import authoritative_run
    from .no_delivery_refunds import _direct_delivery, points_receipt
    from .article_publication_evidence import publication_state
    try:
        closed = _closed(run)
        if (not authoritative_run(run) or not _original_request_intact(run) or closed is None
                or _benefit(run) or _topic_uncertainty(run) or points_receipt(run).get('state') in {'captured', 'kept'}
                or publication_state(run) != 'none'):
            return False
        _journal(run, closed)
        return True
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def _family(run, stamp):
    from workflow_runs.models import ContentFactoryRun
    from .run_dispatch_settlement import _family_query
    family = list(ContentFactoryRun.objects.select_for_update().filter(_family_query(stamp)).order_by('pk')[:MAX_FAMILY + 1])
    if len(family) > MAX_FAMILY or not any(other.pk == run.pk for other in family):
        raise TerminalSettlementPending('terminal_original_family_unbounded')
    return family


def _family_proof(run, family, stamp):
    from .run_dispatch_settlement import (_original_request_intact, _spend, _free_original,
        dispatch_closed_without_effects)
    from .run_reconciliation import authoritative_run
    from .no_delivery_refunds import _direct_delivery, points_receipt
    from .article_publication_evidence import publication_state
    for other in family:
        if (not authoritative_run(other) or other.organization_id != run.organization_id
                or other.domain.casefold() != run.domain.casefold() or not _original_request_intact(other)):
            raise TerminalSettlementPending('terminal_original_family_unverified')
        if _research_carrier(other, stamp):
            continue
        spend, free = _obligation(other)
        actual = spend.pk if spend is not None else (_free_original(other).run_request or {}).get('roo_points_ledger_id') if free is not None else None
        if str(actual) != str(stamp):
            raise TerminalSettlementPending('terminal_original_family_charge_changed')
        if (_benefit(other) or _topic_uncertainty(other) or points_receipt(other).get('state') in {'captured', 'kept'}
                or publication_state(other) != 'none'):
            raise TerminalSettlementPending('terminal_original_family_benefit_or_uncertainty')
        # An unstarted sibling may already have its separate immutable dispatch
        # tombstone. A started sibling must have its own complete close proof.
        closed = _closed(other)
        if closed is not None:
            _journal(other, closed)
        elif not dispatch_closed_without_effects(other):
            raise TerminalSettlementPending('terminal_original_family_not_closed')


def _close_remote(run):
    from .reconciliation import _remote_base_url, _remote_headers
    from .website_cancellation_retirement import expected_article_cancellation
    if connection.in_atomic_block:
        raise TerminalSettlementPending('terminal_http_under_database_lock')
    base, headers = _remote_base_url(), _remote_headers()
    if not base or not headers.get('X-API-KEY'):
        raise TerminalSettlementPending('terminal_remote_unavailable')
    body = {WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run),
        'cancellation_reason': 'system_stop', 'cancelled_by': 'system'}
    topic = _topic_obligation(run)
    if not topic:
        body['expected_cancellation_v1'] = expected_article_cancellation(run)
    deadline = monotonic() + MAX_TERMINAL_SECONDS
    response = requests.post(base + '/api/runs/' + quote(run.run_id, safe='') + '/cancel', json=body,
        headers=headers, timeout=(3, 10), stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            raise TerminalSettlementPending('terminal_effect_closure_pending')
        data = bytearray()
        for chunk in response.iter_content(chunk_size=1):
            if len(data) + len(chunk) > MAX_TERMINAL_BYTES or monotonic() > deadline:
                raise TerminalSettlementPending('terminal_closure_read_unbounded')
            data.extend(chunk)
        value = json.loads(data)
        key = 'terminal_topic_settlement_v1' if topic else 'terminal_settlement_v1'
        return validate_terminal_receipt(run, value.get(key) if isinstance(value, dict) else None)
    finally:
        response.close()


def _claim(run, now, *, expected_run_binding=None):
    with transaction.atomic():
        run = _locked(run)
        if expected_run_binding is not None:
            from .website_cancellation_retirement import run_binding
            if run_binding(run) != expected_run_binding:
                raise TerminalSettlementPending('terminal_expected_binding_changed')
        meta = _meta(run)
        if terminal_execution_closed(run):
            _transport(run, now, 'already_settled', delay=3600)
            return run, None
        spend, free = _obligation(run)
        if spend is None and free is None or spend is not None and _research_carrier(run, spend.pk):
            raise TerminalSettlementPending('terminal_no_original_obligation')
        if not _due(run, now):
            raise TerminalSettlementPending('terminal_not_due')
        from .no_delivery_refunds import _direct_delivery, points_receipt
        from .article_publication_evidence import publication_state
        from .run_dispatch_settlement import _free_original, _original_request_intact
        from .run_reconciliation import authoritative_run
        stamp = spend.pk if spend is not None else int((_free_original(run).run_request or {}).get('roo_points_ledger_id'))
        family = _family(run, stamp)
        for other in family:
            if (not authoritative_run(other) or not _original_request_intact(other)
                    or other.organization_id != run.organization_id or other.domain.casefold() != run.domain.casefold()):
                raise TerminalSettlementPending('terminal_original_family_unverified')
            if _research_carrier(other, stamp):
                continue
            other_spend, other_free = _obligation(other)
            actual = other_spend.pk if other_spend is not None else (_free_original(other).run_request or {}).get('roo_points_ledger_id') if other_free is not None else None
            if str(actual) != str(stamp):
                raise TerminalSettlementPending('terminal_original_family_charge_changed')
            if _benefit(other) or points_receipt(other).get('state') in {'captured', 'kept'}:
                _record_retained(run, stamp, now)
                return run, None
            if _topic_uncertainty(other) or publication_state(other) != 'none':
                raise TerminalSettlementPending('terminal_original_family_benefit_or_uncertainty')
        until = _timestamp((meta.get('terminal_claim') or {}).get('until'))
        if until is not None and until > now:
            raise TerminalSettlementPending('terminal_claim_active')
        claim = {'nonce': uuid4().hex, 'execution_id': current_execution_ticket(run)['execution_id'],
                 'until': (now + timedelta(seconds=CLAIM_SECONDS)).isoformat()}
        meta.update(terminal_claim=claim, terminal_last_attempt_at=now.isoformat(), terminal_next_at=claim['until'])
        _save_meta(run, meta)
        _transport(run, now, 'claimed', delay=CLAIM_SECONDS)
        return run, claim


def _retain(run, claim, closed):
    with transaction.atomic():
        run = _locked(run)
        meta = _meta(run)
        if meta.get('terminal_claim') != claim or current_execution_ticket(run)['execution_id'] != claim['execution_id']:
            raise TerminalSettlementPending('terminal_claim_superseded')
        closed = validate_terminal_receipt(run, closed)
        closures = meta.setdefault(TERMINAL_CLOSURES_KEY, {})
        old = closures.get(claim['execution_id'])
        if old is not None and canonical_bytes(old) != canonical_bytes(closed):
            raise TerminalSettlementPending('terminal_closure_conflicting_replay')
        closures[claim['execution_id']] = closed
        _save_meta(run, meta)
        return run


def _finish(run, claim, now, outcome):
    with transaction.atomic():
        run = _locked(run)
        meta = _meta(run)
        if meta.get('terminal_claim') == claim:
            meta.pop('terminal_claim', None)
            meta.update(terminal_last_outcome=outcome, terminal_next_at=(now + timedelta(seconds=RETRY_SECONDS)).isoformat())
            _save_meta(run, meta)
            _transport(run, now, outcome)


def _record_retained(run, stamp, now):
    from .website_contract import evidence_digest
    meta = _meta(run)
    state = {'schema_version': 1, 'state': 'kept', 'run_id': run.run_id,
        'admission_digest': evidence_digest(wire_identity(owner_receipt(run))),
        'execution_id': current_execution_ticket(run)['execution_id'], 'closure_sha256': None,
        'original_ledger_id': stamp, 'effect_id': None, 'at': now.isoformat()}
    state['settlement_sha256'] = receipt_digest(state, field='settlement_sha256')
    meta[TERMINAL_SETTLEMENT_KEY] = state
    meta.pop('terminal_claim', None)
    _save_meta(run, meta)


def _defer_unclaimed(run, now, reason):
    """A cheap invalid/not-due oldest row cannot starve later obligations."""
    with transaction.atomic():
        from workflow_runs.models import ContentFactoryRun
        from .run_ownership import OWNER_RESULT_KEY
        run = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first()
        result = getattr(run, 'result', None)
        marker = result.get(OWNER_RESULT_KEY) if isinstance(result, dict) else None
        authority = marker.get('authority') if isinstance(marker, dict) else None
        if (not isinstance(authority, dict) or authority.get('backend_facts') is not True
                or type(authority.get('event_protocol')) is not int or authority['event_protocol'] != 1):
            return
        existing = result.get(RECONCILIATION_RESULT_KEY)
        meta = existing if isinstance(existing, dict) else {}
        claim = meta.get('terminal_claim')
        until = _timestamp(claim.get('until')) if isinstance(claim, dict) else None
        if until is not None and until > now:
            return
        _transport(run, now, reason)


@contextmanager
def _refund_scope(run, family, stamp, *, spend):
    from .billing_lineage import ARTICLE_CHARGE_PREFIX, TOPIC_CHARGE_PREFIX
    from .run_dispatch_settlement import _free_original
    if not connection.in_atomic_block:
        raise TerminalSettlementPending('terminal_refund_scope_requires_lock')
    _family_proof(run, family, stamp)
    saved = _free_original(run).run_request if spend is None else run.run_request
    scope = {'pk': run.pk, 'organization_id': run.organization_id, 'domain': run.domain,
        'execution_id': current_execution_ticket(run)['execution_id'], 'closure_sha256': _closed(run)['receipt_sha256'],
        'original_key': spend.idempotency_key.removeprefix(TOPIC_CHARGE_PREFIX if _topic_obligation(run) else ARTICLE_CHARGE_PREFIX)
            if spend is not None else saved['client_request_id'],
        'ledger_id': str(stamp), 'user_id': spend.user_id if spend is not None else None, 'free': spend is None}
    token = _terminal_authority.set(scope)
    try:
        yield scope
    finally:
        _terminal_authority.reset(token)


def terminal_refund_authority_matches(*, user, article_request, resolved_domain):
    """Read only the exact private locked terminal original-charge scope."""
    from workflow_runs.models import ContentFactoryRun
    from .billing import normalize_content_factory_domain
    from .run_dispatch_settlement import _original_request_intact
    scope = _terminal_authority.get()
    if not isinstance(scope, dict) or not connection.in_atomic_block or not isinstance(article_request, dict):
        return False
    if (normalize_content_factory_domain(resolved_domain) != normalize_content_factory_domain(scope['domain'])
            or normalize_content_factory_domain(article_request.get('domain')) != normalize_content_factory_domain(scope['domain'])
            or article_request.get('client_request_id') != scope['original_key']
            or str(article_request.get('roo_points_ledger_id')) != scope['ledger_id']
            or getattr(user, 'pk', None) != scope['user_id']
            or not scope['free'] and str(article_request.get('roo_points_billing_user_id')) != str(scope['user_id'])):
        return False
    run = ContentFactoryRun.objects.filter(pk=scope['pk'], organization_id=scope['organization_id']).first()
    try:
        return (run is not None and _original_request_intact(run) and terminal_closed_without_effects(run)
            and current_execution_ticket(run)['execution_id'] == scope['execution_id']
            and _closed(run)['receipt_sha256'] == scope['closure_sha256'])
    except (ValueError, TypeError, KeyError):
        return False


def _settle(run, claim, now):
    from .run_dispatch_settlement import _free_original, _free_obligation_state
    from .run_backend_facts import update_job_facts
    from .models import ContentFactoryJob
    from roo.models import Ledger
    from .website_contract import evidence_digest
    from integrations.services.article_generation import (refund_content_factory_request_for_user,
        refund_content_factory_topic_generation_for_user)
    from workflow_runs.models import ContentFactoryRun
    with transaction.atomic():
        run = _locked(run)
        meta = _meta(run)
        if terminal_execution_closed(run):
            return deepcopy(meta[TERMINAL_SETTLEMENT_KEY])
        if meta.get('terminal_claim') != claim or current_execution_ticket(run)['execution_id'] != claim['execution_id']:
            raise TerminalSettlementPending('terminal_claim_superseded')
        spend, free = _obligation(run)
        controller = refund_content_factory_topic_generation_for_user if _topic_obligation(run) else refund_content_factory_request_for_user
        stamp = spend.pk if spend is not None else int((_free_original(run).run_request or {}).get('roo_points_ledger_id')) if free is not None else None
        if stamp is None:
            raise TerminalSettlementPending('terminal_original_obligation_missing')
        family = _family(run, stamp)
        run = next(other for other in family if other.pk == run.pk)
        _family_proof(run, family, stamp)
        fields = ('run_request', 'created_at', 'updated_at', 'last_event_emitted_at', 'reconciled_at',
                  'status', 'current_step', 'organization_id', 'domain', 'approval_state')
        before = {other.pk: {field: deepcopy(getattr(other, field)) for field in fields} for other in family}
        if spend is not None:
            refunds = list(Ledger.objects.filter(refund_of_id=spend.pk, kind='REFUND', user_id=spend.user_id,
                source=spend.source).order_by('pk')[:2])
            if len(refunds) > 1:
                raise TerminalSettlementPending('terminal_refund_history_ambiguous')
            if refunds:
                effect = refunds[0]
            else:
                with _refund_scope(run, family, stamp, spend=spend) as scope:
                    # The original goal is a retired competing execution owner.
                    # This minimal proved request cannot invoke its old defer path.
                    request = {'domain': run.domain, 'client_request_id': scope['original_key'],
                        'roo_points_ledger_id': str(spend.pk), 'roo_points_billing_user_id': spend.user_id}
                    effect = controller(user=spend.user, actor_id='system', article_request=request,
                        resolved_domain=run.domain, reason='Exact original execution permanently closed without delivered value')
            if (effect is None or effect.kind != 'REFUND' or effect.refund_of_id != spend.pk or effect.user_id != spend.user_id
                    or effect.source != spend.source or effect.delta_microroo != -spend.delta_microroo):
                raise TerminalSettlementPending('terminal_original_refund_unconfirmed')
            update_job_facts(ContentFactoryJob.objects.filter(job_id__in=[other.legacy_job_id or other.run_id for other in family]),
                billing_status='refunded', billing_ledger_id=effect.pk)
            state, effect_id = 'refunded', effect.pk
        else:
            request = deepcopy(_free_original(run).run_request)
            request.pop('first_article_goal_operation_id', None)
            with _refund_scope(run, family, stamp, spend=None):
                controller(user=None, actor_id='system', article_request=request,
                    resolved_domain=run.domain, reason='Exact original execution permanently closed without delivered value')
            if _free_obligation_state(run) != 'released':
                raise TerminalSettlementPending('terminal_original_release_unconfirmed')
            state, effect_id = 'released', None
        if any(any(row[field] != before[row['pk']][field] for field in fields)
               for row in ContentFactoryRun.objects.filter(pk__in=before).values('pk', *fields)):
            raise TerminalSettlementPending('terminal_controller_changed_original_execution')
        run.refresh_from_db()
        meta = _meta(run)
        receipt = {'schema_version': 1, 'state': state, 'run_id': run.run_id,
            'admission_digest': evidence_digest(wire_identity(owner_receipt(run))),
            'execution_id': current_execution_ticket(run)['execution_id'], 'closure_sha256': _closed(run)['receipt_sha256'],
            'original_ledger_id': stamp, 'effect_id': effect_id, 'at': now.isoformat()}
        receipt['settlement_sha256'] = receipt_digest(receipt, field='settlement_sha256')
        meta[TERMINAL_SETTLEMENT_KEY] = receipt
        meta.pop('terminal_claim', None)
        meta.update(terminal_last_outcome=state)
        _save_meta(run, meta)
        _transport(run, now, state)
        return receipt


def settle_authoritative_terminal(run_id, *, now=None, expected_run_binding=None):
    """Close, independently drain, then settle one exact original obligation."""
    from workflow_runs.models import ContentFactoryRun
    from .run_reconciliation import reconcile_authoritative_run, authoritative_run
    if connection.in_atomic_block:
        raise TerminalSettlementPending('terminal_http_under_database_lock')
    now = now or timezone.now()
    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    if run is None or not authoritative_run(run):
        return {'state': 'pending', 'reason': 'terminal_owner_missing'}
    claim = None
    try:
        run, claim = _claim(run, now, expected_run_binding=expected_run_binding)
        if claim is None:
            saved = _meta(run).get(TERMINAL_SETTLEMENT_KEY)
            if saved is not None:
                return deepcopy(saved)
            from .run_dispatch_settlement import _spend
            spend = _spend(run)
            return {'state': 'refunded' if spend is not None else 'released', 'already_settled': True,
                    'original_ledger_id': getattr(spend, 'pk', None)}
        closed = _closed(run) or _close_remote(run)
        run = _retain(run, claim, closed)
        reconcile_authoritative_run(run.run_id, refresh=True, now=now)
        run.refresh_from_db()
        return _settle(run, claim, now)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, requests.RequestException) as exc:
        reason = str(exc) if isinstance(exc, TerminalSettlementPending) else 'terminal_proof_pending'
        if reason == 'terminal_expected_binding_changed':
            return {'state': 'pending', 'reason': reason}
        if claim is not None:
            try:
                _finish(run, claim, now, reason)
            except (ValueError, TypeError, KeyError):
                pass
        else:
            try:
                _defer_unclaimed(run, now, reason)
            except (ValueError, TypeError, KeyError):
                pass
        return {'state': 'pending', 'reason': reason}


def run_terminal_settlement_sweep(*, limit=10, now=None):
    """Fair independently scheduled slice for terminal original obligations."""
    from workflow_runs.models import ContentFactoryRun
    from .run_status_contract import TERMINAL
    from .run_ownership import OWNER_RESULT_KEY
    now = now or timezone.now()
    limit = limit if type(limit) is int and 1 <= limit <= 100 else 10
    path = 'result__' + RECONCILIATION_RESULT_KEY
    cadence = 'result__' + TERMINAL_TRANSPORT_RESULT_KEY
    rows = ContentFactoryRun.objects.filter(status__in=sorted(TERMINAL), **{
        'result__' + OWNER_RESULT_KEY + '__authority__backend_facts': True,
        'result__' + OWNER_RESULT_KEY + '__authority__event_protocol': 1}).filter(
        Q(run_request__roo_points_ledger_id__isnull=False) | Q(result__run_owner_article_charge_v1__ledger_id__isnull=False)).filter(
        Q(**{cadence + '__next_at__isnull': True}) | Q(**{cadence + '__next_at__lte': now.isoformat()})).filter(
        Q(**{path + '__' + TERMINAL_SETTLEMENT_KEY + '__state__isnull': True})
        | Q(**{path + '__' + TERMINAL_SETTLEMENT_KEY + '__state': None})
        | ~Q(**{path + '__' + TERMINAL_SETTLEMENT_KEY + '__state__in': ['refunded', 'released', 'kept', 'none']})).order_by(
        F(cadence + '__last_attempt_at').asc(nulls_first=True), 'created_at', 'pk')
    ids = list(rows.values_list('run_id', flat=True)[:limit])
    deadline = monotonic() + MAX_SWEEP_SECONDS
    result = {'status': 'completed', 'checked': 0, 'refunded': 0, 'released': 0, 'kept': 0, 'pending': 0}
    for run_id in ids:
        if monotonic() >= deadline:
            break
        outcome = settle_authoritative_terminal(run_id, now=now)
        result['checked'] += 1
        result[outcome['state'] if outcome['state'] in {'refunded', 'released', 'kept'} else 'pending'] += 1
    return result
