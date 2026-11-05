"""Row-locked startup membership without creating owner-level company rows."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib
import logging
import secrets
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import User
from content_factory.models import OrganizationContentConfig
from content_factory.website_goal import mapping, stamp
from organizations.models import Organization
from .models import VibeRaisingCompany

logger = logging.getLogger(__name__)
KEY = 'startup_team'
NEEDS = ('read', 'write_articles', 'publish', 'spend_startup_points')
_member_context = ContextVar('startup_team_member_context', default=None)
ARTICLE_WORKFLOWS = frozenset({'article_generation', 'direct_generate', 'confirmed_topic', 'article_revision', 'component_revision'})


class TeamError(ValueError):
    """A closed requester-safe error that exposes no tenant or person identity."""
    def __init__(self, reason, detail, status=403):
        self.reason, self.detail, self.status = reason, detail, status
        super().__init__(detail)


def enabled():
    """The membership rollout is independent of domain auto-join."""
    return bool(getattr(settings, 'MY_STARTUP_TEAM_ENABLED', False))


def _valid_user(user):
    return bool(getattr(user, 'is_authenticated', False) and getattr(user, 'is_active', False))


def _verified(user):
    return _valid_user(user) and bool(user.email_verified_at and user.email and user.email.count('@') == 1)


def _config(organization):
    return OrganizationContentConfig.objects.filter(organization=organization).first()


def _roster(config):
    value = mapping(getattr(config, 'pillar_strategy', {})).get(KEY)
    return deepcopy(value) if isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 1 else None


def pinned_owner(organization):
    """Return (roster_exists, owner_id); a departed owner never falls through."""
    if organization is None or not getattr(organization, 'pk', None) or getattr(getattr(organization, '_state', None), 'adding', True):
        return False, None
    config = _config(organization)
    strategy = mapping(getattr(config, 'pillar_strategy', {}))
    if KEY not in strategy:
        return False, None
    roster = _roster(config)
    owner = roster.get('owner_user_id') if roster else None
    if type(owner) is not int or not VibeRaisingCompany.objects.filter(organization=organization, profile__user_id=owner, profile__user__is_active=True).exists():
        return True, None
    return True, owner


def _owner(organization):
    from .services import organization_owner_user_id
    return organization_owner_user_id(organization)


def _member(roster, user):
    return next((row for row in (roster or {}).get('members', []) if isinstance(row, dict)
                 and row.get('user_id') == user.pk and row.get('state') in {'active', 'unconfirmed'}), None)


@contextmanager
def _locked(organization, *, create=False):
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        config = OrganizationContentConfig.objects.select_for_update().filter(organization=organization).first()
        if config is None and create:
            config = OrganizationContentConfig.objects.create(organization=organization)
        roster = _roster(config)
        if roster is None and create:
            if KEY in mapping(getattr(config, 'pillar_strategy', {})):
                raise TeamError('not_available', 'The team needs an ownership review.', 409)
            owner = _owner(organization)
            if owner is None:
                raise TeamError('not_available', 'The team needs an ownership review.', 409)
            roster = {'version': 1, 'owner_user_id': owner, 'frozen': False, 'members': [],
                      'requests': [], 'invite': None, 'history': []}
            company = VibeRaisingCompany.objects.filter(organization=organization, profile__user_id=owner).order_by('created_at', 'pk').first()
            if company:
                roster['company'] = {'id': str(company.pk), 'name': company.name, 'domain': company.domain}
        yield config, roster


def _save(config, roster):
    OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**mapping(config.pillar_strategy), KEY: roster})


def _history(roster, event, actor, subject, now):
    roster['history'] = (list(roster.get('history') or []) + [{'at': now.isoformat(), 'event': event,
        'actor_user_id': getattr(actor, 'pk', actor), 'subject_user_id': getattr(subject, 'pk', subject)}])[-50:]


def _manage(user, organization, roster):
    if not _valid_user(user) or not roster or roster.get('frozen') or _owner(organization) != user.pk:
        raise TeamError('owner_only', 'Only the startup owner can do this.')


def _rights(user, organization, roster):
    member = _member(roster, user)
    if not _valid_user(user) or member is None:
        return frozenset()
    if roster.get('frozen') or _owner(organization) is None:
        return frozenset({'read'})
    rights = {'read', 'write_articles'}
    if member['state'] == 'active':
        if mapping(member.get('rights')).get('publish') is True:
            rights.add('publish')
        if mapping(member.get('rights')).get('billing') is True:
            rights.add('spend_startup_points')
    return frozenset(rights)


def viewer_role(user, organization):
    """Re-resolve current ownership or membership, without a permission cache."""
    if not _valid_user(user) or organization is None:
        return None
    with _locked(organization) as (_, roster):
        if _owner(organization) == user.pk:
            return 'owner'
        return 'member' if enabled() and _member(roster, user) else None


def member_may_publish(user, organization):
    """Owners publish; only currently active members with explicit rights may."""
    if not _valid_user(user):
        return False
    with _locked(organization) as (_, roster):
        if _owner(organization) == user.pk:
            return not roster or not roster.get('frozen')
        return enabled() and 'publish' in _rights(user, organization, roster)


def member_may_spend(user, organization):
    """A member uses startup billing only with a current explicit billing grant."""
    if not enabled() or not _valid_user(user):
        return False
    with _locked(organization) as (_, roster):
        return 'spend_startup_points' in _rights(user, organization, roster)


def member_may_use_run(user, run):
    """Allow member article access only within the run's actual saved tenant."""
    if not getattr(run, 'organization_id', None):
        return False
    role = viewer_role(user, run.organization)
    return role == 'owner' or (role == 'member' and run.workflow in ARTICLE_WORKFLOWS)


