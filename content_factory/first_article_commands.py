"""Durable first-article HTTP command identities, independent of execution state."""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
import hashlib
import json
import secrets

from django.utils import timezone

KEY = 'first_article_commands'
MAX_COMMANDS = 1024
_active_claim = ContextVar('first_article_command_claim', default=None)


@contextmanager
def dispatch_claim(context, user, claim):
    token = _active_claim.set((context.organization.pk, user.pk, claim))
    try:
        yield
    finally:
        _active_claim.reset(token)


def recheck_current_claim(context, user, payload):
    """Recheck current consent under the actual debit/outbox transaction lock."""
    active = _active_claim.get()
    if active is None:
        return
    from django.db import transaction
    from founder_tools import startup_team
    from .content_first_article import require_owner
    from .models import OrganizationContentConfig
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('First-article consent must be checked with its debit.')
    organization_id, user_id, (digest, original) = active
    if (organization_id != context.organization.pk or user_id != user.pk
            or payload.get('client_request_id') != original['client_request_id']):
        _refuse()
    with startup_team._locked(context.organization) as (_, _roster):
        require_owner(user, context.organization)
        config = OrganizationContentConfig.objects.select_for_update().get(organization=context.organization)
        current = (config.pillar_strategy or {}).get(KEY, {}).get(digest)
        if (not isinstance(current, dict) or current.get('lease_token') != original['lease_token']
                or current.get('payload') != original['payload'] or current.get('body_sha256') != original['body_sha256']):
            _refuse()


def _refuse(reason='operation_conflict', status=409):
    from .content_first_article import FirstArticleRefusal
    raise FirstArticleRefusal(status, 'Review the saved request before trying again.', reason)


def identity(header, data):
    if (not isinstance(header, str) or not 1 <= len(header) <= 255
            or header != header.strip() or any(ord(char) < 33 or ord(char) == 127 for char in header)):
        _refuse('invalid_key', 400)
    try:
        body = json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError):
        _refuse('invalid_key', 400)
    return hashlib.sha256(header.encode()).hexdigest(), hashlib.sha256(body.encode()).hexdigest()


def _replay(context, receipt):
    from workflow_runs.models import ContentFactoryRun
    from .content_first_article import started_body
    from .dispatch_models import ContentFactoryDispatchOutbox
    key = receipt['client_request_id']
    outbox = ContentFactoryDispatchOutbox.objects.filter(organization=context.organization,
        client_request_id=key).first()
    run = ContentFactoryRun.objects.filter(organization=context.organization,
        run_request__client_request_id=key).first()
    if run is not None:
        return started_body(run.run_id, run.status, context.company)
    if outbox is not None:
        # A retained outbox owns delivery even when its first acknowledgement
        # was lost. Replaying this HTTP command must never allocate another start.
        return started_body(outbox.run_id or key, 'queued', context.company)
    response = receipt.get('response')
    return deepcopy(response) if isinstance(response, dict) and response.get('runId') else None


def claim(context, user, header, data, prepare):
    """Bind body/requester and save the exact approved payload before any debit/HTTP.

    ``prepare`` performs only the existing local profile/catalogue writes. The
    short database lock ends before the charged dispatch and provider transport.
    """
    from founder_tools import startup_team
    from .content_first_article import FirstArticleRefusal, require_owner
    digest, body_hash = identity(header, data)
    with startup_team._locked(context.organization) as (_, _roster):
        from .models import OrganizationContentConfig
        config = OrganizationContentConfig.objects.select_for_update().get(organization=context.organization)
        require_owner(user, context.organization)
        strategy = deepcopy(config.pillar_strategy or {})
        commands = strategy.get(KEY, {})
        if not isinstance(commands, dict):
            _refuse()
        receipt = commands.get(digest)
        if receipt is not None:
            if (not isinstance(receipt, dict) or receipt.get('schema_version') != 1
                    or receipt.get('user_id') != user.pk or receipt.get('company_id') != str(context.company.pk)
                    or receipt.get('body_sha256') != body_hash
                    or receipt.get('client_request_id') != f'first-article:{context.company.pk}:command:{digest[:32]}'):
                _refuse()
            replay = _replay(context, receipt)
            if replay is not None:
                return None, replay
            until = receipt.get('lease_until')
            if not isinstance(until, str):
                _refuse()
            try:
                active = timezone.datetime.fromisoformat(until) > timezone.now()
            except (TypeError, ValueError):
                _refuse()
            if active:
                raise FirstArticleRefusal(409, 'Your original request is still being confirmed.', 'in_progress')
            if not isinstance(receipt.get('payload'), dict):
                _refuse()
        else:
            if len(commands) >= MAX_COMMANDS:
                _refuse('not_available')
            client_key = f'first-article:{context.company.pk}:command:{digest[:32]}'
            payload, replay = prepare(config, client_key)
            receipt = {'schema_version': 1, 'company_id': str(context.company.pk), 'user_id': user.pk,
                'body_sha256': body_hash, 'client_request_id': client_key, 'payload': deepcopy(payload)}
            if replay is not None:
                receipt['response'] = deepcopy(replay)
        receipt['lease_token'] = secrets.token_hex(16)
        receipt['lease_until'] = (timezone.now() + timedelta(seconds=120)).isoformat()
        # Catalogue preparation may have updated this same config under the lock.
        config.refresh_from_db()
        strategy = deepcopy(config.pillar_strategy or {})
        strategy[KEY] = {**commands, digest: receipt}
        config.pillar_strategy = strategy
        config.save(update_fields=['pillar_strategy', 'updated_at'])
        return (digest, deepcopy(receipt)), replay


def finish(context, claim, response=None):
    """Release only our claim; retain the first successful acknowledgement forever."""
    from founder_tools import startup_team
    from .models import OrganizationContentConfig
    digest, original = claim
    with startup_team._locked(context.organization) as (_, _roster):
        config = OrganizationContentConfig.objects.select_for_update().get(organization=context.organization)
        strategy = deepcopy(config.pillar_strategy or {})
        commands = strategy.get(KEY)
        current = commands.get(digest) if isinstance(commands, dict) else None
        if not isinstance(current, dict) or current.get('lease_token') != original['lease_token']:
            return
        current['lease_until'] = timezone.now().isoformat()
        if isinstance(response, dict) and response.get('runId') and 'response' not in current:
            current['response'] = deepcopy(response)
        config.pillar_strategy = strategy
        config.save(update_fields=['pillar_strategy', 'updated_at'])
