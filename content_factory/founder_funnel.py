"""Read-only founder conversion facts and privacy-safe journey event emission.

The signup window selects a cohort, as in the original P0 report. Conversion
facts may arrive later. Timings use source timestamps, never a mutable run
clock. Missing historic timestamps remain unknown rather than becoming zero.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from community_chat.onboarding_models import CommunityMemberProfile
from founder_tools.models import VibeRaisingCompany
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from .models import OrganizationContentConfig, WrittenArticle
from .run_state import ARTICLE_WORKFLOWS
from .website_models import WebsiteConnection

logger = logging.getLogger(__name__)
STAGES = ('signed_up', 'joined', 'intent', 'startup_created', 'research_done',
          'first_article_started', 'queued', 'draft_ready', 'copied', 'published', 'github_upgraded')
RESEARCH_WORKFLOWS = ('startup_autofill', 'website_scan', 'repo_scan')
MAX_COHORT = 20_000


def timestamp(value):
    """Accept an aware source timestamp; reject absent, malformed and naive values."""
    if isinstance(value, str):
        try:
            value = parse_datetime(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, datetime) and timezone.is_aware(value) else None


def mapping(value):
    """Treat absent or malformed optional JSON as an empty object."""
    return value if isinstance(value, dict) else {}


def has_first_draft(organization):
    """Read monotonic delivery evidence, including history after normal draft deletion."""
    if not getattr(organization, 'pk', None):
        return False
    strategy = OrganizationContentConfig.objects.filter(organization=organization).values_list('pillar_strategy', flat=True).first()
    receipt = mapping(mapping(mapping(strategy).get('founder_funnel')).get('first_draft'))
    if type(receipt.get('version')) is int and receipt['version'] == 1 and receipt.get('runId'):
        return True
    from .founder_journey_alerts import organization_has_delivered_draft
    if organization_has_delivered_draft(organization.pk):
        return True
    runs = ContentFactoryRun.objects.filter(organization=organization, workflow__in=ARTICLE_WORKFLOWS).exclude(status__in=['cancelled', 'denied'])
    if runs.filter(steps__step_key='package_content_delivery', steps__status='completed', steps__completed_at__isnull=False).exists():
        return True
    return any(draft_time(run) for run in runs.only('status', 'result'))


MAX_DRAFT_PROOFS = 64


def _add_draft_proof(proofs, run_id, *, at=None, created_at=None):
    """Join clocks only for the same source run; never date one run from another."""
    if not isinstance(run_id, str) or not 0 < len(run_id) <= 100:
        return
    old = proofs.get(run_id, {})
    known = [value for value in (timestamp(old.get('at')), timestamp(at)) if value is not None]
    bounds = [value for value in (timestamp(old.get('createdAt')), timestamp(created_at)) if value is not None]
    proofs[run_id] = {'at': min(known).isoformat() if known else None,
                     'createdAt': min(bounds).isoformat() if bounds else None}


def _marker_draft_proofs(marker, proofs):
    if type(marker.get('version')) is not int or marker['version'] != 1:
        return
    rows = mapping(marker.get('proofs'))
    if rows:
        for run_id, row in rows.items():
            _add_draft_proof(proofs, run_id, at=mapping(row).get('at'), created_at=mapping(row).get('createdAt'))
    elif type(marker.get('version')) is int and marker['version'] == 1:
        # An older deleted source without a clock or lower bound stays unknown.
        _add_draft_proof(proofs, marker.get('runId'), at=marker.get('at'), created_at=marker.get('createdAt'))


def _first_draft_marker(proofs, *, overflow=False):
    """An exact earliest clock needs proof no unknown delivery could precede it."""
    known = [(timestamp(row.get('at')), run_id) for run_id, row in proofs.items() if timestamp(row.get('at'))]
    earliest, known_id = min(known) if known else (None, None)
    unknown = {run_id: row for run_id, row in proofs.items() if not timestamp(row.get('at'))
        and (earliest is None or timestamp(row.get('createdAt')) is None or timestamp(row.get('createdAt')) < earliest)}
    # Later unknown deliveries cannot precede an already proved earliest clock.
    # Retain competing unknown sources across deletion; a same-run proof may
    # resolve one while another still makes the first clock unknowable.
    retained = dict(unknown)
    if known_id:
        retained[known_id] = proofs[known_id]
    if len(retained) > MAX_DRAFT_PROOFS:
        overflow = True
        ids = sorted(unknown, key=lambda key: (timestamp(unknown[key].get('createdAt')) is not None,
            timestamp(unknown[key].get('createdAt')), key))
        retained = {key: unknown[key] for key in ids[:MAX_DRAFT_PROOFS-int(known_id is not None)]}
        if known_id:
            retained[known_id] = proofs[known_id]
    primary = next(iter(unknown), known_id)
    if primary is None:
        return {}
    return {'version': 1, 'runId': primary,
        'at': earliest.isoformat() if earliest and not unknown and not overflow else None,
        'createdAt': proofs[primary].get('createdAt'), 'proofs': retained, 'unknownOverflow': bool(overflow)}


def _organization_draft_proofs(organization_id, *, using):
    """Read delivered source facts once when initializing historical bookkeeping."""
    runs = list(ContentFactoryRun.objects.using(using).filter(organization_id=organization_id,
        workflow__in=ARTICLE_WORKFLOWS).exclude(status__in=['cancelled', 'denied']).only(
            'pk', 'run_id', 'created_at', 'status', 'result'))
    by_id = {run.pk: run for run in runs}
    proofs = {}
    for run in runs:
        at = draft_time(run)
        if at or run.status in {'completed', 'awaiting_approval'}:
            _add_draft_proof(proofs, run.run_id, at=at, created_at=run.created_at)
    for step in ContentFactoryRunStep.objects.using(using).filter(run_id__in=by_id, step_key='package_content_delivery',
            status='completed', completed_at__isnull=False).values('run_id', 'completed_at'):
        run = by_id[step['run_id']]
        _add_draft_proof(proofs, run.run_id, at=step['completed_at'], created_at=run.created_at)
    return proofs


def remember_first_draft(organization_id, *, run_id, at=None, using=DEFAULT_DB_ALIAS):
    """Retain delivered eligibility and exact/unknown timing without clock writes."""
    from django.db import transaction
    from organizations.models import Organization
    with transaction.atomic(using=using):
        organization = Organization.objects.using(using).select_for_update().filter(pk=organization_id).first()
        if organization is None:
            return
        config = OrganizationContentConfig.objects.using(using).select_for_update().filter(organization_id=organization_id).first()
        from .founder_journey_cohort import is_new_journey
        if not is_new_journey(organization, config):
            return
        if config is None:
            config = OrganizationContentConfig.objects.using(using).create(organization_id=organization_id)
        strategy = dict(mapping(config.pillar_strategy))
        funnel = dict(mapping(strategy.get('founder_funnel')))
        previous = mapping(funnel.get('first_draft'))
        proofs = _organization_draft_proofs(organization_id, using=using) if 'proofs' not in previous else {}
        _marker_draft_proofs(previous, proofs)
        created = ContentFactoryRun.objects.using(using).filter(organization_id=organization_id,
            run_id=run_id).values_list('created_at', flat=True).first()
        _add_draft_proof(proofs, run_id, at=at, created_at=created)
        receipt = _first_draft_marker(proofs, overflow=previous.get('unknownOverflow') is True)
        if not receipt or receipt == previous:
            return
        funnel['first_draft'] = receipt
        strategy['founder_funnel'] = funnel
        OrganizationContentConfig.objects.using(using).filter(pk=config.pk).update(pillar_strategy=strategy)


def draft_time(run):
    """The writer's immutable readiness receipt, with a known revision."""
    receipt = mapping(mapping(run.result).get('draft_ready_v1'))
    if (run.status not in {'cancelled', 'denied'} and type(receipt.get('version')) is int
            and receipt['version'] == 1 and isinstance(receipt.get('revision'), str)
            and 0 < len(receipt['revision']) <= 256):
        return timestamp(receipt.get('at'))
    return None