def member_context(user, company_id, *, need):
    """Build an explicit member context, never a founder company ownership row."""
    if not enabled() or need not in NEEDS or not _valid_user(user):
        return None
    company = VibeRaisingCompany.objects.select_related('organization', 'profile').filter(pk=company_id).first()
    if company is None:
        config = OrganizationContentConfig.objects.select_related('organization').filter(
            pillar_strategy__startup_team__company__id=str(company_id)).first()
        if config is None:
            return None
        # A departed owner's deleted company has only a read-only descriptor.
        # This unsaved instance never grants ownership or creates a company row.
        saved = mapping(mapping(config.pillar_strategy).get(KEY)).get('company')
        from .services import get_or_create_founder_profile
        company = VibeRaisingCompany(pk=company_id, name=saved.get('name', ''), domain=saved.get('domain', ''),
            organization=config.organization, profile=get_or_create_founder_profile(user))
    if company.organization_id is None:
        return None
    with _locked(company.organization) as (_, roster):
        rights = _rights(user, company.organization, roster)
        if need not in rights:
            return None
        from .services import FounderCompanyContext, get_or_create_founder_profile
        return FounderCompanyContext(profile=get_or_create_founder_profile(user), company=company,
            organization=company.organization, role='member', rights=rights)


def bind_member_context(context, *, need='read'):
    """Bind only a context already authorized by the exact route allowlist."""
    return _member_context.set((context, need))


def reset_member_context(token):
    """Reset in the request dispatch finally, including raised exceptions."""
    _member_context.reset(token)


def bound_member_context(user, company_id):
    """Return the request-local context only for the exact user/company pair."""
    binding = _member_context.get()
    if binding is None:
        return None
    context, need = binding
    if context.profile.user_id != user.pk or str(context.company.pk) != str(company_id):
        return None
    return member_context(user, company_id, need=need)


def notification_recipients(organization, *, item, ask=None):
    """Resolve one-recipient candidates; owner-only asks never reach members."""
    owner = _owner(organization)
    ids = {owner} if owner else set()
    if ask == 'approve_on_github' and isinstance(item, str) and item.startswith('article:'):
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.filter(run_id=item.split(':', 1)[1], organization=organization).first()
        if (run is None or run.workflow not in ARTICLE_WORKFLOWS
                or mapping(mapping(run.result).get('publish_pr_v1')).get('kind') in {'setup', 'scaffold', 'proxy'}):
            return list(User.objects.filter(pk__in=ids, is_active=True))
    if enabled() and isinstance(item, str) and item.startswith('article') and ask not in {
            'approve_setup', 'authorize_github', 'approve_join_request', 'agree_terms', 'describe_customer'}:
        with _locked(organization) as (_, roster):
            for row in (roster or {}).get('members', []):
                if row.get('state') not in {'active', 'unconfirmed'}:
                    continue
                if ask == 'approve_on_github' and (row['state'] != 'active' or mapping(row.get('rights')).get('publish') is not True):
                    continue
                ids.add(row.get('user_id'))
    return list(User.objects.filter(pk__in=ids, is_active=True).order_by('pk'))


