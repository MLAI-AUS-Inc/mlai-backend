"""Publish an explicitly approved saved draft without another generation or debit."""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from . import p2_foundation
from .founder_draft_views import is_delivered_content_only
from .founder_journey_cohort import read_marker
from .hosted_articles import active_site
from .models import OrganizationContentConfig

logger = logging.getLogger(__name__)


class PublishReviewedError(ValueError):
    """A closed founder-safe refusal; provider text never becomes user copy."""

    def __init__(self, reason, detail, status=409):
        self.reason, self.detail, self.status = reason, detail, status
        super().__init__(detail)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _provider(module, name):
    try:
        return getattr(importlib.import_module(module), name, None)
    except ImportError:
        return None


def may_publish(user, organization):
    """Recheck the owner or a current member's publish right on every click."""
    from founder_tools.services import organization_owner_user_id
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return False
    if organization_owner_user_id(organization) == user.pk:
        return True
    fn = _provider('founder_tools.startup_team', 'member_may_publish')
    if fn is None:
        return False
    try:
        return fn(user, organization) is True
    except Exception:
        logger.warning('publish_reviewed_rights_unavailable organization_id=%s', organization.pk)
        return False


def _destination(organization):
    site = active_site(organization)
    config = OrganizationContentConfig.objects.select_related('website_connection').filter(organization=organization).first()
    connection = config.website_connection if config else None
    if site is not None:
        return config, connection, site
    marker = read_marker(organization, config=config)
    opted_in = _mapping(marker).get('publishAutomatically')
    if (not isinstance(opted_in, dict) or not opted_in.get('enabledAt') or connection is None
            or connection.state != 'connected' or not connection.verified_sha
            or not (connection.capabilities or {}).get('publishingReady')):
        return None
    target = connection.targets.filter(target_key=config.default_publish_target_id, generation=connection.generation,
        source_sha=connection.verified_sha, verified_at__isnull=False).first()
    if target is None or not _mapping(target.capabilities).get('publishingReady'):
        return None
    return config, connection, None


def publish_offer(run):
    """Return the once-per-draft publish offer only for an eligible P2 startup."""
    if (not getattr(settings, 'ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED', False) or run.organization_id is None
            or not p2_foundation.p2_applies(run.organization) or not is_delivered_content_only(run)):
        return None
    receipt = _mapping(_mapping(run.result).get('publish_offer_v1'))
    if receipt.get('dismissed_at') or receipt.get('run_id'):
        return None
    destination = _destination(run.organization)
    if destination is None:
        return None
    config, connection, site = destination
    from .founder_draft_views import posted_record
    posted = posted_record(config, run.run_id) if config else {}
    posted_link = _mapping(posted.get('postedLink'))
    url = posted_link.get('url') or posted.get('url')
    try:
        posted_host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        posted_host = None
    return {'runId': run.run_id, 'blogHost': site.domain if site else run.organization.domain, 'postedHost': posted_host}


def claim_count(run, review):
    """Count canonical claim findings; unknown review state is never zero."""
    result = _mapping(run.result)
    strict = _mapping(result.get('strict_pass_v1'))
    strict_count = strict.get('claims')
    if strict_count is not None and (type(strict_count) is not int or strict_count < 0):
        return None
    if strict_count:
        return strict_count
    findings, known = [], strict_count == 0
    for source in [result, _mapping(review), *(_mapping(review).get('fields') or [])]:
        if not isinstance(source, dict) or 'reviewFindings' not in source:
            continue
        value = source['reviewFindings']
        if (not isinstance(value, list)
                or any(not isinstance(item, dict) or item.get('kind') not in {'claim', 'style'} for item in value)):
            return None
        known = True
        findings.extend(item for item in value if item.get('kind') == 'claim')
    if not known:
        return None
    return len({json.dumps(item, sort_keys=True, default=str) for item in findings})


def _read_draft(context, source):
    from .article_review_views import remote_review
    from .article_export import article_export
    from .vibe_marketing_views import _latest_review_ready_component_revision
    from rest_framework.response import Response
    review = remote_review(source)
    if isinstance(review, Response):
        raise PublishReviewedError('not_available', 'Your saved draft cannot be read just now.', 503)
    latest = _latest_review_ready_component_revision(source, context)
    export = article_export(review, run_id=source.run_id, latest_run_id=latest.run_id if latest else None,
                            allow_preview_pending=True, copy_formats=True)
    if export.get('status') != 'ready':
        raise PublishReviewedError('not_offered', 'Open the latest saved draft before publishing it.')
    if claim_count(source, review) != 0:
        raise PublishReviewedError('claims_open', 'Check the highlighted passages before publishing this article.')
    body = export['markdown'].replace('\r\n', '\n').replace('\r', '\n').strip()
    return body, export.get('revision')


def _dispatch(source, run):
    try:
        _send_dispatch(source, run)
    except PublishReviewedError:
        raise
    except Exception:
        logger.warning('publish_reviewed_dispatch_pending run_id=%s', run.run_id, exc_info=True)
        raise PublishReviewedError('not_available', 'Publishing is still being confirmed. Please try again shortly.', 503) from None


def _send_dispatch(source, run):
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    config = _content_factory_remote_config()
    if not config['enabled']:
        raise PublishReviewedError('not_available', 'Publishing is not available just now.', 503)
    require_unlocked_remote_call()
    response = http_client.post(f"{config['base_url']}/api/runs/{source.run_id}/publish-reviewed-draft",
                               json=run.run_request, headers=_content_factory_headers(), timeout=(3, 30))
    if response.status_code not in {200, 202}:
        raise PublishReviewedError('not_available', 'Your article has not been published. Please try again shortly.', 503)
    value = response.json()
    if not isinstance(value, dict) or value.get('run_id', value.get('new_run_id')) != run.run_id:
        raise PublishReviewedError('not_available', 'Publishing is still being confirmed. Please try again shortly.', 503)
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**_mapping(current.result),
            'publish_dispatch_v1': {'accepted_at': timezone.now().isoformat()}})