def emit_event(kind, *, user_id=None, organization_id=None, run_id=None, at=None, signed_up_at=None, source):
    """Emit one deduplicable event after commit; never log email, URLs or body text.

    Replays have the same event id. Consumers count distinct event ids, while
    funnel conversion counts are reconstructed from the durable source rows.
    """
    if kind not in STAGES:
        raise ValueError('Unknown founder journey stage.')
    if not getattr(settings, 'MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED', False):
        return
    moment = timestamp(at) or timezone.now()
    signup = timestamp(signed_up_at)
    identity = json.dumps([kind, user_id, organization_id, run_id], separators=(',', ':'))
    fields = {'version': 1, 'event': kind, 'event_id': hashlib.sha256(identity.encode()).hexdigest(),
              'user_id': user_id, 'organization_id': organization_id, 'run_id': run_id,
              'at': moment.isoformat(), 'source': source,
              'seconds_since_signup': max(0, (moment-signup).total_seconds()) if signup and moment >= signup else None}
    logger.info('founder_journey_event %s', json.dumps(fields, sort_keys=True, separators=(',', ':')),
                extra={'founder_journey_event': fields})


def journey_funnel(*, since, until, using=DEFAULT_DB_ALIAS):
    """Aggregate the P0 signup cohort without writes, network access or inferred clocks."""
    users = list(get_user_model().objects.using(using).filter(
        date_joined__gte=since, date_joined__lt=until, is_staff=False).values('pk', 'date_joined')[:MAX_COHORT+1])
    if len(users) > MAX_COHORT:
        raise ValueError('Choose a smaller signup window (maximum 20,000 accounts).')
    facts = {row['pk']: {} for row in users}
    signups = {row['pk']: row['date_joined'] for row in users}

    def mark(user_id, stage, at=None):
        if user_id not in facts:
            return
        at = timestamp(at)
        current = facts[user_id].get(stage)
        # Presence and a timestamp are separate facts; a later known timestamp
        # may fill a gap but must not erase an earlier, proved timestamp.
        if stage not in facts[user_id] or (at is not None and (current is None or at < current)):
            facts[user_id][stage] = at

    for user_id, at in signups.items():
        mark(user_id, 'signed_up', at)
    for profile in CommunityMemberProfile.objects.using(using).filter(user_id__in=facts).values(
            'user_id', 'submitted_at', 'interests'):
        if profile['submitted_at']:
            mark(profile['user_id'], 'joined', profile['submitted_at'])
            if 'startups' in (profile['interests'] if isinstance(profile['interests'], list) else []):
                # Interests can change after joining. Its original selection time
                # is available in new event logs, not historic profile rows.
                mark(profile['user_id'], 'intent')
    owners = defaultdict(set)
    for row in VibeRaisingCompany.objects.using(using).filter(profile__user_id__in=facts).values(
            'profile__user_id', 'organization_id', 'created_at'):
        mark(row['profile__user_id'], 'startup_created', row['created_at'])
        if row['organization_id']:
            owners[row['organization_id']].add(row['profile__user_id'])

    def org_mark(org_id, stage, at=None):
        for user_id in owners.get(org_id, ()):
            mark(user_id, stage, at)

    runs = list(ContentFactoryRun.objects.using(using).filter(organization_id__in=owners,
        workflow__in=tuple(ARTICLE_WORKFLOWS)+RESEARCH_WORKFLOWS).only(
        'pk', 'organization_id', 'run_id', 'workflow', 'created_at', 'result', 'status'))
    by_id = {run.pk: run for run in runs}
    draft_proofs = defaultdict(dict)
    marker_overflow = {}
    for run in runs:
        if run.workflow in ARTICLE_WORKFLOWS:
            org_mark(run.organization_id, 'first_article_started', run.created_at)
            org_mark(run.organization_id, 'queued', run.created_at)
            ready = draft_time(run)
            if ready or run.status in {'completed', 'awaiting_approval'}:
                _add_draft_proof(draft_proofs[run.organization_id], run.run_id, at=ready, created_at=run.created_at)
        elif run.status == 'completed':
            org_mark(run.organization_id, 'research_done', timestamp(mapping(run.result).get('completed_at')))
    for step in ContentFactoryRunStep.objects.using(using).filter(run_id__in=by_id,
            status='completed', completed_at__isnull=False).values('run_id', 'step_key', 'completed_at'):
        run = by_id[step['run_id']]
        if run.workflow in ARTICLE_WORKFLOWS and run.status not in {'cancelled', 'denied'}:
            if step['step_key'] == 'package_content_delivery':
                _add_draft_proof(draft_proofs[run.organization_id], run.run_id, at=step['completed_at'], created_at=run.created_at)
    research_times = defaultdict(list)
    for step in ContentFactoryRunStep.objects.using(using).filter(run_id__in=[r.pk for r in runs
            if r.workflow in RESEARCH_WORKFLOWS and r.status == 'completed'], status='completed',
            completed_at__isnull=False).values('run_id', 'completed_at'):
        research_times[step['run_id']].append(step['completed_at'])
    for run_pk, times in research_times.items():
        org_mark(by_id[run_pk].organization_id, 'research_done', max(times))
    for row in OrganizationContentConfig.objects.using(using).filter(organization_id__in=owners).values(
            'organization_id', 'pillar_strategy'):
        first_draft = mapping(mapping(mapping(row['pillar_strategy']).get('founder_funnel')).get('first_draft'))
        if type(first_draft.get('version')) is int and first_draft['version'] == 1 and first_draft.get('runId'):
            _marker_draft_proofs(first_draft, draft_proofs[row['organization_id']])
            marker_overflow[row['organization_id']] = first_draft.get('unknownOverflow') is True
        for receipt in mapping(mapping(row['pillar_strategy']).get('founder_posted_articles')).values():
            copied = timestamp(mapping(receipt).get('copiedAt'))
            if copied:
                org_mark(row['organization_id'], 'copied', copied)
    # The cohort counts people. An unknown earlier delivery at any startup
    # they founded can precede a known clock at another startup as well.
    user_proofs = defaultdict(dict)
    user_overflow = defaultdict(bool)
    for org_id, proofs in draft_proofs.items():
        for user_id in owners.get(org_id, ()):
            for run_id, proof in proofs.items():
                _add_draft_proof(user_proofs[user_id], run_id, at=proof.get('at'), created_at=proof.get('createdAt'))
            user_overflow[user_id] |= marker_overflow.get(org_id, False)
    for user_id, proofs in user_proofs.items():
        first = _first_draft_marker(proofs, overflow=user_overflow[user_id])
        if first:
            mark(user_id, 'draft_ready', first.get('at'))
    for row in WrittenArticle.objects.using(using).filter(organization_id__in=owners, publish_status='live').values(
            'organization_id', 'live_verified_at'):
        org_mark(row['organization_id'], 'published', row['live_verified_at'])
    for row in WebsiteConnection.objects.using(using).filter(organization_id__in=owners,
            state='connected', last_verified_at__isnull=False).exclude(github_repo='').values(
            'organization_id', 'last_verified_at'):
        org_mark(row['organization_id'], 'github_upgraded', row['last_verified_at'])
    durations = sorted((stages['draft_ready']-signups[user_id]).total_seconds()
        for user_id, stages in facts.items() if stages.get('draft_ready') is not None
        and stages['draft_ready'] >= signups[user_id])
    counts = {stage: {'users': sum(stage in stages for stages in facts.values()),
                     'timedUsers': sum(stages.get(stage) is not None for stages in facts.values())} for stage in STAGES}
    def percentile(fraction):
        return durations[max(0, math.ceil(len(durations)*fraction)-1)] if durations else None
    return {'version': 1, 'since': since.isoformat(), 'until': until.isoformat(),
        'observedAt': timezone.now().isoformat(), 'stages': counts,
        'timeToFirstDraft': {'p50Seconds': percentile(.5), 'p90Seconds': percentile(.9),
            'sampleSize': len(durations), 'unknownTimingUsers': counts['draft_ready']['users']-len(durations)},
        'methodology': {'cohort': 'Non-staff accounts signed up in [since, until); conversions observed through now.',
            'attribution': 'Startup facts are attributed to its recorded founder accounts; team members are not inferred.',
            'percentile': 'Nearest rank from proved readiness timestamps; mutable run updated_at is never used.',
            'unknowns': 'Historic intent selections and legacy draft/research completion clocks can be unknown. No workshop exclusion is inferred.'}}
