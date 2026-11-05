"""Resumable retirement of legacy founder blogs, preserving URLs and publication history."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .hosted_article_models import HostedArticleSite, HostedArticlePublication
from .website_live_fetch import fetch_live_route


def _save(site, receipt, **fields):
    with transaction.atomic():
        current = HostedArticleSite.objects.select_for_update().get(pk=site.pk)
        previous = current.profile.get('retirement') or {}
        if previous.get('started_at') and previous.get('deadline_at'):
            receipt.update(started_at=previous['started_at'], deadline_at=previous['deadline_at'])
        current.profile = {**current.profile, 'retirement': deepcopy(receipt)}
        for name, value in fields.items():
            setattr(current, name, value)
        current.save(update_fields=['profile', *fields, 'updated_at'])
        site.profile, site.state = current.profile, current.state


def _remote(site, action, **fields):
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    remote = _content_factory_remote_config()
    if not remote['enabled']:
        raise ValueError('retirement_service_unavailable')
    response = http_client.post(f"{remote['base_url']}/api/hosted-sites/{site.pk}/retire",
        json={'action': action, 'domain': site.domain, **fields}, headers=_content_factory_headers(), timeout=(3, 60))
    if response.status_code not in {200, 202}:
        reason = response.json().get('reason') if response.status_code == 409 else None
        error = ValueError(reason if reason == 'proxy_modified' else 'retirement_service_unavailable')
        error.instructions = response.json().get('instructions') if reason == 'proxy_modified' else None
        raise error
    result = response.json()
    if not isinstance(result, dict):
        raise ValueError('retirement_service_unavailable')
    return result


def _founder_serves(url, domain):
    try:
        _, headers = fetch_live_route(url, domain)
    except ValueError:
        return False
    return headers.get('x-mlai-hosted') != '1'


def _alert(site, receipt, slot):
    from .founder_journey_alerts import queue_founder_journey_alert
    # URLs and exact removal instructions remain in the private site receipt;
    # the alert system intentionally carries only identifiers.
    return queue_founder_journey_alert(organization=site.organization, alert_reason='hosting_retired',
        slot=f'{site.pk}-{slot}', details={'site_id': str(site.pk), 'article_count': len(receipt['articles'])})


def _repository_delivery_merged(site, article):
    """An old hosted LIVE status alone is never proof of a new repository merge."""
    from .models import WrittenArticle
    from workflow_runs.models import ContentFactoryRun
    if article.get('delivery') != 'repository' or not article.get('publish_run_id'):
        return False
    child = ContentFactoryRun.objects.filter(organization=site.organization, run_id=article['publish_run_id']).first()
    result = getattr(child, 'result', None) or {}
    request = getattr(child, 'run_request', None) or {}
    merged, pull = result.get('merge_v1') or {}, result.get('publish_pr_v1') or {}
    if (request.get('hosted_site_id') or result.get('merge_status') != 'merged' or not merged.get('merged_at')
            or not pull.get('head_sha') or merged.get('head_sha') != pull.get('head_sha')):
        return False
    return WrittenArticle.objects.filter(organization=site.organization, pk=article.get('written_article_id'),
        slug=article['slug'], publish_status__in=['merged', 'live']).exists()


def _start_prepare(site, config):
    """Use normal setup admission; never manufacture missing founder consent."""
    from founder_tools.models import VibeRaisingCompany
    from .website_prepare import start_prepare
    from .publish_reviewed_draft import may_publish
    connection = config.website_connection
    user = connection.authorized_by
    company = VibeRaisingCompany.objects.filter(organization=site.organization, profile__user=user).first()
    if company is None or not may_publish(user, site.organization):
        raise ValueError('website_prepare_requires_owner')
    return start_prepare(config, company_id=company.pk, user=user)


def retire_site(site, *, dry_run=False, now=None):
    """Advance one site without ever merging founder PRs; dry-run performs no effects."""
    from . import publish_reviewed_draft as publishing
    from .channel_switch import switch_to_copy_ready
    from workflow_runs.models import ContentFactoryRun

    now = now or timezone.now()
    if site.state == HostedArticleSite.State.RETIRED:
        return deepcopy(site.profile.get('retirement', {'status': 'retired'}))
    receipt = deepcopy(site.profile.get('retirement') or {})
    if not receipt:
        receipt = {'version': 1, 'started_at': now.isoformat(), 'deadline_at': (now + timedelta(days=30)).isoformat(),
            'status': 'paused', 'articles': [], 'inventory_complete': False}
    if not dry_run:
        # Freeze before inventory so a final publication cannot slip between
        # the article list and the pause. Incomplete inventory is resumable.
        _save(site, receipt, state=HostedArticleSite.State.PAUSED)
    publications = list(site.publications.filter(Q(status__in=HostedArticlePublication.RENDERED)
        | Q(live_verified_at__isnull=False)).select_related('written_article', 'requested_by').order_by('slug', 'pk'))
    if not receipt.get('inventory_complete'):
        receipt['articles'] = [{'publication_id': str(p.pk), 'slug': p.slug,
            'url': f'https://{site.domain}{site.base_path}{p.slug}/', 'run_id': p.run_id,
            'written_article_id': p.written_article_id} for p in publications]
        receipt['inventory_complete'] = True
        if not dry_run:
            _save(site, receipt)
    if dry_run:
        return {**receipt, 'dry_run': True, 'next': 'freeze_then_deliver_then_proxy_pr_then_verify_then_delete',
            'repository_available': publishing._destination(site.organization) is not None,
            'worker_url': site.worker_url, 'domain': site.domain}
    destination = publishing._destination(site.organization)
    rows = {str(p.pk): p for p in publications}
    expired = now >= timezone.datetime.fromisoformat(receipt['deadline_at'])
    for article in receipt['articles']:
        if article.get('delivery'):
            continue
        publication = rows.get(article['publication_id'])
        if publication is None:
            article['blocked'] = 'publication_missing'
            continue
        source_id = (publication.written_article.source_run_id if publication.written_article else '') or publication.run_id
        source = ContentFactoryRun.objects.filter(organization=site.organization, run_id=source_id).first()
        if source is None:
            article['blocked'] = 'source_run_missing'
            continue
        try:
            if destination is not None:
                child, _ = publishing.start_publish(context=SimpleNamespace(organization=site.organization),
                    source_run=source, user=publication.requested_by, retirement_slug=article['slug'])
                if child.run_request.get('hosted_site_id') or child.run_request.get('retirement_slug') != article['slug']:
                    raise ValueError('fresh_repository_review_required')
                article.update(delivery='repository', publish_run_id=child.run_id)
            else:
                config = publishing.OrganizationContentConfig.objects.filter(organization=site.organization).first()
                connection = config.website_connection if config else None
                if connection is not None and connection.state in {'connected', 'paused'}:
                    # Setup requires the founder's current consent; do not manufacture it.
                    if not receipt.get('prepare_operation_id'):
                        try:
                            operation = _start_prepare(site, config)
                            receipt['prepare_operation_id'] = str(operation.pk)
                        except ValueError:
                            article['blocked'] = 'website_prepare_requires_owner_consent'
                            continue
                    article['blocked'] = 'website_prepare_pending'
                    continue
                switched = switch_to_copy_ready(run=source, reason='hosting_retired')
                if switched.get('channel') != 'copy_ready' or switched.get('dispatch_state') != 'accepted':
                    raise ValueError('copy_ready_pending')
                article['delivery'] = 'copy_ready'
                receipt['alert_id'] = _alert(site, receipt, 'copy-ready')
            article.pop('blocked', None)
        except (ValueError, publishing.PublishReviewedError) as exc:
            article['blocked'] = getattr(exc, 'reason', None) or 'delivery_pending'
        _save(site, receipt)
    # A merged repository article is sufficient to open the proxy-removal PR.
    # It is never sufficient to delete the Worker; live routes are checked again below.
    ready = True
    for article in receipt['articles']:
        served = _founder_serves(article['url'], site.domain)
        merged = _repository_delivery_merged(site, article)
        article['founder_ready'] = served or merged
        ready = ready and article['founder_ready']
    if not ready and not expired:
        receipt['status'] = 'waiting_for_founder_articles'
        receipt['alert_id'] = _alert(site, receipt, 'articles')
        _save(site, receipt)
        return receipt
    proxy = receipt.get('proxy_removal')
    if not isinstance(proxy, dict) or proxy.get('status') != 'proxy_absent':
        binding = {}
        # Removing an existing proxy needs the current connection grant, not
        # a verified article publishing target. This also covers empty sites.
        from .hosted_articles import _source_connection, HostedPublishError
        from .website_connections import contract_for
        try:
            connection = _source_connection(site)
            binding = contract_for(connection)
            binding.update(github_repo=connection.github_repo)
        except HostedPublishError:
            pass
        try:
            # Re-read an outstanding PR through CF's idempotent planner. Only
            # an inspected default branch with no managed proxy proves removal.
            # Keep the original PR identity in the durable audit receipt.
            receipt['proxy_removal'] = {**(proxy if isinstance(proxy, dict) else {}),
                **_remote(site, 'proxy_removal_pr', **binding)}
        except ValueError as exc:
            receipt['status'] = 'proxy_modified' if str(exc) == 'proxy_modified' else 'proxy_removal_pending'
            if getattr(exc, 'instructions', None):
                receipt['manual_removal'] = exc.instructions
            receipt['alert_id'] = _alert(site, receipt, 'proxy')
            _save(site, receipt)
            if not expired:
                return receipt
        _save(site, receipt)
    proxy = receipt.get('proxy_removal')
    if not expired and (not isinstance(proxy, dict) or proxy.get('status') != 'proxy_absent'):
        receipt['status'] = 'waiting_for_proxy_merge'
        _save(site, receipt)
        return receipt
    index_clear = _founder_serves(f'https://{site.domain}{site.base_path}', site.domain)
    articles_clear = all(_founder_serves(a['url'], site.domain) for a in receipt['articles'])
    if not (index_clear and articles_clear) and not expired:
        receipt['status'] = 'waiting_for_proxy_merge'
        _save(site, receipt)
        return receipt
    if expired:
        receipt['grace_expired'] = True
        receipt['alert_id'] = _alert(site, receipt, 'grace-expired')
        _save(site, receipt)
    # CF independently verifies its managed tag and public-route/deadline evidence.
    _remote(site, 'delete_worker')
    receipt.update(status='retired', retired_at=now.isoformat())
    with transaction.atomic():
        _save(site, receipt, state=HostedArticleSite.State.RETIRED, worker_url='')
        site.publications.update(status=HostedArticlePublication.Status.WITHDRAWN)
    return receipt