def start_publish(*, context, source_run, user):
    """Reserve a deterministic publish continuation, carry its charge, then dispatch."""
    organization = context.organization
    if source_run.organization_id != organization.pk:
        raise PublishReviewedError('not_found', 'Not found.', 404)
    if not may_publish(user, organization):
        raise PublishReviewedError('not_allowed', 'Only someone allowed to publish may add this article to your website.', 403)
    if (not getattr(settings, 'ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED', False)
            or not p2_foundation.p2_applies(organization)):
        raise PublishReviewedError('not_offered', 'Publishing this draft is not available yet.')
    existing_id = _mapping(_mapping(source_run.result).get('publish_offer_v1')).get('run_id')
    if existing_id:
        run = ContentFactoryRun.objects.filter(run_id=existing_id, organization=organization).first()
        if run is None:
            raise PublishReviewedError('already_publishing', 'Publishing this draft is still being confirmed.')
        if not _mapping(run.result).get('publish_dispatch_v1'):
            _dispatch(source_run, run)
        return run, False
    if publish_offer(source_run) is None:
        raise PublishReviewedError('not_offered', 'This draft is not ready to publish to your website yet.')
    carry = _provider('content_factory.no_delivery_refunds', 'carry_charge')
    if carry is None:
        raise PublishReviewedError('not_available', 'Publishing is not available yet.', 503)
    body, revision = _read_draft(context, source_run)  # Provider read before locks.
    destination = _destination(organization)
    if destination is None or destination[1] is None:
        raise PublishReviewedError('not_offered', 'Connect your website before publishing this draft.')
    _, connection, site = destination
    from .website_connections import contract_for
    binding = contract_for(connection)
    observed_at = source_run.updated_at
    now = timezone.now().isoformat()
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    run_id = str(uuid5(NAMESPACE_URL, f'mlai:publish-reviewed:{organization.pk}:{source_run.run_id}'))
    with transaction.atomic():
        locked = ContentFactoryRun.objects.select_for_update().get(pk=source_run.pk)
        if locked.organization_id != organization.pk or not may_publish(user, organization):
            raise PublishReviewedError('not_allowed', 'Publishing permission changed. Please open the draft again.', 403)
        receipt = _mapping(_mapping(locked.result).get('publish_offer_v1'))
        if receipt.get('run_id'):
            run = ContentFactoryRun.objects.get(run_id=receipt['run_id'], organization=organization)
            created = False
        else:
            if locked.updated_at != observed_at or receipt.get('dismissed_at') or not is_delivered_content_only(locked):
                raise PublishReviewedError('not_offered', 'Your draft changed. Open the latest draft before publishing.')
            prior = _mapping(locked.run_request).get('article_publish_approval_receipt')
            approval = {'version': 1, 'source': 'publish_reviewed', 'approved_by_user_id': user.pk,
                        'approved_at': now, 'approved_body_sha256': digest, 'claims_open': 0,
                        'review_identity': {key: prior.get(key) for key in ('run_id', 'run_generation', 'preview_url',
                            'commit_sha', 'resume_generation', 'quality_inputs_sha256')} if isinstance(prior, dict) else None,
                        'prior_approval': prior}
            request = {**binding, 'run_id': run_id, 'new_run_id': run_id, 'source_run_id': locked.run_id,
                       'workflow': 'direct_generate', 'delivery_mode': 'review_draft', 'domain': locked.domain,
                       'github_repo': connection.github_repo, 'expected_source_sha': connection.verified_sha,
                       'approved_body': body, 'approved_body_sha256': digest, 'body_format': 'markdown',
                       'approval': {'by_user_id': user.pk, 'at': now}, 'merge_policy': 'backend_worker',
                       'self_heal_policy_v1': {'version': 1, 'cohort': 'p2', 'failure_policy': 'v2'},
                       'source_revision': revision}
            run = ContentFactoryRun.objects.create(run_id=run_id, organization=organization, domain=locked.domain,
                github_repo=connection.github_repo, workflow='direct_generate', status='queued', run_request=request,
                result={'article_approval_v1': approval, 'publish_reviewed_v1': {'source_run_id': locked.run_id, 'approved_body_sha256': digest}})
            try:
                carry(source_run=locked, new_run=run)
            except ValueError:
                raise PublishReviewedError('not_allowed', 'This draft has no eligible article charge to carry forward.') from None
            ContentFactoryRun.objects.filter(pk=locked.pk).update(result={**_mapping(locked.result),
                'publish_offer_v1': {'run_id': run_id, 'started_at': now, 'body_sha256': digest}})
            created = True
    _dispatch(source_run, run)
    return run, created


def dismiss_offer(*, source_run, user):
    """Dismiss a source's offer under the same current publish permission gate."""
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update(of=('self',)).select_related('organization').get(pk=source_run.pk)
        if (not getattr(settings, 'ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED', False)
                or not p2_foundation.p2_applies(run.organization)):
            raise PublishReviewedError('not_offered', 'Publishing this draft is not available yet.')
        if not may_publish(user, run.organization):
            raise PublishReviewedError('not_allowed', 'You cannot change this article.', 403)
        previous = _mapping(_mapping(run.result).get('publish_offer_v1'))
        if previous.get('run_id'):
            raise PublishReviewedError('already_publishing', 'This article is already being published.')
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**_mapping(run.result),
            'publish_offer_v1': {'dismissed_at': previous.get('dismissed_at') or timezone.now().isoformat()}})