def pending_request_count(organization):
    """Count only current pending requests for the owner's journey."""
    return len(pending_request_ids(organization))


def pending_request_ids(organization):
    """Read opaque pending IDs for individually deduplicated owner notices."""
    if not enabled():
        return []
    now = timezone.now()
    with _locked(organization) as (_, roster):
        return [row['request_id'] for row in (roster or {}).get('requests', [])
                if row.get('state') == 'pending' and (stamp(row.get('expires_at')) or now) > now]


def issue_join_reference(*, user, organization):
    """Issue an opaque, user-bound seven-day reference with no encoded identity."""
    if not enabled() or not _valid_user(user) or organization is None:
        return None
    nonce = secrets.token_urlsafe(16)
    cache.set('startup_team:join-ref:' + hashlib.sha256(nonce.encode()).hexdigest(),
              {'user_id': user.pk, 'organization_id': organization.pk}, timeout=7 * 86400)
    return nonce


def _rate(user, organization, *, invite=False, now):
    week = now.isocalendar()
    rules = [(f'user:{user.pk}:day:{now.date()}', 3, 2 * 86400),
             (f'user:{user.pk}:week:{week.year}-{week.week}', 10, 8 * 86400)] if not invite else [
             (f'org:{organization.pk}:invite-day:{now.date()}', 10, 2 * 86400)]
    for suffix, limit, timeout in rules:
        key = 'startup_team:rate:' + suffix
        cache.add(key, 0, timeout=timeout)
        try:
            count = cache.incr(key)
        except ValueError:
            raise TeamError('not_available', 'Please try again shortly.', 503) from None
        if count > limit:
            raise TeamError('rate_limited', 'Please try again later.', 429)


def _notify(name, organization, user):
    def notify():
        try:
            callback = getattr(importlib.import_module('content_factory.founder_transitions'), name, None)
            if callback:
                callback(organization, member_user=user)
        except Exception:
            logger.warning('startup_team_notice_pending event=%s user_id=%s', name, user.pk)
    transaction.on_commit(notify)


def _join(roster, user, *, state, via, actor, now):
    member = _member(roster, user)
    if member is not None:
        if member['state'] == 'unconfirmed' and via == 'owner_approval' and actor:
            member.update(state='active', approved_by_user_id=actor.pk, via='owner_approval')
        return member, False
    if user.pk == roster['owner_user_id']:
        return None, False
    if len(roster['members']) >= 100:
        raise TeamError('team_full', 'The team needs an ownership review.', 409)
    member = {'memberId': str(uuid4()), 'user_id': user.pk, 'role': 'member', 'state': state,
        'rights': {'publish': False, 'billing': False}, 'joined_at': now.isoformat(),
        'approved_by_user_id': actor.pk if actor else None, 'via': via}
    roster['members'].append(member)
    _history(roster, 'joined', actor, user, now)
    return member, True


def _auto_join(user, organization):
    if not getattr(settings, 'MY_STARTUP_TEAM_DOMAIN_AUTO_JOIN_ENABLED', False):
        return False
    from content_factory.website_models import WebsiteConnection
    from content_factory.models import WrittenArticle
    if (organization.billing_user_id or WebsiteConnection.objects.filter(organization=organization).exists()
            or WrittenArticle.objects.filter(organization=organization).filter(Q(published_at__isnull=False)
                | Q(publish_status__in=['merged', 'live'])).exists()
            or VibeRaisingCompany.objects.filter(organization=organization).count() != 1):
        return False
    from .startup_team_domains import canonical_host, registrable_domain, denied_domain
    host = canonical_host(organization.domain)
    email_host = canonical_host(user.email.rsplit('@', 1)[-1])
    if not host or host != registrable_domain(host) or denied_domain(host) or denied_domain(email_host):
        return False
    if registrable_domain(email_host) != host:
        return False
    # A typed company domain proves nothing. Require the current owner's
    # consumed email-code proof on the same verified apex, using existing rules.
    from content_factory.preview_domain_claim import _mailbox_proven, _code_received_at
    owner = User.objects.filter(pk=_owner(organization), is_active=True).first()
    return bool(owner and _code_received_at(user, user.email) and _mailbox_proven(owner, organization, host))


