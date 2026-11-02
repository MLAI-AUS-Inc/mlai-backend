"""Emit server-proved journey milestones only after their source writes commit."""
from functools import wraps
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .founder_funnel import ARTICLE_WORKFLOWS, RESEARCH_WORKFLOWS, draft_time, emit_event, mapping, timestamp, remember_first_draft

logger = logging.getLogger(__name__)


def after_commit(callback, *, using):
    """Observability must never break or roll back the founder's action."""
    from django.conf import settings
    if not getattr(settings, 'MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED', False):
        return
    @wraps(callback)
    def safe():
        try:
            callback()
        except Exception:  # noqa: BLE001 - telemetry is a non-authoritative observer
            logger.exception('founder_journey_event_observation_failed')
    transaction.on_commit(safe, using=using)


def emit_for_organization(kind, organization_id, *, at=None, run_id=None, source, using='default'):
    """Attribute startup events only to recorded founder accounts, without PII."""
    from founder_tools.models import VibeRaisingCompany
    if kind == 'draft_ready':
        remember_first_draft(organization_id, run_id=run_id, at=at, using=using)
    rows = VibeRaisingCompany.objects.using(using).filter(organization_id=organization_id).values(
        'profile__user_id', 'profile__user__date_joined', 'profile__user__is_staff').distinct()
    for row in rows:
        if not row['profile__user__is_staff']:
            emit_event(kind, user_id=row['profile__user_id'], organization_id=organization_id,
                run_id=run_id, at=at, signed_up_at=row['profile__user__date_joined'], source=source)


@receiver(post_save, sender='core.User', dispatch_uid='founder_funnel_signup')
def signed_up(sender, instance, created, raw=False, using='default', **kwargs):
    if created and not raw and not instance.is_staff:
        values = dict(user_id=instance.pk, at=instance.date_joined, signed_up_at=instance.date_joined, source='account.created')
        after_commit(lambda: emit_event('signed_up', **values), using=using)


@receiver(post_save, sender='founder_tools.VibeRaisingCompany', dispatch_uid='founder_funnel_startup')
def startup_created(sender, instance, created, raw=False, using='default', **kwargs):
    if created and not raw and instance.organization_id:
        org_id, at = instance.organization_id, instance.created_at
        after_commit(lambda: emit_for_organization('startup_created', org_id, at=at,
            source='startup.created', using=using), using=using)


@receiver(post_save, sender='workflow_runs.ContentFactoryRun', dispatch_uid='founder_funnel_run')
def run_observed(sender, instance, created, raw=False, using='default', **kwargs):
    if raw or not instance.organization_id:
        return
    from django.conf import settings
    if not getattr(settings, 'MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED', False):
        return
    org_id, run_id = instance.organization_id, instance.run_id
    milestones = []
    if created and instance.workflow in ARTICLE_WORKFLOWS:
        milestones.append(('queued', instance.created_at, 'article.admitted'))
        # Revisions/retries are not another first article start.
        first = sender.objects.using(using).filter(organization_id=org_id,
            workflow__in=ARTICLE_WORKFLOWS).order_by('created_at', 'pk').values_list('pk', flat=True).first()
        if first == instance.pk:
            milestones.append(('first_article_started', instance.created_at, 'article.admitted'))
    ready = draft_time(instance) if instance.workflow in ARTICLE_WORKFLOWS else None
    if ready:
        milestones.append(('draft_ready', ready, 'writer.draft_ready_v1'))
    elif instance.workflow in ARTICLE_WORKFLOWS and instance.status in {'completed', 'awaiting_approval'}:
        # Legacy delivery is known, but its historic timestamp is not.
        from .run_reconciliation import authoritative_run
        if not authoritative_run(instance):
            after_commit(lambda: remember_first_draft(org_id, run_id=run_id, using=using), using=using)
    if instance.workflow in RESEARCH_WORKFLOWS and instance.status == 'completed':
        milestones.append(('research_done', timestamp(mapping(instance.result).get('completed_at')) or timezone.now(),
                           'research.completed_observed'))
    for kind, at, source in milestones:
        after_commit(lambda kind=kind, at=at, source=source: emit_for_organization(kind, org_id,
            at=at, run_id=run_id, source=source, using=using), using=using)


@receiver(post_save, sender='workflow_runs.ContentFactoryRunStep', dispatch_uid='founder_funnel_package')
def package_ready(sender, instance, raw=False, using='default', **kwargs):
    if raw or instance.step_key != 'package_content_delivery' or instance.status != 'completed' or not instance.completed_at:
        return
    run, at = instance.run, instance.completed_at
    from .run_reconciliation import authoritative_run
    if authoritative_run(run):
        return
    if run.organization_id and run.workflow in ARTICLE_WORKFLOWS and run.status not in {'cancelled', 'denied'}:
        after_commit(lambda: emit_for_organization('draft_ready', run.organization_id,
            at=at, run_id=run.run_id, source='writer.package_content_delivery', using=using), using=using)


@receiver(post_save, sender='content_factory.WrittenArticle', dispatch_uid='founder_funnel_published')
def published(sender, instance, raw=False, using='default', **kwargs):
    if not raw and instance.publish_status == 'live' and instance.live_verified_at:
        org_id, at, run_id = instance.organization_id, instance.live_verified_at, instance.source_run_id
        after_commit(lambda: emit_for_organization('published', org_id, at=at, run_id=run_id,
            source='article.live_verified', using=using), using=using)


@receiver(post_save, sender='content_factory.WebsiteConnection', dispatch_uid='founder_funnel_github')
def github_upgraded(sender, instance, raw=False, using='default', **kwargs):
    if not raw and instance.state == 'connected' and instance.last_verified_at and instance.github_repo:
        org_id, at = instance.organization_id, instance.last_verified_at
        after_commit(lambda: emit_for_organization('github_upgraded', org_id, at=at,
            source='website.connection_verified', using=using), using=using)
