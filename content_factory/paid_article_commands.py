"""Durable generic article HTTP commands; replay never starts another worker."""
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import re
import secrets

from django.utils import timezone
from rest_framework.response import Response

KEY = 'paid_article_commands'
OUTBOX_KEY = 'article_http_command_v1'
MAX_COMMANDS = 1024
_active_claim = ContextVar('paid_article_http_claim', default=None)


def _exact(value, expected):
    return (isinstance(value, dict) and set(value) == set(expected)
        and all(type(value[key]) is type(item) and value[key] == item for key, item in expected.items()))


class PaidArticleCommandRefusal(ValueError):
    def __init__(self, reason='operation_conflict', *, status=409):
        if reason not in {'operation_conflict', 'in_progress', 'permission_denied', 'refunded'}:
            raise ValueError('Unknown article command refusal.')
        self.reason, self.status = reason, status
        super().__init__('The original article request is still being confirmed.' if reason == 'in_progress'
            else 'Review the original article request before trying again.')


def _identity(data, header):
    key = data.get('client_request_id', data.get('clientRequestId'))
    if (type(key) is not str or not 1 <= len(key) <= 255 or key != key.strip()
            or any(ord(char) < 33 or ord(char) == 127 for char in key)
            or ('clientRequestId' in data and data['clientRequestId'] != key)
            or header is not None and header != key):
        raise PaidArticleCommandRefusal()
    try:
        raw = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        raise PaidArticleCommandRefusal()
    return key, hashlib.sha256(key.encode()).hexdigest(), hashlib.sha256(raw.encode()).hexdigest()


def _confirmed_points(data):
    values = [data[key] for key in ('expected_cost_points', 'expectedCostPoints') if key in data]
    return (values[0] if values and type(values[0]) is int and values[0] >= 0
        and all(type(value) is int and value == values[0] for value in values) else None)


def _cost_confirmed(data):
    values = [data[key] for key in ('cost_confirmed', 'costConfirmed') if key in data]
    return bool(values) and all(value is True for value in values)


def _current_user(context, user, config, roster, *, confirmed_points=None, cost_confirmed=False):
    """Run inside the organization lock, including immediately before debit."""
    from django.contrib.auth import get_user_model
    from founder_tools import startup_team
    from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
    fresh = get_user_model().objects.filter(pk=getattr(user, 'pk', None), is_active=True).first()
    if (fresh is None or config is None or not isinstance(config.pillar_strategy, dict)
            or startup_team.KEY in config.pillar_strategy and roster is None
            or roster and roster.get('frozen')
            or not VibeRaisingCompany.objects.filter(pk=context.company.pk,
                organization_id=context.organization.pk).exists()
            or not VibeRaisingProfile.objects.filter(pk=context.profile.pk, user_id=fresh.pk).exists()):
        raise PaidArticleCommandRefusal('permission_denied', status=403)
    owner = startup_team._owner(context.organization)
    rights = startup_team._rights(fresh, context.organization, roster) if startup_team.enabled() else frozenset()
    if owner != fresh.pk and not {'write_articles', 'spend_startup_points'} <= rights:
        raise PaidArticleCommandRefusal('permission_denied', status=403)
    if owner != fresh.pk:
        # A member's paid portable command cannot consume the owner's free
        # first article or fall back to an unstated personal-wallet payment.
        from organizations.models import Organization
        from .company_billing import billing_user_for_organization
        from .website_contract import WebsiteAuthorityError
        current_org = Organization.objects.select_related('billing_user').get(pk=context.organization.pk)
        if (cost_confirmed is not True or type(confirmed_points) is not int or confirmed_points <= 0
                or current_org.billing_user_id is None):
            raise PaidArticleCommandRefusal('permission_denied', status=403)
        try:
            payer = billing_user_for_organization(current_org, fresh)
        except WebsiteAuthorityError:
            raise PaidArticleCommandRefusal('permission_denied', status=403)
        if not VibeRaisingCompany.objects.filter(organization_id=current_org.pk, profile__user_id=payer.pk,
                profile__role=VibeRaisingProfile.ROLE_FOUNDER, profile__user__is_active=True).exists():
            raise PaidArticleCommandRefusal('permission_denied', status=403)
    return fresh


def _proof(context, receipt):
    return {'schema_version': 1, 'organization_id': str(context.organization.pk),
        'company_id': receipt['company_id'], 'user_id': receipt['user_id'],
        'client_request_id': receipt['client_request_id'], 'body_sha256': receipt['body_sha256']}


