"""Bounded, free, post-draft website checks with durable daily dispatch identity.

Private receipts use the existing JSON store, not a new schedule/migration.
The original free per-account cap is reserved before remote publication. An
uncertain crash may conservatively retain a free slot; it never grants an extra
slot, changes a paid cap or dispatches a new key to escape uncertainty.
"""
from copy import deepcopy
from datetime import timedelta
import logging
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import Throttled

from organizations.models import Organization
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import FounderCompanyContext, organization_owner_user_id, normalize_company_domain
from workflow_runs.models import ContentFactoryRun
from .billing import free_setup_actions, check_free_setup_daily_cap, record_free_setup_use
from .founder_funnel import has_first_draft, mapping, timestamp
from .models import OrganizationContentConfig, WebsiteBaselineSnapshot
from .p2_foundation import p2_applies
from .founder_journey_cohort import v2_enabled

logger = logging.getLogger(__name__)
KEY = 'website_check_schedule_v1'
CURSOR = 'founder_website_checks:cursor:v1'
LEASE = timedelta(minutes=2)
MAX_SENDS = 3
NIGHTLY_HOUR = 2
ACTIVE = ('queued', 'running', 'pending', 'researching')


def _enabled():
    return bool(getattr(settings, 'MY_STARTUP_WEBSITE_CHECKS_BACKGROUND_ENABLED', False)
                and getattr(settings, 'MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED', False)
                and v2_enabled()
                and 'website_baseline' in free_setup_actions())


def _context(organization):
    owner_id = organization_owner_user_id(organization)
    company = VibeRaisingCompany.objects.select_related('profile__user').filter(
        organization=organization, profile__user_id=owner_id, profile__user__is_active=True).order_by('created_at').first()
    if company is None or not owner_id:
        return None
    domain = normalize_company_domain(organization.domain)
    if not domain or normalize_company_domain(company.domain) != domain:
        return None
    from founder_tools.my_startup.api import require_my_startup_access
    require_my_startup_access(company.profile.user)
    return FounderCompanyContext(profile=company.profile, company=company, organization=organization)


def _receipt(config):
    return mapping(mapping(config.pillar_strategy).get('self_heal')).get(KEY) or {}


def _save(config, receipt):
    strategy = deepcopy(mapping(config.pillar_strategy))
    heal = dict(mapping(strategy.get('self_heal')))
    heal[KEY] = receipt
    strategy['self_heal'] = heal
    OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=strategy)


def _scope_matches(receipt, context):
    return (receipt.get('owner_id') == context.profile.user_id and receipt.get('company_id') == str(context.company.pk)
            and receipt.get('domain') == context.organization.domain)


def _claim(config_id, now):
    from .vibe_marketing_views import _website_baseline_payload, _mark_roo_points_gate_authorized
    org_id = OrganizationContentConfig.objects.filter(pk=config_id).values_list('organization_id', flat=True).first()
    if org_id is None:
        return 'skipped', None
    with transaction.atomic():
        org = Organization.objects.select_for_update().filter(pk=org_id).first()
        config = OrganizationContentConfig.objects.select_for_update().filter(pk=config_id, organization=org).first()
        if org is None or config is None or not _enabled() or not p2_applies(org) or not has_first_draft(org):
            return 'skipped', None
        context = _context(org)
        if context is None:
            return 'skipped', None
        if ContentFactoryRun.objects.filter(organization=org, domain=org.domain,
                workflow='website_baseline', status__in=ACTIVE).exists():
            return 'waiting', None
        previous = mapping(_receipt(config))
        if previous.get('state') == 'pending':
            if not _scope_matches(previous, context):
                _save(config, {**previous, 'state': 'cancelled', 'reason': 'scope_changed'})
                return 'skipped', None
            if timestamp(previous.get('lease_until')) and timestamp(previous['lease_until']) > now:
                return 'waiting', None
            if int(previous.get('sends') or 0) >= MAX_SENDS:
                return 'held', None
            receipt = dict(previous)
        else:
            try:
                local = now.astimezone(ZoneInfo(config.default_timezone or 'Australia/Melbourne'))
            except (ZoneInfoNotFoundError, ValueError):
                local = now.astimezone(ZoneInfo('Australia/Melbourne'))
            day = local.date().isoformat()
            if previous.get('date') == day:
                return 'skipped', None
            snapshot = WebsiteBaselineSnapshot.objects.filter(organization=org, status='completed').order_by('-collected_at').first()
            if snapshot and (snapshot.collected_at.astimezone(local.tzinfo).date() >= local.date() or local.hour < NIGHTLY_HOUR):
                return 'skipped', None
            key = f'website-check:{org.pk}:{day}'
            payload = _website_baseline_payload(context=context, config=config, user=context.profile.user)
            payload['client_request_id'] = key
            _mark_roo_points_gate_authorized(payload, domain=org.domain, action='website_baseline', current_balance=None)
            receipt = {'version': 1, 'date': day, 'key': key, 'owner_id': context.profile.user_id,
                'company_id': str(context.company.pk), 'domain': org.domain, 'payload': payload,
                'usage_reserved': False, 'sends': 0}
        receipt.update(state='pending', nonce=uuid4().hex, lease_until=(now+LEASE).isoformat(), sends=receipt['sends']+1)
        _save(config, receipt)
        return 'claimed', (config.pk, context, receipt)


