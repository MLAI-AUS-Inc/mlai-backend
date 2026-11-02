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


def _delivered_source(run):
    if is_delivered_content_only(run):
        return True
    result = _mapping(run.result)
    if not _mapping(result.get("channel_v1")).get("waiting_to_publish"):
        return False
    from .incident_guards import delivered_content
    return delivered_content(run)


def publish_offer(run):
    """Return the once-per-draft publish offer only for an eligible P2 startup."""
    if (not getattr(settings, 'ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED', False) or run.organization_id is None
            or not p2_foundation.p2_applies(run.organization) or not _delivered_source(run)):
        return None
    if _mapping(run.result).get("publish_reviewed_v1"):
        return None
    receipt = _mapping(_mapping(run.result).get('publish_offer_v1'))
    if receipt.get('dismissed_at') or receipt.get('run_id'):
        return None
    destination = _destination(run.organization)
    if destination is None:
        return None
    config, connection, site = destination
    if site is not None and site.publications.filter(run_id=run.run_id, status="live").exists():
        return None
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
    if active_site(source.organization) is not None and getattr(settings, "HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW", False):
        if (review.get("previewPending") or not review.get("revision") or review.get("previewedRevision") != review.get("revision")):
            raise PublishReviewedError("preview_required", "Open the rendered preview of this version before publishing.")
    latest = _latest_review_ready_component_revision(source, context)
    export = article_export(review, run_id=source.run_id, latest_run_id=latest.run_id if latest else None,
                            allow_preview_pending=True, copy_formats=True)
    if export.get('status') != 'ready':
        raise PublishReviewedError('not_offered', 'Open the latest saved draft before publishing it.')
    if claim_count(source, review) != 0:
        raise PublishReviewedError('claims_open', 'Check the highlighted passages before publishing this article.')
    body = export['markdown'].replace('\r\n', '\n').replace('\r', '\n').strip()
    previewed = review.get('previewedRevision') if not review.get('previewPending') else None
    return body, export.get('revision'), previewed


def _dispatch(source, run, *, retry=False):
    try:
        _send_dispatch(source, run, retry=retry)
    except PublishReviewedError:
        raise
    except Exception:
        logger.warning('publish_reviewed_dispatch_pending run_id=%s', run.run_id, exc_info=True)
        raise PublishReviewedError('not_available', 'Publishing is still being confirmed. Please try again shortly.', 503) from None