def _replay(context, receipt):
    from roo.models import Ledger
    from workflow_runs.models import ContentFactoryRun
    from .dispatch_models import ContentFactoryDispatchOutbox
    key = receipt['client_request_id']
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=key).first()
    if row is None:
        return None
    envelope = row.payload
    if (row.organization_id != context.organization.pk or not isinstance(envelope, dict)
            or not _exact(envelope.get(OUTBOX_KEY), _proof(context, receipt))
            or not isinstance(envelope.get('request'), dict)
            or str(envelope['request'].get('roo_points_requested_by_user_id')) != str(receipt['user_id'])
            or envelope['request'].get('client_request_id') != key):
        raise PaidArticleCommandRefusal()
    request = envelope['request']
    payer = envelope.get('billing_user_id')
    if not isinstance(payer, str) or not payer.isascii() or not payer.isdigit() or int(payer) <= 0:
        raise PaidArticleCommandRefusal()
    if Ledger.objects.filter(kind='REFUND', user_id=int(payer), refund_of__user_id=int(payer),
            refund_of__idempotency_key='content_factory:charge:' + key).exists():
        raise PaidArticleCommandRefusal('refunded')
    if row.state not in {'pending', 'delivering', 'delivered'}:
        raise PaidArticleCommandRefusal()
    runs = list(ContentFactoryRun.objects.filter(organization=context.organization,
        run_request__client_request_id=key)[:2])
    if len(runs) > 1:
        raise PaidArticleCommandRefusal()
    run = runs[0] if runs else None
    if (row.state == 'delivered' and (not row.run_id or run is None)
            or run is None and row.run_id):
        raise PaidArticleCommandRefusal()
    if run is not None:
        if (run.workflow != 'article_generation' or run.domain != request.get('domain')
                or row.run_id and row.run_id != run.run_id):
            raise PaidArticleCommandRefusal()
        from .run_ownership import WIRE_REQUEST_KEY, owner_receipt, wire_identity
        try:
            admission = owner_receipt(run)
            if (admission is not None and not _exact(request.get(WIRE_REQUEST_KEY), wire_identity(admission))
                    or admission is None and WIRE_REQUEST_KEY in request):
                raise PaidArticleCommandRefusal()
        except ValueError:
            raise PaidArticleCommandRefusal()
        from .website_run_authority import event_owned
        if event_owned(run):
            from .run_terminal_settlement import terminal_execution_closed
            try:
                if terminal_execution_closed(run):
                    raise PaidArticleCommandRefusal()
            except PaidArticleCommandRefusal:
                raise
            except ValueError:
                raise PaidArticleCommandRefusal()
    else:
        from .run_ownership import WIRE_REQUEST_KEY
        if WIRE_REQUEST_KEY in request:
            raise PaidArticleCommandRefusal()
    run_id = run.run_id if run is not None else row.run_id or key
    return Response({'run_id': run_id, 'runId': run_id,
        'status': run.status if run is not None else 'queued'}, status=202)