def request_join(*, user, join_reference, now=None):
    """Request membership without revealing the tenant or the owner to a caller."""
    if not enabled():
        raise TeamError('not_found', 'Not found.', 404)
    if not _verified(user):
        raise TeamError('email_unverified', 'Verify your email before joining the team.')
    if not isinstance(join_reference, str) or not 16 <= len(join_reference) <= 128:
        raise TeamError('not_allowed', 'This request is not available.')
    reference = cache.get('startup_team:join-ref:' + hashlib.sha256(join_reference.encode()).hexdigest())
    if not isinstance(reference, dict) or reference.get('user_id') != user.pk:
        raise TeamError('not_allowed', 'This request is not available.')
    organization = Organization.objects.filter(pk=reference.get('organization_id')).first()
    if organization is None:
        raise TeamError('not_allowed', 'This request is not available.')
    now = now or timezone.now()
    with _locked(organization, create=True) as (config, roster):
        if roster.get('frozen') or _owner(organization) is None:
            raise TeamError('not_available', 'The team needs an ownership review.', 409)
        if _member(roster, user) or roster['owner_user_id'] == user.pk:
            return {'status': 'joined'}
        pending = next((row for row in roster['requests'] if row.get('user_id') == user.pk
            and row.get('state') == 'pending' and (stamp(row.get('expires_at')) or now) > now), None)
        if pending:
            return {'status': 'already_requested'}
        _rate(user, organization, now=now)
        if sum(row.get('state') == 'pending' and (stamp(row.get('expires_at')) or now) > now for row in roster['requests']) >= 10:
            raise TeamError('rate_limited', 'Please try again later.', 429)
        if _auto_join(user, organization):
            _join(roster, user, state='unconfirmed', via='domain', actor=None, now=now)
            roster['requests'].append({'request_id': str(uuid4()), 'user_id': user.pk, 'state': 'pending',
                'created_at': now.isoformat(), 'expires_at': (now + timedelta(days=7)).isoformat(),
                'decided_at': None, 'decided_by_user_id': None})
            _save(config, roster)
            _notify('notify_member_joined', organization, user)
            return {'status': 'joined'}
        roster['requests'] = [row for row in roster['requests'] if row.get('state') == 'pending' or
                              (stamp(row.get('created_at')) or now) > now - timedelta(days=30)][-100:]
        roster['requests'].append({'request_id': str(uuid4()), 'user_id': user.pk, 'state': 'pending',
            'created_at': now.isoformat(), 'expires_at': (now + timedelta(days=7)).isoformat(),
            'decided_at': None, 'decided_by_user_id': None})
        _history(roster, 'requested', user, user, now)
        _save(config, roster)
    return {'status': 'requested'}


def decide_request(*, organization, user, request_id, decision, now=None):
    """Let the pinned owner decide a currently pending unexpired request once."""
    if decision not in {'approve', 'decline'}:
        raise TeamError('invalid_decision', 'Choose approve or decline.', 400)
    now = now or timezone.now()
    with _locked(organization, create=True) as (config, roster):
        _manage(user, organization, roster)
        row = next((row for row in roster['requests'] if row.get('request_id') == str(request_id)), None)
        if row is None or row.get('state') != 'pending' or (stamp(row.get('expires_at')) or now) <= now:
            raise TeamError('request_closed', 'This request has already closed.', 409)
        member_user = User.objects.filter(pk=row['user_id'], is_active=True, email_verified_at__isnull=False).first()
        if decision == 'approve' and member_user is None:
            raise TeamError('email_unverified', 'The requester must verify their email.', 409)
        row.update(state='approved' if decision == 'approve' else 'declined', decided_at=now.isoformat(), decided_by_user_id=user.pk)
        if decision == 'approve':
            _join(roster, member_user, state='active', via='owner_approval', actor=user, now=now)
            _notify('notify_join_approved', organization, member_user)
        else:
            unconfirmed = next((member for member in roster['members'] if member.get('user_id') == row['user_id']
                                and member.get('state') == 'unconfirmed'), None)
            if unconfirmed:
                roster['members'].remove(unconfirmed)
                from content_factory.founder_journey_alerts import queue_founder_journey_alert
                transaction.on_commit(lambda: queue_founder_journey_alert(organization=organization,
                    alert_reason='domain_claim_disputed', details={'claimant_user_id': row['user_id']}))
        _history(roster, 'approved' if decision == 'approve' else 'declined', user, row['user_id'], now)
        _save(config, roster)
    return {'status': row['state']}


