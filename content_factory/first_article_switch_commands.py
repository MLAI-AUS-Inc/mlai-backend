"""Recover only the replacement admitted by one original keyed topic switch."""
from contextlib import contextmanager
from copy import deepcopy

from django.contrib.auth import get_user_model

from . import first_article_commands as commands


def _pending():
    from .content_first_article import FirstArticleRefusal
    raise FirstArticleRefusal(503, 'Your original topic switch is still being confirmed.', 'switch_pending')


def _owner(context, user):
    from .content_first_article import require_owner
    current = get_user_model().objects.filter(pk=getattr(user, 'pk', None), is_active=True).first()
    require_owner(current, context.organization)
    return current


def _source(context, payload):
    from workflow_runs.models import ContentFactoryRun
    from .website_cancellation_retirement import run_binding
    target = payload['switch']
    run = ContentFactoryRun.objects.filter(pk=target['run_pk'], organization=context.organization).first()
    if (run is None or run.run_id != target['run_id'] or run_binding(run) != target['binding']):
        commands._refuse()
    return run


@contextmanager
def _locked_claim(context, user, claim):
    from founder_tools import startup_team
    digest, original = claim
    with startup_team._locked(context.organization) as (config, _roster):
        _owner(context, user)
        strategy = deepcopy(config.pillar_strategy or {})
        current = strategy.get(commands.KEY, {}).get(digest)
        if (not isinstance(current, dict) or current.get('lease_token') != original['lease_token']
                or current.get('payload') != original['payload']
                or current.get('body_sha256') != original['body_sha256']
                or current.get('user_id') != user.pk or current.get('company_id') != str(context.company.pk)):
            commands._refuse()
        yield config, strategy, current


def _cancelled(context, user, claim):
    with _locked_claim(context, user, claim) as (_, _, current):
        run = _source(context, current['payload'])
        return run, deepcopy(current.get('switch_cancellation'))


def _retain_cancellation(context, user, claim, proof):
    with _locked_claim(context, user, claim) as (config, strategy, current):
        _source(context, current['payload'])
        current['switch_cancellation'] = deepcopy(proof)
        config.pillar_strategy = strategy
        config.save(update_fields=['pillar_strategy', 'updated_at'])


def _stop_original(context, user, claim):
    from . import vibe_marketing_views as views
    from .article_publication_evidence import cancellation_receipt, useful_delivery, publication_state
    from .first_article_entitlement import release_entitlement
    from .run_cancellation import cancellation_request_fields
    from .run_reconciliation import authoritative_run
    from .website_cancellation_retirement import expected_article_cancellation

    run, saved = _cancelled(context, user, claim)
    if authoritative_run(run):
        # A status hint or legacy cancellation receipt cannot release an owned
        # grant. The existing closure controller owns the complete effect proof.
        from .run_terminal_settlement import settle_authoritative_terminal, terminal_closed_without_effects
        from .run_dispatch_settlement import _free_obligation_state
        if not terminal_closed_without_effects(run):
            if run.status not in {'cancelled', 'denied', 'failed', 'completed'}:
                views._call_content_factory_run_action(run_id=run.run_id, action='cancel',
                    payload={**cancellation_request_fields('superseded', 'system'),
                        'expected_cancellation_v1': expected_article_cancellation(run)},
                    workflow=run.workflow, control_user=user, control_command_key='switch-' + claim[0])
            from .run_owned_observations import refresh_owned_run
            refresh_owned_run(run)
            settle_authoritative_terminal(run.run_id)
            run.refresh_from_db()
        if not terminal_closed_without_effects(run) or _free_obligation_state(run) != 'released':
            _pending()
        _retain_cancellation(context, user, claim, {'kind': 'owned_terminal_closure'})
        return

    if saved is not None:
        try:
            receipt = cancellation_receipt(run, saved)
        except (ValueError, TypeError):
            commands._refuse()
        if (receipt is None or receipt.get('outcome') != 'no_article_effect'
                or receipt.get('delivered_value') is not False
                or useful_delivery(run) or publication_state(run) not in {'none', 'setup_only'}):
            commands._refuse()
        return

    remote = views._call_content_factory_run_action(run_id=run.run_id, action='cancel',
        payload={**cancellation_request_fields('superseded', 'system'),
            'expected_cancellation_v1': expected_article_cancellation(run)},
        workflow=run.workflow, control_user=user, control_command_key='switch-' + claim[0])
    remote = remote if isinstance(remote, dict) else {}
    try:
        receipt = cancellation_receipt(run, remote)
        remote_status = int(remote.get('content_factory_status_code') or 0)
    except (ValueError, TypeError, OverflowError):
        receipt = None
        remote_status = 503
    if (remote.get('error') or remote_status >= 400
            or receipt is None or receipt.get('outcome') != 'no_article_effect'
            or receipt.get('delivered_value') is not False):
        _pending()
    # Commit the bound stop, its original grant release and the switch progress
    # together. No external request is made while these SQL locks are held.
    with _locked_claim(context, user, claim) as (config, strategy, current):
        run = _source(context, current['payload'])
        try:
            checked = cancellation_receipt(run, {'article_cancellation_v1': receipt})
        except (ValueError, TypeError):
            commands._refuse()
        if checked is None or useful_delivery(run) or publication_state(run) not in {'none', 'setup_only'}:
            commands._refuse()
        release_entitlement(organization=context.organization, run=run, reason='superseded')
        views._cancel_local_article_run(run=run, organization=context.organization,
            remote_data={'article_cancellation_v1': receipt}, cancellation_reason='superseded', cancelled_by='system')
        current['switch_cancellation'] = {'article_cancellation_v1': receipt}
        # Cancellation may update other configuration fields. Preserve them.
        config.refresh_from_db()
        strategy = deepcopy(config.pillar_strategy or {})
        strategy[commands.KEY][claim[0]] = current
        config.pillar_strategy = strategy
        config.save(update_fields=['pillar_strategy', 'updated_at'])