def _claim(context, user, data, header, *, allow_new):
    from founder_tools import startup_team
    from roo.models import Ledger
    from workflow_runs.models import ContentFactoryRun
    from .dispatch_models import ContentFactoryDispatchOutbox
    key, digest, body_hash = _identity(data, header)
    confirmed_points = _confirmed_points(data)
    cost_confirmed = _cost_confirmed(data)
    with startup_team._locked(context.organization) as (config, roster):
        fresh = _current_user(context, user, config, roster, confirmed_points=confirmed_points,
            cost_confirmed=cost_confirmed)
        strategy = deepcopy(config.pillar_strategy)
        commands = strategy.get(KEY, {})
        if not isinstance(commands, dict):
            raise PaidArticleCommandRefusal()
        receipt = commands.get(digest)
        if receipt is None:
            if (not allow_new or len(commands) >= MAX_COMMANDS
                    or ContentFactoryDispatchOutbox.objects.filter(client_request_id=key).exists()
                    or ContentFactoryRun.objects.filter(run_request__client_request_id=key).exists()
                    or Ledger.objects.filter(kind='SPEND', idempotency_key='content_factory:charge:' + key).exists()):
                # An old transport has no original HTTP-body proof to adopt.
                raise PaidArticleCommandRefusal()
            receipt = {'schema_version': 1, 'company_id': str(context.company.pk), 'user_id': fresh.pk,
                'client_request_id': key, 'body_sha256': body_hash, 'confirmed_points': confirmed_points,
                'cost_confirmed': cost_confirmed}
        else:
            if (not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int
                    or set(receipt) != {'schema_version', 'company_id', 'user_id', 'client_request_id',
                        'body_sha256', 'confirmed_points', 'cost_confirmed', 'lease_token', 'lease_until'}
                    or receipt['schema_version'] != 1 or receipt.get('company_id') != str(context.company.pk)
                    or type(receipt.get('user_id')) is not int or receipt['user_id'] != fresh.pk
                    or receipt.get('client_request_id') != key or receipt.get('body_sha256') != body_hash
                    or type(receipt.get('confirmed_points')) is not type(confirmed_points)
                    or receipt.get('confirmed_points') != confirmed_points
                    or type(receipt.get('cost_confirmed')) is not bool or receipt['cost_confirmed'] != cost_confirmed
                    or not isinstance(receipt.get('lease_token'), str)
                    or not re.fullmatch('[a-f0-9]{32}', receipt['lease_token'])):
                raise PaidArticleCommandRefusal()
            try:
                active = timezone.datetime.fromisoformat(receipt['lease_until']) > timezone.now()
            except (KeyError, TypeError, ValueError):
                raise PaidArticleCommandRefusal()
            replay = _replay(context, receipt)
            if replay is not None:
                return None, replay
            if not allow_new:
                raise PaidArticleCommandRefusal()
            if active:
                raise PaidArticleCommandRefusal('in_progress')
        receipt['lease_token'] = secrets.token_hex(16)
        receipt['lease_until'] = (timezone.now() + timedelta(seconds=120)).isoformat()
        strategy[KEY] = {**commands, digest: receipt}
        config.pillar_strategy = strategy
        config.save(update_fields=['pillar_strategy', 'updated_at'])
        return (digest, deepcopy(receipt)), None


def recheck_current_claim(context, user, payload):
    active = _active_claim.get()
    if active is None:
        return
    from django.db import transaction
    from founder_tools import startup_team
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Article command authority requires the debit transaction.')
    saved_context, saved_user, (digest, original) = active
    if (context.organization.pk != saved_context.organization.pk or user.pk != saved_user.pk
            or payload.get('client_request_id') != original['client_request_id']):
        raise PaidArticleCommandRefusal()
    with startup_team._locked(context.organization) as (config, roster):
        fresh = _current_user(context, user, config, roster, confirmed_points=original['confirmed_points'],
            cost_confirmed=original['cost_confirmed'])
        from core.actor_ids import actor_ids_for_user
        if payload.get('slack_user_id') not in actor_ids_for_user(fresh):
            raise PaidArticleCommandRefusal('permission_denied', status=403)
        commands = config.pillar_strategy.get(KEY)
        current = commands.get(digest) if isinstance(commands, dict) else None
        if not _exact(current, original):
            raise PaidArticleCommandRefusal()


def portable_member_context(*, user, actor_id, domain, article_request):
    """A paid HTTP claim can authorize only a source-free portable member draft."""
    active = _active_claim.get()
    if active is None:
        return None
    from core.actor_ids import actor_ids_for_user
    from founder_tools import startup_team
    from .founder_journey_cohort import is_new_journey
    from .portable_drafts import explicit_portable_request
    from .website_contract import CONNECTION_FIELDS, SECRET_KEYS
    context, original_user, (_, original) = active
    forbidden = {*CONNECTION_FIELDS, *SECRET_KEYS, 'operation_id', 'operation_attempt', 'deletion_epoch',
        'expected_source_sha', 'source_sha', 'repo_head_sha', 'app_root', 'branch', 'github_installation_id',
        'installation_id', 'connection_role', 'verification'}
    if (user.pk != original_user.pk or domain != context.organization.domain
            or article_request.get('client_request_id') != original['client_request_id']
            or not is_new_journey(context.organization) or not explicit_portable_request(article_request)
            or article_request.get('github_repo') not in (None, '')
            or any(key in article_request for key in forbidden)
            or actor_id not in actor_ids_for_user(user)
            or article_request.get('slack_user_id') != actor_id
            or type(original['confirmed_points']) is not int or original['confirmed_points'] <= 0
            or original['cost_confirmed'] is not True
            or _confirmed_points(article_request) != original['confirmed_points']):
        raise PaidArticleCommandRefusal('permission_denied', status=403)
    with startup_team._locked(context.organization) as (config, roster):
        fresh = _current_user(context, user, config, roster, confirmed_points=original['confirmed_points'],
            cost_confirmed=original['cost_confirmed'])
        if startup_team._owner(context.organization) == fresh.pk:
            return None
        recheck_current_claim(context, fresh, article_request)
        return context


