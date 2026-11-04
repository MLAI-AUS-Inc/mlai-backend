"""Preserve a draft when website delivery has exhausted its safe recovery paths."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import registry_policy
from .models import OrganizationContentConfig
from .website_goal import mapping, stamp


def _enabled(organization):
    from .failure_policy_engine import policy_mode
    return bool(getattr(settings, 'ARTICLES_CHANNEL_SWITCH_ENABLED', False)
                and policy_mode(organization) == 'policy')


def _hosted(run):
    """Retired hosting never intercepts copy-ready delivery."""
    return None


def switch_to_copy_ready(*, run, reason, now=None):
    """Switch the same run's delivery without replacing its consent or billing."""
    def allowed(current):
        if _enabled(current.organization):
            return True
        if reason != "hosting_retired":
            return False
        from .hosted_article_models import HostedArticlePublication
        from django.db.models import Q
        return HostedArticlePublication.objects.filter(site__organization=current.organization).filter(
            Q(run_id=current.run_id) | Q(written_article__source_run_id=current.run_id)).exists()
    if not allowed(run):
        return {'status': 'refused', 'detail': 'Not available yet.'}
    now = now or timezone.now()
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update(of=('self',)).select_related('organization').get(pk=run.pk)
        if not allowed(current) or current.status in {'cancelled', 'denied'}:
            return {'status': 'refused', 'detail': 'This article cannot change delivery.'}
        old = mapping(mapping(current.result).get('channel_v1'))
        if old.get('version') == 1 and old.get('dispatch_state') == 'accepted':
            return old
        if old.get('dispatch_state') == 'pending' and (stamp(old.get('lease_until')) or now) > now:
            return old
        receipt = old if old.get('version') == 1 else {'version': 1, 'channel': 'copy_ready', 'reason': str(reason)[:100],
                   'waiting_to_publish': True, 'at': now.isoformat(), 'dispatch_state': 'pending'}
        claim = uuid4().hex
        receipt = {**receipt, 'claim': claim, 'lease_until': (now + timedelta(minutes=2)).isoformat(), 'dispatch_state': 'pending'}
        ContentFactoryRun.objects.filter(pk=current.pk).update(result={**mapping(current.result), 'channel_v1': receipt})
    # CF receives a delivery intent, never a rewritten portable authority tuple.
    # It retains saved body/spec and only replaces remaining delivery steps.
    from .founder_draft_views import is_delivered_content_only
    if not is_delivered_content_only(current):
        from .vibe_marketing_views import _call_content_factory_run_action
        response = _call_content_factory_run_action(run_id=current.run_id, workflow=current.workflow, action='resume',
            payload={'channel_switch': {'delivery_mode': 'content_only', 'key': f'{current.run_id}:copy-ready'}},
            transport_errors_are_pending=True, preserve_run_clock=True)
        acknowledgement = mapping(mapping(response).get('channel_switch'))
        if (type(acknowledgement.get('version')) is int and acknowledgement['version'] == 1
                and acknowledgement.get('run_id') == current.run_id
                and acknowledgement.get('key') == f'{current.run_id}:copy-ready'
                and acknowledgement.get('state') in {'applied', 'completed'}
                and acknowledgement.get('delivery_mode') == 'content_only'):
            receipt['dispatch_state'] = 'accepted'
    else:
        receipt['dispatch_state'] = 'accepted'
    with transaction.atomic():
        locked = ContentFactoryRun.objects.select_for_update().get(pk=current.pk)
        latest = mapping(mapping(locked.result).get('channel_v1'))
        if latest.get('claim') != claim:
            return latest
        ContentFactoryRun.objects.filter(pk=current.pk).update(result={**mapping(locked.result), 'channel_v1': receipt})
    return receipt


def article_start_delivery_mode(*, organization, request):
    """Start draft-only when a P2 setup strategy would otherwise stop writing."""
    if not _enabled(organization):
        return None
    request = mapping(request)
    code = request.get('error_code') or request.get('code') or mapping(request.get('failure')).get('code')
    if code == 'ARTICLE_SYSTEM_SETUP_REQUIRED' or registry_policy.failure_class(code, organization) == 'strategy':
        return 'content_only'
    return None


def ladder_exhausted(*, organization, connection=None, fingerprint=None, now=None):
    """Record one source-scoped exhausted ladder and an actionable ops ticket."""
    if not _enabled(organization):
        return {'status': 'refused', 'detail': 'Not available yet.'}
    from organizations.models import Organization
    from .founder_journey_alerts import queue_founder_journey_alert
    now = now or timezone.now()
    source = getattr(connection, 'verified_sha', None) or ''
    facts = mapping(fingerprint)
    safe = {key: facts[key] for key in ('framework', 'host', 'failing_checks') if key in facts}
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=organization)
        config = OrganizationContentConfig.objects.select_for_update().get(pk=config.pk)
        strategy = mapping(config.pillar_strategy)
        heal = mapping(strategy.get('self_heal'))
        old = mapping(heal.get('ladder'))
        receipt = old if old.get('source_sha') == source and old.get('exhausted_at') else {
            'exhausted_at': now.isoformat(), 'source_sha': source, 'fingerprint': safe}
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**strategy,
            'self_heal': {**heal, 'ladder': receipt}})
    queue_founder_journey_alert(organization=organization, alert_reason='ladder_exhausted', slot=f'source-{source}',
        details={'source_sha': source, **safe})
    return receipt