def _send_dispatch(source, run, *, retry=False):
    from .run_ownership import owner_receipt, reserve_admitted_run
    admitted = owner_receipt(run)
    if admitted is not None:
        reserve_admitted_run(organization=source.organization, principal=admitted['identity']['principal'],
            workflow=run.workflow, payload=run.run_request, reserved_run_id=run.run_id)
    if _mapping(run.run_request).get("hosted_site_id"):
        from .hosted_auto_register import publish_via_hosted
        receipt = publish_via_hosted(run, retry=retry)
        if receipt is None:
            raise PublishReviewedError("not_offered", "The hosted website is not active.")
        with transaction.atomic():
            current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            ContentFactoryRun.objects.filter(pk=run.pk).update(result={**_mapping(current.result),
                "publish_dispatch_v1": {"accepted_at": timezone.now().isoformat(), "hosted": True}})
        return
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    config = _content_factory_remote_config()
    if not config['enabled']:
        raise PublishReviewedError('not_available', 'Publishing is not available just now.', 503)
    require_unlocked_remote_call()
    response = http_client.post(f"{config['base_url']}/api/runs/{source.run_id}/publish-reviewed-draft",
                               json={**run.run_request, **({'publish_retry': True} if retry else {})}, headers=_content_factory_headers(), timeout=(3, 30))
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
            from .run_ownership import owner_receipt
            if owner_receipt(run) is not None:
                retry_publish(run)
            else:
                _dispatch(source_run, run)
        return run, False
    if publish_offer(source_run) is None:
        raise PublishReviewedError('not_offered', 'This draft is not ready to publish to your website yet.')
    carry = _provider('content_factory.no_delivery_refunds', 'carry_charge')
    if carry is None:
        raise PublishReviewedError('not_available', 'Publishing is not available yet.', 503)
    body, revision, previewed = _read_draft(context, source_run)  # Provider read before locks.
    destination = _destination(organization)
    if destination is None or (destination[1] is None and destination[2] is None):
        raise PublishReviewedError('not_offered', 'Connect your website before publishing this draft.')
    _, connection, site = destination
    from .website_connections import contract_for
    binding = contract_for(connection) if site is None else {"hosted_site_id": str(site.pk)}
    if site is None:
        target = connection.targets.filter(target_key=destination[0].default_publish_target_id,
            generation=connection.generation, source_sha=connection.verified_sha, verified_at__isnull=False).first()
        if target is None:
            raise PublishReviewedError('not_offered', 'The publishing target changed.')
        binding['connection_target_id'] = str(target.pk)
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
            if locked.updated_at != observed_at or receipt.get('dismissed_at') or not _delivered_source(locked):
                raise PublishReviewedError('not_offered', 'Your draft changed. Open the latest draft before publishing.')
            prior = _mapping(locked.run_request).get('article_publish_approval_receipt')
            approval = {'version': 1, 'source': 'publish_reviewed', 'approved_by_user_id': user.pk,
                        'approved_at': now, 'approved_body_sha256': digest, 'claims_open': 0,
                        'rendered_preview_revision': previewed,
                        'review_identity': {key: prior.get(key) for key in ('run_id', 'run_generation', 'preview_url',
                            'commit_sha', 'resume_generation', 'quality_inputs_sha256')} if isinstance(prior, dict) else None,
                        'prior_approval': prior}
            request = {**binding, 'run_id': run_id, 'new_run_id': run_id, 'source_run_id': locked.run_id,
                       'workflow': 'direct_generate', 'delivery_mode': 'review_draft', 'domain': locked.domain,
                       'github_repo': connection.github_repo if site is None else '', 'expected_source_sha': connection.verified_sha if site is None else '',
                       'approved_body': body, 'approved_body_sha256': digest, 'body_format': 'markdown',
                       'approval': {'by_user_id': user.pk, 'at': now}, 'merge_policy': 'backend_worker',
                       'self_heal_policy_v1': {'version': 1, 'cohort': 'p2', 'failure_policy': 'v2'},
                       'source_revision': revision}
            if site is not None:
                current_site = type(site).objects.select_for_update().get(pk=site.pk, organization=organization)
                if current_site.state != 'active':
                    raise PublishReviewedError('not_offered', 'Your hosted website is not active.')
                request['delivery_mode'] = 'content_only'
            run = ContentFactoryRun.objects.create(run_id=run_id, organization=organization, domain=locked.domain,
                github_repo=connection.github_repo if site is None else '', workflow='direct_generate', status='queued', run_request=request,
                result={'article_approval_v1': approval, 'publish_reviewed_v1': {'source_run_id': locked.run_id, 'approved_body_sha256': digest}})
            try:
                carry(source_run=locked, new_run=run)
            except ValueError:
                raise PublishReviewedError('not_allowed', 'This draft has no eligible article charge to carry forward.') from None
            from .run_ownership import RunOwnershipConflict, seal_new_run_admission, user_principal
            from core.actor_ids import preferred_actor_id_for_user
            try:
                seal_new_run_admission(run, created=True,
                    principal=user_principal(user.pk, preferred_actor_id_for_user(user)))
            except RunOwnershipConflict:
                raise PublishReviewedError('not_allowed', 'The saved publishing admission changed. Open the draft again.') from None
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