def switch_topic_command(context, user, data, header):
    """Bind an original switch before cancellation and recover its exact child."""
    from workflow_runs.models import ContentFactoryRun
    from . import content_first_article as cards
    from .first_article_entitlement import entitlement_state
    from .founder_journey import project_founder_journey
    from .website_cancellation_retirement import run_binding

    user = _owner(context, user)
    original_key, _ = commands.identity(header, data)
    data = data if isinstance(data, dict) else {}
    run_id = data.get('runId')
    if not isinstance(run_id, str) or not run_id.strip():
        commands._refuse('not_available')
    press = cards.parse_press({**{key: data.get(key) for key in ('topicId', 'customTopic') if key in data},
        'expectedCostPoints': 0})

    def prepare(config, client_key):
        journey = project_founder_journey(context.organization, user=user, company=context.company)
        if not journey.get('switchTopicAvailable') or journey.get('runId') != run_id:
            commands._refuse('not_available')
        run = ContentFactoryRun.objects.filter(organization=context.organization, run_id=run_id).first()
        if run is None or not cards.is_first_article_request(run, context.company):
            commands._refuse('not_available')
        binding = run_binding(run)
        if not binding:
            commands._refuse()
        prepared = cards._prepare(context, press, config,
            audience_override={**press, 'audienceId': None, 'audienceDescription': None})
        brief = cards._approve(context, user, config, prepared)
        return {'data': cards._start_data(prepared['topic'], brief, client_request_id=client_key, expected=0),
            'switch': {'run_pk': run.pk, 'run_id': run.run_id, 'binding': binding}}, None

    # Namespacing prevents a start and a switch using the same HTTP key from
    # sharing a receipt, while retaining the existing transaction recheck.
    claim, replay = commands.claim(context, user, 'switch:' + original_key,
        {'operation': 'switch_topic', 'request': data}, prepare)
    if replay is not None:
        return 200, replay
    result = None
    try:
        _stop_original(context, user, claim)
        with _locked_claim(context, user, claim) as (config, _, current):
            _source(context, current['payload'])
            entitlement = entitlement_state(context.organization, user, config=config)
            if not entitlement.get('free'):
                commands._refuse('not_available')
        with commands.dispatch_claim(context, user, claim):
            result = cards._dispatch(context, user, {}, {}, entitlement, attempt_source='switch_topic',
                command_data=claim[1]['payload']['data'])
        return result
    finally:
        commands.finish(context, claim, result[1] if result and result[0] in {200, 201} else None)
