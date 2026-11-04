"""Bounded, cohort-gated recovery using the shared failure registry and executors.

Decisions are pure reads. Applying one first reserves its attempt under the
owning row lock; remote I/O happens after commit and keeps the same run identity.
Evidence-only writes deliberately leave the worker's stall clock unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
import importlib
import logging
from typing import Literal
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import p2_foundation, registry_policy
from .models import OrganizationContentConfig
from .website_goal import mapping, stamp

logger = logging.getLogger(__name__)
ACTIONS = frozenset({'retry', 'resume', 're_pin', 'rescan', 'advance_rung', 'switch_channel'})
WORKFLOWS = ('article_generation', 'article_revision', 'confirmed_topic', 'direct_generate', 'article_system_setup', 'repo_scan', 'auto_discovery', 'island_refresh')


@dataclass(frozen=True)
class Decision:
    """A closed recovery intent, including the earliest permitted execution time."""
    action: Literal['wait', 'retry', 'resume', 're_pin', 'rescan', 'advance_rung', 'switch_channel', 'await_owner', 'observe', 'stop', 'none']
    at: datetime | None
    reason: str
    attempt: int
    cap: str | None
    mode: str


def policy_mode(organization):
    """Return policy only with both rollout gates and current cohort membership."""
    return 'policy' if (getattr(settings, 'ARTICLES_SELF_HEAL_POLICY_ENABLED', False)
                        and p2_foundation.policy_v2_applies(organization)) else 'observe'


def owns_recovery(run, *, now=None):
    """Stand old recovery down only when a live P2 executor owns this item."""
    from .website_run_authority import event_owned
    if event_owned(run):
        return True
    return policy_mode(run.organization) == 'policy' and decide(run, now=now).reason != 'executor_off'


def _parts(subject):
    if isinstance(subject, ContentFactoryRun):
        return subject.organization, mapping(subject.result), mapping(subject.run_request), subject
    value = mapping(subject)
    operation = value.get('_operation')
    organization = value.get('_organization') or (operation.connection.organization if operation else None)
    return organization, value, mapping(value.get('run_request')), operation


def _code(result):
    from .founder_journey_alerts import run_stop_code
    return run_stop_code(result)


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _rung(result, request):
    value = (mapping(result.get('self_heal_v1')).get('rung')
             or mapping(mapping(result.get('goal')).get('strategy_pin')).get('rung')
             or request.get('strategy_pin'))
    if isinstance(value, dict):
        value = value.get('rung')
    return value if isinstance(value, str) and value in {'adopt', 'native_collection', 'deterministic', 'model_adapter', 'agent'} else 'native_collection'


def _config(organization):
    cached = getattr(organization, '_state', None)
    if cached and 'content_config' in cached.fields_cache:
        return cached.fields_cache['content_config']
    return OrganizationContentConfig.objects.filter(organization=organization).first() if organization else None


def _daily(config, now):
    stored = mapping(mapping(getattr(config, 'pillar_strategy', {})).get('self_heal'))
    day = now.astimezone(dt_timezone.utc).date().isoformat()
    if stored.get('day') == day:
        return {**stored, 'rung_attempts': dict(mapping(stored.get('rung_attempts'))),
                'pull_request_keys': list(stored.get('pull_request_keys') or [])}
    return {'version': 1, 'day': day, 'strategy_attempts': 0, 'rung_attempts': {},
            'pull_requests': 0, 'pull_request_keys': [], 'pull_request_slots': [],
            **{key: stored[key] for key in ('ladder', 'dry_run') if key in stored}}


def _draft(result):
    return bool(result.get('draft_ready_v1') or result.get('articleExport') or result.get('draft_ready')
                or result.get('content_package') or result.get('markdown') or result.get('article'))


def _delivered(subject, result):
    if isinstance(subject, ContentFactoryRun):
        from .no_delivery_refunds import _direct_delivery
        return _direct_delivery(subject)
    return bool(result.get('draft_ready_v1') or result.get('delivery_v1'))


def _executor(action, organization):
    if action == 're_pin':
        return bool(getattr(settings, 'ARTICLES_SOURCE_REPIN_ENABLED', False))
    if action == 'switch_channel':
        return bool(getattr(settings, 'ARTICLES_CHANNEL_SWITCH_ENABLED', False))
    return True


def decide(subject, *, now=None):
    """Choose one registry action without dispatching or changing any receipt."""
    if _event_owner(subject):
        return Decision('observe', None, 'execution_owned', 0, None, 'observe')
    organization, _, _, _ = _parts(subject)
    with registry_policy.organization_read_scope(organization, _config(organization)):
        return _decide(subject, now=now)


def _decide(subject, *, now=None, prospective=False):
    if _event_owner(subject):
        return Decision('observe', None, 'execution_owned', 0, None, 'observe')
    now = now or timezone.now()
    organization, result, request, operation = _parts(subject)
    mode = 'policy' if (getattr(settings, 'ARTICLES_SELF_HEAL_POLICY_ENABLED', False)
                        and registry_policy._policy_v2_for(organization)) else 'observe'
    receipt = mapping(result.get('self_heal_v1'))
    attempts = _count(receipt.get('attempts'))
    attempt = attempts + 1
    code = _code(result)
    entry = registry_policy.report_entry(code) if prospective else registry_policy.effective_entry(code, organization)
    limits = registry_policy.limits()
    first = min(stamp(receipt.get('first_failed_at')) or stamp(result.get('first_failed_at')) or now, now)
    started = min(stamp(receipt.get('last_action_at')) or first, now)
    if _delivered(subject, result):
        return Decision('none', None, 'delivered', attempt, None, mode)
    if getattr(subject, 'status', None) in {'cancelled', 'denied'}:
        return Decision('none', None, 'terminal', attempt, None, mode)
    if receipt.get('last_action') == 'stop':
        return Decision('none', None, 'terminal', attempt, None, mode)
    if isinstance(subject, ContentFactoryRun) and (request.get('first_article_goal_operation_id') or request.get('strategy_goal_id')):
        return Decision('observe', None, 'waiting', attempt, None, mode)
    if (operation is not None and not isinstance(subject, ContentFactoryRun)
            and receipt.get('last_failure_occurrence')
            and receipt['last_failure_occurrence'] == result.get('failure_occurrence_id')):
        return Decision('none', None, 'waiting', attempt, None, mode)
    if receipt.get('code') == 'pull_request_daily_cap' and (stamp(receipt.get('next_at')) or now) > now:
        return Decision('wait', stamp(receipt['next_at']), 'waiting', attempt, 'pull_request_day', mode)
    # An uncertain effect retains financial proof, but never extends the
    # recovery executor beyond the item wall indefinitely.
    if now - first >= timedelta(hours=limits['wall_hours']):
        action = 'switch_channel' if _draft(result) else 'stop'
        if mode == 'policy' and not _executor(action, organization):
            return Decision('observe', None, 'executor_off', attempt, 'wall', mode)
        return Decision(action, None, 'wall' if mode == 'policy' or prospective else 'legacy_observe', attempt, 'wall', mode)
    if entry and entry.get('retry_policy') == 'retry_same_step' and now - first >= timedelta(hours=limits['max_elapsed_hours']):
        action = 'switch_channel' if _draft(result) else 'stop'
        if mode == 'policy' and not _executor(action, organization):
            return Decision('observe', None, 'executor_off', attempt, 'transient_elapsed', mode)
        return Decision(action, None, 'cap' if mode == 'policy' or prospective else 'legacy_observe', attempt, 'transient_elapsed', mode)
    pending = mapping(receipt.get('pending_action'))
    if pending.get('state') in {'reserved', 'uncertain'} and pending.get('action') in ACTIONS:
        action = pending['action']
        if mode == 'policy' and not _executor(action, organization):
            return Decision('observe', None, 'executor_off', _count(pending.get('attempt')), None, mode)
        return Decision(action, stamp(pending.get('lease_until')), pending.get('reason') or 'backoff', _count(pending.get('attempt')), pending.get('cap'), mode)
    action, reason, cap, at = 'stop', 'terminal', None, None
    if entry:
        policy = entry['retry_policy']
        classification = entry['failure_class']
        if policy == 'retry_same_step' and now - first >= timedelta(hours=limits['max_elapsed_hours']):
            action, reason, cap = ('switch_channel' if _draft(result) else 'stop'), 'cap', 'transient_elapsed'
        elif policy == 'repair_owned_candidate' and attempts >= limits['repairable_max_attempts']:
            action, reason, cap = ('switch_channel' if _draft(result) else 'stop'), 'cap', 'repairable_max_attempts'
        elif policy == 're_pin' and _count(mapping(result.get('re_pin_v1')).get('count')) >= limits['source_repins_per_run']:
            action, reason, cap = 'switch_channel', 'cap', 'repairable_max_attempts'
        elif policy in {'retry_same_step', 'repair_owned_candidate', 're_pin'}:
            action = {'retry_same_step': 'retry', 'repair_owned_candidate': 'resume', 're_pin': 're_pin'}[policy]
            reason = {'retry': 'backoff', 'resume': 'repair', 're_pin': 're_pin'}[action]
            backoff = limits['backoff_minutes']
            at = started + timedelta(minutes=backoff[min(attempts, len(backoff) - 1)])
        elif policy == 'advance_strategy':
            day = _daily(_config(organization), now)
            goal = mapping(result.get('goal'))
            rung = _rung(result, request)
            sha = receipt.get('source_sha') or goal.get('current_source_sha') or request.get('expected_source_sha') or ''
            rung_key = f'{rung}:{sha[:12]}'
            if _count(day.get('strategy_attempts')) >= limits['strategy_max_attempts_per_org_per_day']:
                action, reason, cap = 'switch_channel', 'cap', 'strategy_org_day'
            elif _count(day['rung_attempts'].get(rung_key)) >= limits['strategy_max_attempts_per_rung_per_source']:
                action, reason, cap = 'switch_channel', 'cap', 'strategy_rung_source'
            else:
                action, reason = 'advance_rung', 'rung'
        elif policy == 'switch_channel':
            # The closed sequence is the registry's switch-channel policy.
            steps = registry_policy.policy_details('switch_channel').get('steps') or []
            step = steps[min(attempts, len(steps) - 1)] if steps else 'switch_channel'
            action, reason = {'retry_same_step': 'retry'}.get(step, step), 'rung'
        elif classification == 'needs_user':
            action, reason = 'await_owner', 'owner'
        elif classification == 'waiting':
            action, reason = 'observe', 'waiting'
    if mode == 'observe' and not prospective:
        return Decision(action, at, 'legacy_observe', attempt, cap, mode)
    if not _executor(action, organization):
        return Decision('observe', None, 'executor_off', attempt, cap, mode)
    return Decision(action, at, reason, attempt, cap, mode)


def _event_owner(subject):
    from .website_run_authority import event_owned, owned_goal_child
    if isinstance(subject, ContentFactoryRun):
        return event_owned(subject)
    operation = mapping(subject).get('_operation')
    return operation is not None and owned_goal_child(operation) is not None


def _fresh_owned_run(run):
    if run.organization_id is None:
        return False
    from .website_run_authority import event_owned
    with transaction.atomic():
        # Ownership depends only on the fresh backend receipt. Keep its row
        # lock without hydrating article inputs or joining the organisation.
        current = ContentFactoryRun.objects.select_for_update(of=('self',)).only('result').get(pk=run.pk)
        return event_owned(current)


def _write_locked_run(run, receipt):
    """Merge metadata into the fresh Run row the caller already holds locked."""
    from .website_run_authority import event_owned
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Policy metadata requires the current Run row lock')
    if event_owned(run):
        return
    result = {**mapping(run.result), 'self_heal_v1': receipt}
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result


def _write_run(run, receipt):
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        _write_locked_run(current, receipt)
        run.result = current.result


def _save_daily(config, day):
    config.pillar_strategy = {**mapping(config.pillar_strategy), 'self_heal': day}
    OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=config.pillar_strategy)


def _alert(organization, reason, *, run=None, slot=None, details=None):
    from .founder_journey_alerts import queue_founder_journey_alert
    return queue_founder_journey_alert(organization=organization, alert_reason=reason, run=run, slot=slot, details=details)


def _locked_config(organization):
    from organizations.models import Organization
    Organization.objects.select_for_update().get(pk=organization.pk)
    try:
        # The normal path needs one fresh locked read, rather than an ordinary
        # get_or_create read followed by a second read of the same config.
        return OrganizationContentConfig.objects.select_for_update().get(organization=organization)
    except OrganizationContentConfig.DoesNotExist:
        pass
    config, _ = OrganizationContentConfig.objects.get_or_create(organization=organization)
    return OrganizationContentConfig.objects.select_for_update().get(pk=config.pk)


def claim_pull_request_slot(organization, *, key, now=None):
    """Reserve one automatic PR slot atomically; the same key never consumes two."""
    if policy_mode(organization) != 'policy':
        return True
    if not isinstance(key, str) or not key or len(key) > 256:
        return False
    now = now or timezone.now()
    with transaction.atomic():
        config = _locked_config(organization)
        day = _daily(config, now)
        slots = list(day.get('pull_request_slots') or [])
        if key in slots:
            return True
        completed = set(day.get('pull_request_slot_completions') or [])
        if _count(day.get('pull_requests')) + sum(slot not in completed for slot in slots) >= registry_policy.limits()['pull_requests_per_org_per_day']:
            allowed = False
        else:
            day['pull_request_slots'] = slots + [key]
            _save_daily(config, day)
            allowed = True
    if not allowed:
        _alert(organization, 'pull_request_cap', slot=f"day-{day['day']}")
    return allowed


def record_pull_request(organization, *, pr_url, run=None, now=None):
    """Count actual PRs, including founder clicks, once per URL and UTC day."""
    if not isinstance(pr_url, str) or not pr_url or len(pr_url) > 1024:
        return 0
    now = now or timezone.now()
    if not p2_foundation.p2_applies(organization):
        return 0
    with transaction.atomic():
        config = _locked_config(organization)
        day = _daily(config, now)
        keys = day['pull_request_keys']
        if pr_url not in keys:
            if len(keys) >= 256:
                raise ValueError('The daily pull request journal is full.')
            day['pull_request_keys'] = keys + [pr_url]
            day['pull_requests'] = _count(day.get('pull_requests')) + 1
            if run is not None:
                slots = day.get('pull_request_slots') or []
                completions = list(day.get('pull_request_slot_completions') or [])
                prefixes = {f'{run.run_id}:'}
                request = mapping(run.run_request)
                identifiers = {str(run.run_id), str(request.get('client_request_id') or '')}
                from .website_models import WebsiteConnectionOperation
                for field in ('operation_id', 'first_article_goal_operation_id'):
                    operation_id = request.get(field)
                    if not operation_id:
                        continue
                    try:
                        operation = WebsiteConnectionOperation.objects.select_related('connection').filter(
                            pk=operation_id, connection__organization=organization).first()
                    except (ValueError, TypeError):
                        operation = None
                    if operation is None:
                        continue
                    children = {str(mapping(operation.receipt).get(key) or '') for key in (
                        'child_run_id', 'setup_run_id', 'scan_run_id', 'first_article_run_id', 'first_article_publish_run_id')}
                    children.add(str(mapping(operation.payload).get('run_id') or ''))
                    if (identifiers - {''}) & children and request.get('website_connection_id') == str(operation.connection_id) and request.get('connection_generation') == operation.generation:
                        prefixes.add(f'{operation.pk}:')
                match = next((slot for slot in slots if any(slot.startswith(prefix) for prefix in prefixes) and slot not in completions), None)
                if match:
                    day['pull_request_slot_completions'] = completions + [match]
            _save_daily(config, day)
        count = _count(day.get('pull_requests'))
    if count >= registry_policy.limits()['pull_requests_per_org_per_day']:
        _alert(organization, 'pull_request_cap', slot=f"day-{day['day']}")
    return count


def _execute_run(run, decision, *, now):
    if _fresh_owned_run(run):
        return {'status': 'deferred', 'reason': 'execution_owned'}
    if policy_mode(run.organization) != 'policy' or not _executor(decision.action, run.organization):
        return {'status': 'refused'}
    if decision.action == 'switch_channel':
        from .channel_switch import switch_to_copy_ready
        return switch_to_copy_ready(run=run, reason=_code(mapping(run.result)) or decision.cap or decision.reason, now=now)
    if decision.action == 'stop':
        if decision.cap:
            code = 'SELF_HEAL_DEADLINE_REACHED' if decision.cap == 'wall' else 'SELF_HEAL_ATTEMPTS_EXHAUSTED'
            with transaction.atomic():
                current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
                ContentFactoryRun.objects.filter(pk=run.pk).update(result={**mapping(current.result), 'error_code': code})
            _alert(run.organization, 'self_heal_capped', run=run, details={'cap': decision.cap})
        else:
            _alert(run.organization, 'codeless_terminal' if _code(mapping(run.result)) == 'TERMINAL_CODE_MISSING' else 'our_team_item', run=run)
        from .no_delivery_refunds import refund_decision, apply_refund_decision
        run.refresh_from_db()
        return apply_refund_decision(run, refund_decision(run, now=now), now=now)
    if decision.action in {'retry', 'resume', 're_pin', 'rescan'}:
        result = mapping(run.result)
        if result.get('publish_reviewed_v1') and mapping(result.get('article_approval_v1')).get('source') == 'publish_reviewed':
            try:
                retry_publish = getattr(importlib.import_module('content_factory.publish_reviewed_draft'), 'retry_publish')
            except (ImportError, AttributeError):
                return {'status': 'refused'}
            # A publish-only continuation must never enter the article writer.
            return retry_publish(run)
        # This is the existing authority-checked resume path, including its
        # durable operation-attempt reservation for ambiguous retransmission.
        from .vibe_marketing_views import _call_content_factory_run_action
        payload = {'self_heal_key': f'{run.run_id}:{decision.attempt}'}
        if decision.action == 're_pin':
            payload['repin'] = {'to': 'latest'}
        if decision.action == 'rescan':
            payload['self_heal_rescan'] = True
        outcome = _call_content_factory_run_action(run_id=run.run_id, workflow=run.workflow,
            action='resume', payload=payload, transport_errors_are_pending=True, preserve_run_clock=True)
        ack = mapping(mapping(outcome).get('self_heal_resume'))
        intent = 're_pin' if decision.action == 're_pin' else 'rescan' if decision.action == 'rescan' else 'resume'
        if ack != {'version': 1, 'run_id': run.run_id, 'key': payload['self_heal_key'],
                   'intent': intent, 'state': 'accepted'}:
            # Older CF versions ignored these keys and could accidentally queue
            # ordinary work. Only the exact durable task reservation acknowledges
            # policy intent; uncertainty keeps this same attempt and charge held.
            return {**mapping(outcome), 'status': 'action_pending', 'dispatch_state': 'pending'}
        return outcome
    if decision.action == 'advance_rung':
        from .website_models import WebsiteConnectionOperation
        from .website_connections import authority_guard, scoped_run_contract
        from . import website_goal as goals
        identifier = mapping(run.run_request).get('first_article_goal_operation_id') or mapping(run.run_request).get('operation_id')
        # Match the org -> config -> website -> operation lock order used by
        # binding and the owning goal controller, before any mutation.
        with transaction.atomic():
            _locked_config(run.organization)
            with authority_guard(scoped_run_contract(run), action='read'):
                operation = WebsiteConnectionOperation.objects.select_for_update(of=('self',)).select_related('connection__organization').filter(
                    pk=identifier, action='prepare', connection__organization=run.organization).first() if identifier else None
                if operation:
                    if (operation.state not in {'pending', 'needs_attention', 'needs_user'}
                            or operation.generation != operation.connection.generation
                            or not goals.valid_goal(mapping(operation.receipt).get('goal'), goals.scope_for(operation.connection, operation.payload.get('company_id')))):
                        return {'status': 'refused', 'detail': 'The owning website goal changed.'}
                    value = {**operation.receipt, '_organization': run.organization, '_operation': operation, 'run_request': operation.payload}
                    fresh = decide(value, now=now)
                    if fresh.action != 'advance_rung' or fresh.at and fresh.at > now:
                        return {'status': 'refused', 'detail': 'The website controller owns the next action.'}
                    answer = _apply_operation(value, fresh, now=now)
                    operation.save(update_fields=['receipt', 'payload', 'state', 'next_attempt_at', 'updated_at'])
                    return answer
        # An orphan run cannot invent a goal or fresh repository setup consent.
        from .channel_switch import switch_to_copy_ready
        return switch_to_copy_ready(run=run, reason='strategy_goal_unavailable', now=now)
    return {}


def apply(subject, decision, *, now=None):
    """Record a decision and perform its due action once per reserved attempt."""
    now = now or timezone.now()
    organization, result, request, operation = _parts(subject)
    if isinstance(subject, ContentFactoryRun):
        if _event_owner(subject):
            _fresh_owned_run(subject)
            return {'status': 'deferred', 'reason': 'execution_owned'}
        # Observe-only legacy reports cannot mutate execution or policy state;
        # avoid a per-row lock/query in the bounded historical batch report.
        if decision.mode != 'observe' and _fresh_owned_run(subject):
            return {'status': 'deferred', 'reason': 'execution_owned'}
    elif _event_owner(subject):
        return {'status': 'deferred', 'reason': 'execution_owned'}
    if organization is None:
        return {'status': 'refused', 'detail': 'Not available yet.'}
    if decision.mode == 'observe' or policy_mode(organization) != 'policy':
        # Legacy report state is private organisation metadata only. In
        # particular, no legacy run snapshot or workflow authority is changed.
        logger.info('self_heal_observed org=%s run=%s code=%s action=%s', organization.pk,
                    getattr(subject, 'run_id', None), _code(result), decision.action)
        return {'version': 1, 'mode': 'observe', 'code': _code(result), 'last_action': decision.action,
                'attempts': _count(mapping(result.get('self_heal_v1')).get('attempts'))}
    if not isinstance(subject, ContentFactoryRun):
        return _apply_saved_operation(subject, decision, now=now)
    with transaction.atomic():
        config = _locked_config(organization)
        current = ContentFactoryRun.objects.select_for_update(of=('self',)).select_related('organization').get(pk=subject.pk)
        from .website_run_authority import event_owned
        if event_owned(current):
            return {'status': 'deferred', 'reason': 'execution_owned'}
        if current.organization_id != organization.pk:
            return {'status': 'refused', 'detail': 'The startup changed.'}
        request = mapping(current.run_request)
        current.organization._state.fields_cache['content_config'] = config
        old = mapping(mapping(current.result).get('self_heal_v1'))
        pending = mapping(old.get('pending_action'))
        retransmit = (pending.get('state') in {'reserved', 'uncertain'} and pending.get('attempt') == decision.attempt
                      and pending.get('action') == decision.action
                      and (stamp(pending.get('lease_until')) or now) <= now)
        if _count(old.get('attempts')) >= decision.attempt and not retransmit:
            return old
        fresh = decide(current, now=now)
        if (fresh.action, fresh.attempt, fresh.cap) != (decision.action, decision.attempt, decision.cap):
            return old
        receipt = {**old, 'version': 1, 'mode': 'policy', 'code': _code(mapping(current.result)),
            'policy': mapping(registry_policy.effective_entry(_code(mapping(current.result)), organization)).get('retry_policy'),
            'attempts': _count(old.get('attempts')), 'first_failed_at': old.get('first_failed_at') or now.isoformat(),
            'next_at': decision.at.isoformat() if decision.at else None, 'capped': bool(decision.cap),
            'rung': _rung(mapping(current.result), request),
            'source_sha': old.get('source_sha') or request.get('expected_source_sha'), 'history': list(old.get('history') or [])[-10:]}
        if decision.at and decision.at > now:
            _write_locked_run(current, receipt)
            return receipt
        if decision.action in {'observe', 'await_owner', 'none', 'wait'}:
            receipt.update(last_action=decision.action, next_at=(now + timedelta(hours=1)).isoformat()
                           if decision.action == 'observe' and decision.reason == 'waiting' else None)
            if decision.action == 'await_owner':
                receipt['next_at'] = ((stamp(receipt['first_failed_at']) or now)
                                      + timedelta(hours=registry_policy.limits()['wall_hours'])).isoformat()
            _write_locked_run(current, receipt)
            return receipt
        # Fresh limits are read while holding the same org row as every writer.
        if decision.action == 'advance_rung' and not retransmit and not request.get('operation_id') and not request.get('first_article_goal_operation_id'):
            day = _daily(config, now)
            key = f"{receipt['rung'] or 'native_collection'}:{(receipt['source_sha'] or '')[:12]}"
            day['strategy_attempts'] = _count(day.get('strategy_attempts')) + 1
            day['rung_attempts'][key] = _count(day['rung_attempts'].get(key)) + 1
            _save_daily(config, day)
        receipt.update(attempts=decision.attempt, last_action=decision.action, last_action_at=now.isoformat(),
                       next_at=(now + timedelta(minutes=registry_policy.limits()['backoff_minutes'][min(decision.attempt, 6)])).isoformat()
                       if decision.action in ACTIONS and not decision.cap else None)
        if not retransmit:
            receipt['history'] = (receipt['history'] + [{'at': now.isoformat(), 'action': decision.action,
                                                     'code': receipt['code'], 'attempt': decision.attempt}])[-10:]
        if decision.action in ACTIONS:
            lease_claim = uuid4().hex
            receipt['pending_action'] = {'state': 'reserved', 'claim': lease_claim, 'action': decision.action, 'attempt': decision.attempt,
                                         'cap': decision.cap, 'reason': decision.reason,
                                         'lease_until': (now + timedelta(minutes=2)).isoformat()}
        _write_locked_run(current, receipt)
    # A setup resume may create a PR. Owner-click continuations bypass this
    # reservation in their own services, but are still counted on ingest.
    explicit_publish = mapping(mapping(current.result).get('article_approval_v1')).get('source') == 'publish_reviewed'
    if (not explicit_publish and (current.workflow in {'article_system_setup', 'repo_scan'}
            or mapping(current.run_request).get('website_connection_id'))
            and decision.action in {'retry', 'resume', 're_pin', 'rescan', 'advance_rung'}):
        if not claim_pull_request_slot(organization, key=f'{current.run_id}:{decision.attempt}', now=now):
            with transaction.atomic():
                locked = ContentFactoryRun.objects.select_for_update().get(pk=current.pk)
                receipt = mapping(mapping(locked.result).get('self_heal_v1'))
                receipt.update(code='pull_request_daily_cap', next_at=(now.astimezone(dt_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat(), capped=False)
                receipt['attempts'] = max(0, decision.attempt - 1)
                receipt['history'] = [row for row in receipt.get('history', []) if row.get('attempt') != decision.attempt]
                receipt.pop('pending_action', None)
                _write_locked_run(locked, receipt)
            return receipt
    try:
        outcome = _execute_run(current, decision, now=now)
        uncertain = isinstance(outcome, dict) and (outcome.get('content_factory_transport_error')
            or outcome.get('dispatch_state') == 'pending'
            or outcome.get('content_factory_status_code', 200) >= 400 or outcome.get('status') in {'action_pending', 'blocked', 'refused'})
    except Exception:
        uncertain = True
        logger.warning('self_heal_action_pending run_id=%s action=%s', current.run_id, decision.action, exc_info=True)
    if decision.action in ACTIONS:
        with transaction.atomic():
            locked = ContentFactoryRun.objects.select_for_update().get(pk=current.pk)
            receipt = mapping(mapping(locked.result).get('self_heal_v1'))
            pending = mapping(receipt.get('pending_action'))
            if pending.get('attempt') == decision.attempt and pending.get('claim') == lease_claim:
                receipt['pending_action'] = {**pending, 'state': 'uncertain' if uncertain else 'accepted'}
                _write_locked_run(locked, receipt)
    return receipt


def _apply_operation(subject, decision, *, now):
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    from .website_run_authority import owned_goal_child
    operation = subject.get('_operation')
    if operation is None:
        return {'status': 'refused', 'detail': 'Not available yet.'}
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=operation.connection.organization_id)
        current = WebsiteConnectionOperation.objects.select_for_update().select_related('connection__organization').get(pk=operation.pk)
        if owned_goal_child(current) is not None or owned_goal_child(operation) is not None:
            return {'status': 'deferred', 'reason': 'execution_owned'}
        return _apply_unowned_operation(subject, decision, now=now)


def _apply_unowned_operation(subject, decision, *, now):
    operation = subject.get('_operation')
    if operation is None:
        return {'status': 'refused', 'detail': 'Not available yet.'}
    old = mapping(operation.receipt.get('self_heal_v1'))
    if _count(old.get('attempts')) >= decision.attempt:
        return old
    receipt = {**old, 'version': 1, 'mode': 'policy', 'code': _code(subject),
               'attempts': _count(old.get('attempts')), 'first_failed_at': old.get('first_failed_at') or now.isoformat(),
               'next_at': decision.at.isoformat() if decision.at else None, 'capped': bool(decision.cap)}
    operation.receipt['self_heal_v1'] = receipt
    if decision.at and decision.at > now:
        operation.state, operation.next_attempt_at = 'pending', decision.at
        return receipt
    if decision.action in {'retry', 'resume', 'rescan', 'advance_rung'}:
        organization = operation.connection.organization
        if not claim_pull_request_slot(organization, key=f'{operation.pk}:{decision.attempt}', now=now):
            tomorrow = now.astimezone(dt_timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            receipt.update(code='pull_request_daily_cap', next_at=tomorrow.isoformat(), capped=False)
            operation.state, operation.next_attempt_at = 'pending', tomorrow
            operation.receipt['self_heal_v1'] = receipt
            return receipt
        from .website_goal_runner import retry_goal_action
        goal = mapping(operation.receipt.get('goal'))
        if decision.action == 'advance_rung':
            organization = operation.connection.organization
            config = _locked_config(organization)
            day = _daily(config, now)
            rung = mapping(goal.get('strategy_pin')).get('rung') or 'native_collection'
            source = goal.get('current_source_sha') or ''
            key = f'{rung}:{source[:12]}'
            limits = registry_policy.limits()
            if (_count(day.get('strategy_attempts')) >= limits['strategy_max_attempts_per_org_per_day']
                    or _count(day['rung_attempts'].get(key)) >= limits['strategy_max_attempts_per_rung_per_source']):
                from .website_goal_runner import wait_for_channel
                wait_for_channel(operation, goal)
                receipt.update(capped=True, last_action='switch_channel', next_at=None)
                operation.receipt['self_heal_v1'] = receipt
                return receipt
            day['strategy_attempts'] = _count(day.get('strategy_attempts')) + 1
            day['rung_attempts'][key] = _count(day['rung_attempts'].get(key)) + 1
            _save_daily(config, day)
            from .website_strategy import advance
            goal = advance(goal, source=goal.get('current_source_sha'), now=now, reason='strategy_exhausted', organization=operation.connection.organization)
            retry_goal_action(operation, goal, now=now, occurrence=f'policy:{decision.attempt}', step='setup')
        else:
            retry_goal_action(operation, goal, now=now, occurrence=f'policy:{decision.attempt}', step='scan' if decision.action == 'rescan' else None)
        receipt.update(attempts=decision.attempt, last_action=decision.action, last_action_at=now.isoformat())
        receipt['last_failure_occurrence'] = subject.get('failure_occurrence_id')
    elif decision.action == 'switch_channel':
        from .website_goal_runner import wait_for_channel
        wait_for_channel(operation, mapping(operation.receipt.get('goal')))
        receipt.update(attempts=decision.attempt, last_action=decision.action, last_action_at=now.isoformat())
    elif decision.action == 'await_owner':
        operation.state, operation.next_attempt_at = 'needs_user', None
    elif decision.action == 'stop':
        operation.state, operation.next_attempt_at = 'needs_attention', None
        operation.receipt.update(status='stopped', user_action=None)
        receipt.update(attempts=decision.attempt, last_action='stop', last_action_at=now.isoformat())
        _alert(operation.connection.organization, 'self_heal_capped' if decision.cap else 'our_team_item', slot=f'operation-{operation.pk}')
    operation.receipt['self_heal_v1'] = receipt
    return receipt


def _apply_saved_operation(subject, decision, *, now):
    """Use the existing website authority guard before changing an owning goal."""
    from .website_connections import authority_guard, contract_for
    from .website_models import WebsiteConnectionOperation
    from . import website_goal as goals
    original = subject.get('_operation')
    if original is None:
        return {'status': 'refused', 'detail': 'Not available yet.'}
    from .website_run_authority import owned_goal_child
    if owned_goal_child(original) is not None:
        return {'status': 'deferred', 'reason': 'execution_owned'}
    with transaction.atomic():
        _locked_config(original.connection.organization)
        with authority_guard(contract_for(original.connection), action='read'):
            current = WebsiteConnectionOperation.objects.select_for_update().select_related('connection__organization').get(pk=original.pk)
            if (current.state in {'cancelled', 'denied', 'completed'} or current.generation != current.connection.generation
                    or current.updated_at != original.updated_at
                    or not goals.valid_goal(mapping(current.receipt).get('goal'), goals.scope_for(current.connection, current.payload.get('company_id')))):
                return {'status': 'refused', 'detail': 'The saved website goal changed.'}
            if owned_goal_child(current) is not None:
                return {'status': 'deferred', 'reason': 'execution_owned'}
            controller = subject.get('_controller') is True
            if current.state == 'running' and not (controller and current.updated_at == getattr(original, '_claimed_at', None)):
                return {'status': 'refused', 'detail': 'The website controller owns this attempt.'}
            if controller:
                # finish_goal holds the controller's current working observation;
                # its existing lease CAS remains the only operation-state writer.
                current.receipt, current.payload = original.receipt, original.payload
                current.state, current.next_attempt_at = original.state, original.next_attempt_at
            value = {**current.receipt, '_operation': current, '_organization': current.connection.organization,
                     'run_request': current.payload}
            fresh = decide(value, now=now)
            if (fresh.action, fresh.attempt, fresh.cap) != (decision.action, decision.attempt, decision.cap):
                return mapping(current.receipt.get('self_heal_v1'))
            receipt = _apply_operation(value, decision, now=now)
            if not controller:
                current.save(update_fields=['receipt', 'payload', 'state', 'next_attempt_at', 'updated_at'])
            original.receipt, original.payload = current.receipt, current.payload
            original.state, original.next_attempt_at = current.state, current.next_attempt_at
            if not controller:
                original.updated_at = current.updated_at
            return receipt


def _candidates(now, limit, organization_id=None):
    from .founder_journey_cohort import launched_at, _disabled_entries, v2_enabled
    preferred = Q(organization_id__in=p2_foundation.legacy_opt_in_ids())
    if v2_enabled():
        new = Q(organization__content_config__pillar_strategy__founder_journey__version=1,
                organization__content_config__pillar_strategy__founder_journey__cohort='new_journey')
        launch = launched_at()
        if launch:
            new |= Q(organization__created_at__gte=launch,
                     organization__content_config__pillar_strategy__founder_journey__isnull=True)
        disabled = _disabled_entries()
        if disabled:
            new &= ~Q(organization_id__in=[int(value) for value in disabled if value.isdecimal()])
            new &= ~Q(organization__domain__in=list(disabled))
        preferred |= new
    from .run_reconciliation import legacy_event_rows
    query = ContentFactoryRun.objects.filter(legacy_event_rows(), workflow__in=WORKFLOWS, organization__isnull=False,
        status__in=['failed', 'blocked', 'precondition_failed', 'preview_failed']).select_related('organization__content_config')
    if organization_id is not None:
        query = query.filter(organization_id=organization_id)
    return list(query
        .filter(Q(result__self_heal_v1__next_at__isnull=True) | Q(result__self_heal_v1__next_at__lte=now.isoformat()))
        .filter(Q(result__self_heal_v1__last_action__isnull=True) | ~Q(result__self_heal_v1__last_action__in=['stop', 'none']))
        .annotate(_policy_priority=Case(When(preferred, then=Value(0)), default=Value(1), output_field=IntegerField()))
        .order_by('_policy_priority', 'updated_at', 'pk')[:limit])


def run_policy_sweep(now=None, limit=None):
    """Read a bounded candidate batch and apply due decisions independently."""
    now = now or timezone.now()
    limit = max(1, min(int(limit or getattr(settings, 'ARTICLES_SELF_HEAL_SWEEP_LIMIT', 50)), 500))
    rows = _candidates(now, limit)
    summary = {'checked': len(rows), 'applied': 0, 'observed': 0, 'errors': 0}
    for run in rows:
        try:
            decision = decide(run, now=now)
            apply(run, decision, now=now)
            summary['applied' if decision.mode == 'policy' else 'observed'] += 1
        except Exception:
            summary['errors'] += 1
            logger.warning('self_heal_candidate_failed run_id=%s', run.run_id, exc_info=True)
    from .website_models import WebsiteConnectionOperation
    operations = list(WebsiteConnectionOperation.objects.filter(action='prepare', state__in=['pending', 'needs_attention', 'needs_user'],
        receipt__failure_occurrence_id__isnull=False).filter(Q(receipt__self_heal_v1__next_at__isnull=True)
            | Q(receipt__self_heal_v1__next_at__lte=now.isoformat())).select_related('connection__organization__content_config').order_by('updated_at')[:limit])
    summary['goals_checked'] = len(operations)
    for operation in operations:
        try:
            value = {**operation.receipt, '_operation': operation, '_organization': operation.connection.organization,
                     'run_request': operation.payload}
            apply(value, decide(value, now=now), now=now)
        except Exception:
            summary['errors'] += 1
            logger.warning('self_heal_goal_failed operation_id=%s', operation.pk, exc_info=True)
    return summary


def dry_run_report(*, organization_id=None, now=None, limit=500):
    """Compute a read-only report using the P2 registry view for every candidate."""
    now = now or timezone.now()
    rows = _candidates(now, limit, organization_id=organization_id)
    report = []
    for run in rows:
        config = _config(run.organization)
        if registry_policy.cohort_for_report(run.organization, config):
            continue
        with registry_policy.organization_read_scope(run.organization, config):
            decision = _decide(run, now=now, prospective=True)
        report.append({'organization_id': run.organization_id, 'run_id': run.run_id,
                       'code': _code(mapping(run.result)), 'action': decision.action, 'cap': decision.cap})
    return report


def run_dry_run_report_sweep(now=None, limit=None):
    """Log the weekly legacy report and retain only private org report metadata."""
    if not getattr(settings, 'ARTICLES_SELF_HEAL_REPORT_ENABLED', False):
        return {'status': 'disabled'}
    now = now or timezone.now()
    key = f'self_heal:dry_run:week:{now.isocalendar().year}-{now.isocalendar().week}'
    if not cache.add(key, True, timeout=7 * 86400):
        return {'status': 'already_reported'}
    report = dry_run_report(now=now, limit=limit or 500)
    for row in report:
        logger.info('self_heal_dry_run org=%s run=%s code=%s action=%s cap=%s', row['organization_id'], row['run_id'], row['code'], row['action'], row['cap'])
    for organization_id in {row['organization_id'] for row in report}:
        from organizations.models import Organization
        organization = Organization.objects.get(pk=organization_id)
        with transaction.atomic():
            config = _locked_config(organization)
            state = {**mapping(mapping(config.pillar_strategy).get('self_heal')),
                     'dry_run': {'at': now.isoformat(), 'orgs': len({row['organization_id'] for row in report}),
                                 'actions': sum(row['organization_id'] == organization_id for row in report)}}
            _save_daily(config, state)
    return {'status': 'completed', 'actions': len(report)}