def bind_dispatch(row, *, created):
    """Only the trusted active HTTP claim may seal its newly allocated outbox."""
    active = _active_claim.get()
    if active is None:
        return
    context, user, (_, original) = active
    request = row.payload.get('request') if isinstance(row.payload, dict) else None
    if (row.organization_id != context.organization.pk or row.client_request_id != original['client_request_id']
            or not isinstance(request, dict) or str(request.get('roo_points_requested_by_user_id')) != str(user.pk)):
        raise PaidArticleCommandRefusal()
    proof = _proof(context, original)
    if not created:
        if not _exact(row.payload.get(OUTBOX_KEY), proof):
            raise PaidArticleCommandRefusal()
        return
    row.payload = {**row.payload, OUTBOX_KEY: proof}
    row.save(update_fields=['payload'])


def finish(context, claim):
    from founder_tools import startup_team
    digest, original = claim
    with startup_team._locked(context.organization) as (config, _):
        strategy = deepcopy(config.pillar_strategy or {})
        commands = strategy.get(KEY)
        current = commands.get(digest) if isinstance(commands, dict) else None
        if not _exact(current, original):
            return
        current['lease_until'] = timezone.now().isoformat()
        config.pillar_strategy = strategy
        config.save(update_fields=['pillar_strategy', 'updated_at'])


def _retained_header(context, header):
    """A known v2 header cannot escape its body proof via the legacy path."""
    if type(header) is not str or not header:
        return False
    from .models import OrganizationContentConfig
    from .dispatch_models import ContentFactoryDispatchOutbox
    strategy = OrganizationContentConfig.objects.filter(organization=context.organization).values_list(
        'pillar_strategy', flat=True).first()
    if isinstance(strategy, dict) and KEY in strategy:
        commands = strategy[KEY]
        if not isinstance(commands, dict) or hashlib.sha256(header.encode()).hexdigest() in commands:
            return True
    return ContentFactoryDispatchOutbox.objects.filter(client_request_id=header,
        payload__has_key=OUTBOX_KEY).exists()


def execute(*, context, user, data, header, start):
    """Only explicit keyed v2 HTTP commands gain this durable replay contract."""
    from .founder_journey_cohort import is_new_journey
    if not isinstance(data, dict) or not (data.get('client_request_id') or data.get('clientRequestId')):
        if _retained_header(context, header):
            exc = PaidArticleCommandRefusal()
            return Response({'code': 'operation_conflict', 'reason': exc.reason,
                'detail': str(exc), 'retryable': False}, status=exc.status)
        return start()
    allow_new = is_new_journey(context.organization)
    if not allow_new:
        # Switches govern new enrollment; a retained command never loses its
        # body binding or falls through to an old dispatcher after a flag flip.
        from .models import OrganizationContentConfig
        from .dispatch_models import ContentFactoryDispatchOutbox
        strategy = OrganizationContentConfig.objects.filter(organization=context.organization).values_list('pillar_strategy', flat=True).first()
        present = isinstance(strategy, dict) and KEY in strategy
        key = data.get('client_request_id', data.get('clientRequestId'))
        original = ContentFactoryDispatchOutbox.objects.filter(client_request_id=key).first() if type(key) is str else None
        recorded_outbox = original is not None and isinstance(original.payload, dict) and OUTBOX_KEY in original.payload
        if not recorded_outbox and not present:
            return start()
    claim = None
    try:
        claim, replay = _claim(context, user, data, header, allow_new=allow_new)
        if replay is not None:
            return replay
        token = _active_claim.set((context, user, claim))
        try:
            from .run_ownership import RunOwnershipConflict
            try:
                return start()
            except RunOwnershipConflict:
                raise PaidArticleCommandRefusal()
        finally:
            _active_claim.reset(token)
    except PaidArticleCommandRefusal as exc:
        return Response({'code': 'operation_conflict', 'reason': exc.reason,
            'detail': str(exc), 'retryable': exc.reason == 'in_progress'}, status=exc.status)
    finally:
        if claim is not None:
            finish(context, claim)