def _mutate(config_id, receipt, **changes):
    with transaction.atomic():
        org_id = OrganizationContentConfig.objects.filter(pk=config_id).values_list('organization_id', flat=True).first()
        if org_id is None:
            return False
        Organization.objects.select_for_update().get(pk=org_id)
        config = OrganizationContentConfig.objects.select_for_update().get(pk=config_id)
        current = mapping(_receipt(config))
        if current.get('key') != receipt['key'] or current.get('nonce') != receipt['nonce'] or current.get('state') != 'pending':
            return False
        _save(config, {**current, **changes})
        return True


def _dispatch(claim, now):
    from django.contrib.auth import get_user_model
    from .vibe_marketing_views import _start_website_baseline
    config_id, context, receipt = claim
    if not receipt.get('usage_reserved'):
        # Serializes background reservations across different startups owned by
        # this account. No remote call or organization lock is held here.
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().get(pk=context.profile.user_id, is_active=True)
            check_free_setup_daily_cap(user, 'website_baseline', now=now.timestamp())
            record_free_setup_use(user, 'website_baseline', now=now.timestamp())
        if not _mutate(config_id, receipt, usage_reserved=True):
            return 'waiting'
    # Fresh ownership, cohort and free eligibility immediately before dispatch.
    context.organization.refresh_from_db()
    current_context = _context(context.organization)
    if (not _enabled() or not p2_applies(context.organization) or current_context is None
            or not _scope_matches(receipt, current_context)):
        _mutate(config_id, receipt, state='cancelled', reason='scope_changed')
        return 'skipped'
    config = OrganizationContentConfig.objects.get(pk=config_id)
    run, error = _start_website_baseline(context=current_context, config=config, user=current_context.profile.user,
        payload=receipt['payload'], background_reserved=True)
    if error is not None or run is None:
        return 'waiting'
    _mutate(config_id, receipt, state='queued', run_id=run.run_id, lease_until=None)
    return 'queued'


def run_founder_website_check_sweep(*, now=None, limit=25):
    """Visit a bounded fair batch; disabled flags perform no database/provider reads."""
    result = {key: 0 for key in ('visited','queued','waiting','held','limited','skipped','failed')}
    if not _enabled():
        return result
    now = now or timezone.now()
    limit = max(1, min(int(limit), 100))
    cursor = cache.get(CURSOR, 0)
    cursor = cursor if type(cursor) is int and cursor >= 0 else 0
    ids = list(OrganizationContentConfig.objects.filter(pk__gt=cursor).order_by('pk').values_list('pk', flat=True)[:limit])
    if not ids and cursor:
        ids = list(OrganizationContentConfig.objects.order_by('pk').values_list('pk', flat=True)[:limit])
    if ids:
        cache.set(CURSOR, ids[-1], timeout=86400)
    for config_id in ids:
        result['visited'] += 1
        claim = None
        try:
            state, claim = _claim(config_id, now)
            state = _dispatch(claim, now) if claim else state
            result[state] += 1
        except Throttled:
            if claim:
                _mutate(claim[0], claim[2], state='limited', lease_until=None)
            result['limited'] += 1
        except Exception as exc:  # noqa: BLE001 - one optional check must not stop other founders
            result['failed'] += 1
            logger.warning('founder_website_check_failed config_id=%s error_type=%s', config_id, type(exc).__name__)
    return result