def update_member(*, organization, user, member_id, rights=None, remove=False, now=None):
    """Recheck management or self-leave inside the same membership row lock."""
    now = now or timezone.now()
    with _locked(organization, create=True) as (config, roster):
        row = next((row for row in roster['members'] if row.get('memberId') == str(member_id)), None)
        if row is None:
            raise TeamError('not_found', 'Not found.', 404)
        if not (remove and row['user_id'] == user.pk):
            _manage(user, organization, roster)
        if remove:
            roster['members'].remove(row)
            _history(roster, 'removed', user, row['user_id'], now)
            if row['state'] == 'unconfirmed':
                from content_factory.founder_journey_alerts import queue_founder_journey_alert
                transaction.on_commit(lambda: queue_founder_journey_alert(organization=organization,
                    alert_reason='domain_claim_disputed', details={'claimant_user_id': row['user_id']}))
        else:
            if not isinstance(rights, dict) or set(rights) - {'publish', 'billing'} or any(type(v) is not bool for v in rights.values()):
                raise TeamError('invalid_rights', 'Choose valid team permissions.', 400)
            if row['state'] != 'active' and any(rights.values()):
                raise TeamError('not_allowed', 'Confirm this member before granting permissions.')
            row['rights'] = {**mapping(row.get('rights')), **rights}
            _history(roster, 'rights_changed', user, row['user_id'], now)
        _save(config, roster)
    return {'status': 'removed' if remove else 'updated'}


def invite_link(*, organization, user, action, now=None):
    """Return a fresh fragment token once; store only its SHA-256 and limits."""
    if action not in {'create', 'revoke'}:
        raise TeamError('invalid_action', 'Choose create or revoke.', 400)
    now = now or timezone.now()
    with _locked(organization, create=True) as (config, roster):
        _manage(user, organization, roster)
        token = secrets.token_urlsafe(32) if action == 'create' else None
        roster['invite'] = {'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'created_at': now.isoformat(),
            'created_by_user_id': user.pk, 'expires_at': (now + timedelta(days=7)).isoformat(), 'uses': 0, 'max_uses': 5} if token else None
        _save(config, roster)
    if token:
        frontend = str(getattr(settings, 'COMMUNITY_CHAT_FRONTEND_URL', '') or '').rstrip('/')
        return {'url': f'{frontend}/my-startup/join#{token}', 'expiresAt': roster['invite']['expires_at']}
    return {'status': 'revoked'}


def join_invite(*, user, token, now=None):
    """Consume one verified invite use under the organization lock, token-free logs."""
    if not enabled():
        raise TeamError('not_found', 'Not found.', 404)
    if not _verified(user):
        raise TeamError('email_unverified', 'Verify your email before joining the team.')
    if not isinstance(token, str) or len(token) != 43:
        raise TeamError('invite_closed', 'This invite is no longer available.', 409)
    digest = hashlib.sha256(token.encode()).hexdigest()
    config = OrganizationContentConfig.objects.select_related('organization').filter(
        pillar_strategy__startup_team__invite__token_sha256=digest).first()
    if config is None:
        raise TeamError('invite_closed', 'This invite is no longer available.', 409)
    now = now or timezone.now()
    organization = config.organization
    with _locked(organization) as (config, roster):
        invitation = mapping((roster or {}).get('invite'))
        if (not roster or roster.get('frozen') or _owner(organization) is None
                or not secrets.compare_digest(invitation.get('token_sha256', ''), digest)
                or (stamp(invitation.get('expires_at')) or now) <= now
                or type(invitation.get('uses')) is not int or invitation['uses'] >= 5):
            raise TeamError('invite_closed', 'This invite is no longer available.', 409)
        if _member(roster, user) or roster['owner_user_id'] == user.pk:
            return {'status': 'joined'}
        _rate(user, organization, invite=True, now=now)
        member, created = _join(roster, user, state='active', via='invite_link', actor=None, now=now)
        if created:
            invitation['uses'] += 1
            _save(config, roster)
            _notify('notify_member_joined', organization, user)
            logger.info('startup_team_invite_joined member_id=%s', member['memberId'])
    return {'status': 'joined'}