def retry_publish(run):
    """Retry only the retained approved body and destination; never read or generate a new draft."""
    from django.contrib.auth import get_user_model
    run.refresh_from_db()
    if (not getattr(settings, 'ARTICLES_PUBLISH_REVIEWED_DRAFT_ENABLED', False)
            or run.organization_id is None or not p2_foundation.p2_applies(run.organization)):
        raise PublishReviewedError('not_offered', 'Publishing this draft is not available yet.')
    result, request = _mapping(run.result), _mapping(run.run_request)
    approval = _mapping(result.get('article_approval_v1'))
    body = request.get('approved_body')
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest() if isinstance(body, str) else None
    user = get_user_model().objects.filter(pk=approval.get('approved_by_user_id'), is_active=True).first()
    if user is None or not may_publish(user, run.organization):
        raise PublishReviewedError('not_allowed', 'Publishing permission changed.', 403)
    marker = _mapping(result.get('publish_reviewed_v1'))
    source_id = request.get('source_run_id')
    if (approval.get('source') != 'publish_reviewed' or type(approval.get('claims_open')) is not int or approval['claims_open'] != 0
            or _mapping(request.get('approval')).get('by_user_id') != approval.get('approved_by_user_id')
            or _mapping(request.get('approval')).get('at') != approval.get('approved_at')
            or not source_id or marker.get('source_run_id') != source_id
            or digest is None or digest != request.get('approved_body_sha256') or digest != approval.get('approved_body_sha256')
            or digest != marker.get('approved_body_sha256') or request.get('domain') != run.organization.domain
            or request.get('new_run_id') != run.run_id or request.get('run_id') != run.run_id):
        raise PublishReviewedError('not_allowed', 'The saved publishing approval is not available.')
    source = ContentFactoryRun.objects.filter(run_id=source_id, organization_id=run.organization_id).first()
    if source is None:
        raise PublishReviewedError('not_found', 'Not found.', 404)
    from .run_ownership import owner_receipt
    if owner_receipt(run) is not None:
        from types import SimpleNamespace
        current_body, revision, _ = _read_draft(SimpleNamespace(organization=run.organization), source)
        if current_body != body or revision != request.get('source_revision'):
            raise PublishReviewedError('not_offered', 'The approved draft changed. Open its latest version before publishing.')
    destination = _destination(run.organization)
    if destination is None:
        raise PublishReviewedError('not_offered', 'Your publishing destination changed.')
    config, connection, site = destination
    if request.get('hosted_site_id'):
        if getattr(settings, 'HOSTED_PUBLISH_REQUIRES_RENDERED_PREVIEW', False) and approval.get('rendered_preview_revision') != request.get('source_revision'):
            raise PublishReviewedError('preview_required', 'Open the rendered preview of this approved version before retrying.')
        if site is None or str(site.pk) != request['hosted_site_id'] or site.domain != request.get('domain') or request.get('github_repo'):
            raise PublishReviewedError('not_offered', 'Your hosted publishing destination changed.')
    else:
        if site is not None or connection is None:
            raise PublishReviewedError('not_offered', 'Your publishing destination changed.')
        from .website_connections import contract_for
        current = contract_for(connection)
        target = connection.targets.filter(target_key=config.default_publish_target_id, generation=connection.generation,
            source_sha=connection.verified_sha, verified_at__isnull=False).first()
        if target is None or request.get('connection_target_id') != str(target.pk):
            raise PublishReviewedError('not_offered', 'The publishing target changed.')
        if (any(request.get(key) != value for key, value in current.items())
                or request.get('expected_source_sha') != connection.verified_sha or request.get('github_repo') != connection.github_repo):
            raise PublishReviewedError('not_offered', 'Your website changed after this publishing approval.')
    _dispatch(source, run, retry=True)
    return {'status': 'queued', 'run_id': run.run_id, 'source_run_id': source.run_id}


def published_child_for_source(source_run):
    """Resolve the backend's source-bound publish child for journey/live projections."""
    marker = _mapping(_mapping(source_run.result).get('publish_offer_v1'))
    child_id = marker.get('run_id')
    if not child_id or source_run.organization_id is None:
        return None
    child = ContentFactoryRun.objects.filter(run_id=child_id, organization_id=source_run.organization_id).first()
    if child is None:
        return None
    approval = _mapping(_mapping(child.result).get('article_approval_v1'))
    identity = _mapping(_mapping(child.result).get('publish_reviewed_v1'))
    request = _mapping(child.run_request)
    digest = approval.get('approved_body_sha256')
    if (not isinstance(digest, str) or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest)
            or approval.get('source') != 'publish_reviewed' or request.get('source_run_id') != source_run.run_id
            or identity.get('source_run_id') != source_run.run_id
            or approval.get('approved_body_sha256') != identity.get('approved_body_sha256')
            or request.get('approved_body_sha256') != approval.get('approved_body_sha256')):
        return None
    return child


def sync_published_article(run):
    """Project attested child deployment onto the original article without changing its source identity."""
    from urllib.parse import urlsplit
    from .preview_domain_claim import public_host
    from .models import WrittenArticle, ArticlePublishStatus
    request, result = _mapping(run.run_request), _mapping(run.result)
    source = ContentFactoryRun.objects.filter(run_id=request.get('source_run_id'), organization_id=run.organization_id).first()
    if source is None or getattr(published_child_for_source(source), 'pk', None) != run.pk:
        return None
    url = _mapping(result.get('deploy_v1')).get('url')
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
    except ValueError:
        parsed = None
    if (parsed is None or parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment
            or public_host(url) != public_host(source.organization.domain)):
        return None
    article = WrittenArticle.objects.filter(organization_id=source.organization_id, source_run_id=source.run_id).order_by('-created_at').first()
    if article is None:
        return None
    now = timezone.now()
    article.publish_status = ArticlePublishStatus.LIVE
    article.article_url = article.live_url = article.canonical_url = url
    article.canonical_path = parsed.path
    article.live_checked_at = article.live_verified_at = now
    article.published_at = article.published_at or now
    article.save(update_fields=['publish_status', 'article_url', 'live_url', 'canonical_url', 'canonical_path',
        'live_checked_at', 'live_verified_at', 'published_at'])
    return article
