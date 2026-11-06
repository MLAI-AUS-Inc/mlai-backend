"""Refuse ORM writes that would erase explicitly admitted backend fact history.

Signals guard model save/delete and Django's deletion collector. Bulk SQL fact
writes remain restricted to the reviewed backend fact API and projector.
"""
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from .backend_fact_projection import projection_in_progress
from .models import ContentFactoryJob
from .run_backend_facts import fact_run_for_job
from .run_ownership import owns_backend_facts


class RunFactHistoryProtected(ProtectedError):
    """An ORM mutation would discard canonical backend or original charge facts."""


def _job_is_owned(instance, *, using):
    # Saved identity is checked independently of the proposed job_id. An
    # in-memory rename cannot turn existing compatibility output into input.
    saved_id = (ContentFactoryJob.objects.using(using).filter(pk=instance.pk)
                .values_list("job_id", flat=True).first()) if instance.pk is not None else None
    return any(fact_run_for_job(key) is not None for key in {saved_id, instance.job_id} if key)


@receiver(pre_save, sender=ContentFactoryJob, dispatch_uid="r3_job_fact_save_guard")
@receiver(pre_delete, sender=ContentFactoryJob, dispatch_uid="r3_job_fact_delete_guard")
def protect_job_facts(sender, instance, using, **kwargs):
    """Only canonical projection may write or remove owned compatibility output."""
    if not projection_in_progress() and _job_is_owned(instance, using=using):
        raise RunFactHistoryProtected("Use the canonical Run backend fact API.", [instance])


@receiver(pre_delete, sender="workflow_runs.ContentFactoryRun", dispatch_uid="r3_run_fact_delete_guard")
def protect_run_history(sender, instance, using, **kwargs):
    """Retain admitted canonical history even during compatibility projection."""
    saved = sender.objects.using(using).filter(pk=instance.pk).first()
    if saved is not None and owns_backend_facts(saved):
        raise RunFactHistoryProtected("Canonical Run fact history cannot be deleted.", [instance])


def _owned_parent(run_ids, *, using):
    from workflow_runs.models import ContentFactoryRun
    return any(owns_backend_facts(run) for run in ContentFactoryRun.objects.using(using).filter(
        pk__in={key for key in run_ids if key is not None}))


@receiver(pre_delete, sender="workflow_runs.ContentFactoryRunStep", dispatch_uid="r3_step_fact_delete_guard")
def protect_step_history(sender, instance, using, **kwargs):
    """Retain owned steps using saved parent identity, even after local changes."""
    saved = sender.objects.using(using).filter(pk=instance.pk).values_list("run_id", flat=True).first()
    if _owned_parent({saved, instance.run_id}, using=using):
        raise RunFactHistoryProtected("Canonical Run step history cannot be deleted.", [instance])


@receiver(pre_save, sender="workflow_runs.ContentFactoryRunStep", dispatch_uid="r3_step_fact_reparent_guard")
def protect_step_parent(sender, instance, using, **kwargs):
    """Refuse reparenting which could detach or import existing owned history."""
    saved = sender.objects.using(using).filter(pk=instance.pk).values_list("run_id", flat=True).first()
    if saved is not None and saved != instance.run_id and _owned_parent({saved, instance.run_id}, using=using):
        raise RunFactHistoryProtected("Canonical Run steps cannot change their owner.", [instance])


def _attempt_run_ids(sender, instance, *, using):
    from workflow_runs.models import ContentFactoryRunStep
    saved = sender.objects.using(using).filter(pk=instance.pk).values_list("step_id", flat=True).first()
    steps = {key for key in {saved, instance.step_id} if key is not None}
    return saved, ContentFactoryRunStep.objects.using(using).filter(pk__in=steps).values_list("run_id", flat=True)


@receiver(pre_delete, sender="workflow_runs.ContentFactoryRunStepAttempt", dispatch_uid="r3_attempt_fact_delete_guard")
def protect_attempt_history(sender, instance, using, **kwargs):
    """Keep owned immutable attempts before either direct or collector deletion."""
    _, run_ids = _attempt_run_ids(sender, instance, using=using)
    if _owned_parent(run_ids, using=using):
        raise RunFactHistoryProtected("Canonical Run attempt history cannot be deleted.", [instance])


@receiver(pre_save, sender="workflow_runs.ContentFactoryRunStepAttempt", dispatch_uid="r3_attempt_fact_reparent_guard")
def protect_attempt_parent(sender, instance, using, **kwargs):
    """Keep actual attempt identity attached to its original owned step."""
    saved, run_ids = _attempt_run_ids(sender, instance, using=using)
    if saved is not None and saved != instance.step_id and _owned_parent(run_ids, using=using):
        raise RunFactHistoryProtected("Canonical Run attempts cannot change their owner.", [instance])


@receiver(pre_delete, sender="roo.Ledger", dispatch_uid="r3_ledger_fact_delete_guard")
def protect_charge_history(sender, instance, using, **kwargs):
    """Retain pinned original spends and their refunds before collector SET_NULL."""
    from workflow_runs.models import ContentFactoryRun

    saved = sender.objects.using(using).filter(pk=instance.pk).values("id", "refund_of_id").first()
    if saved is None:
        return
    keys = {saved["id"], saved["refund_of_id"]} - {None}
    pinned = Q()
    for key in keys:
        pinned |= (Q(billing_ledger_id=key)
            | Q(run_request__roo_points_ledger_id=key)
            | Q(run_request__roo_points_ledger_id=str(key))
            | Q(result__run_owner_article_charge_v1__ledger_id=key))
    candidates = ContentFactoryRun.objects.using(using).filter(
        result__run_owner_v1__authority__backend_facts=True).filter(pinned)
    if any(owns_backend_facts(run) for run in candidates.iterator(chunk_size=100)):
        raise RunFactHistoryProtected("Original Run charge and refund history cannot be deleted.", [instance])