def team_payload(*, organization, user):
    """Owners see management details; members see first names only."""
    if not enabled() or not _valid_user(user):
        raise TeamError('not_found', 'Not found.', 404)
    now = timezone.now()
    with _locked(organization) as (_, roster):
        owner = _owner(organization)
        manage = bool(_valid_user(user) and owner == user.pk and not (roster or {}).get('frozen'))
        if not manage and not _member(roster, user):
            raise TeamError('not_found', 'Not found.', 404)
        ids = {row.get('user_id') for row in (roster or {}).get('members', [])}
        ids.update(row.get('user_id') for row in (roster or {}).get('requests', []) if manage)
        users = {u.pk: u for u in User.objects.filter(pk__in=ids, is_active=True)}
        members = []
        for row in (roster or {}).get('members', []):
            person = users.get(row['user_id'])
            if person is None:
                continue
            item = {'memberId': row['memberId'], 'firstName': person.first_name or '', 'role': 'member'}
            if manage:
                item.update(email=person.email, state=row['state'], rights=row['rights'], joinedAt=row['joined_at'])
            members.append(item)
        requests = [{'requestId': row['request_id'], 'firstName': users[row['user_id']].first_name or '',
            'email': users[row['user_id']].email, 'createdAt': row['created_at'], 'expiresAt': row['expires_at']}
            for row in (roster or {}).get('requests', []) if manage and row['user_id'] in users
            and row.get('state') == 'pending' and (stamp(row.get('expires_at')) or now) > now]
        payload = {'members': members, 'requests': requests, 'canManage': manage}
        if manage:
            invitation = mapping((roster or {}).get('invite'))
            payload['invite'] = {'active': bool(invitation and (stamp(invitation.get('expires_at')) or now) > now
                and invitation.get('uses', 5) < 5), 'expiresAt': invitation.get('expires_at')}
        return payload


def freeze_for_offboard(organization, user):
    """Freeze the pin and report whether members still require retained reads."""
    if getattr(getattr(organization, '_state', None), 'adding', True):
        return False
    with _locked(organization) as (config, roster):
        if roster is None or roster.get('owner_user_id') != user.pk:
            return False
        if not roster.get('frozen'):
            roster['frozen'] = True
            _history(roster, 'owner_left', user, user, timezone.now())
            _save(config, roster)
        return any(row.get('state') in {'active', 'unconfirmed'} for row in roster.get('members', []))


def member_companies(user):
    """List member startup descriptors separately from the caller's own rows."""
    if not enabled() or not _valid_user(user):
        return []
    rows = []
    for config in OrganizationContentConfig.objects.filter(pillar_strategy__has_key=KEY).select_related('organization').order_by('pk'):
        with _locked(config.organization) as (_, roster):
            rights = _rights(user, config.organization, roster)
            saved = mapping((roster or {}).get('company'))
            if not rights or not saved.get('id'):
                continue
            rows.append({'id': saved['id'], 'name': saved.get('name', ''), 'domain': saved.get('domain', ''),
                'role': 'member', 'rights': {'publish': 'publish' in rights, 'billing': 'spend_startup_points' in rights},
                'state': 'frozen' if roster.get('frozen') else _member(roster, user)['state']})
    return rows


def run_team_request_sweep(now=None, limit=None):
    """Expire seven-day requests and queue one 72-hour unanswered alert each."""
    if not enabled():
        return {'status': 'disabled'}
    now = now or timezone.now()
    rows = OrganizationContentConfig.objects.filter(pillar_strategy__has_key=KEY).select_related('organization').order_by('pk')[:limit or 500]
    summary = {'expired': 0, 'alerted': 0}
    from content_factory.founder_journey_alerts import queue_founder_journey_alert
    for row in rows:
        with _locked(row.organization) as (config, roster):
            if roster is None:
                continue
            for request in roster['requests']:
                if request['state'] != 'pending':
                    continue
                if (stamp(request.get('expires_at')) or now) <= now:
                    request['state'] = 'expired'
                    _history(roster, 'expired', None, request['user_id'], now)
                    summary['expired'] += 1
                elif not request.get('alerted_at') and (stamp(request.get('created_at')) or now) <= now - timedelta(hours=72):
                    queue_founder_journey_alert(organization=row.organization, alert_reason='join_request_unanswered',
                        slot=request['request_id'], details={'request_id': request['request_id']})
                    request['alerted_at'] = now.isoformat()
                    summary['alerted'] += 1
            _save(config, roster)
    return summary
