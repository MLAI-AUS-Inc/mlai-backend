import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from integrations.models import ExternalFinancialRecord, ExternalServiceProvider
from startup_updates.models import (
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectUpdateArtifact,
    LumaEventSelection,
    SlackThreadArtifact,
)

from .models import MemoryProvider
from .provider_events import schedule_artifact_wake
from .wake_control import artifact_wakes_suppressed


logger = logging.getLogger(__name__)


def _defer_wake(provider: str, instance, external_scope_id: str) -> None:
    if artifact_wakes_suppressed():
        return
    try:
        if provider == MemoryProvider.GMAIL:
            connection = getattr(instance, "google_connection", None)
            if connection is None:
                connection = getattr(getattr(instance, "message_artifact", None), "google_connection", None)
            account_id = str(getattr(connection, "google_email", "") or "")
        else:
            account_id = str(getattr(instance.connection, "external_account_id", "") or "")
    except ObjectDoesNotExist:
        return
    scope_id = str(external_scope_id or "")

    def schedule():
        try:
            schedule_artifact_wake(
                provider=provider,
                external_account_id=account_id,
                external_scope_id=scope_id,
            )
        except Exception:
            logger.exception(
                "Unable to schedule organisational-memory artifact wake",
                extra={"provider": provider, "external_scope_id": scope_id},
            )

    transaction.on_commit(
        schedule
    )


def _linear_project_id(instance) -> str:
    if isinstance(instance, LinearProjectArtifact):
        return str(instance.linear_project_id or "")
    project = getattr(instance, "project", None)
    return str(getattr(project, "linear_project_id", "") or "")


@receiver(
    (post_save, post_delete),
    sender=SlackThreadArtifact,
    dispatch_uid="org_memory_slack_artifact_wake",
)
def slack_artifact_changed(sender, instance, **kwargs):
    _defer_wake(MemoryProvider.SLACK, instance, instance.channel_id)


def gmail_artifact_changed(sender, instance, **kwargs):
    _defer_wake(MemoryProvider.GMAIL, instance, "")


@receiver(
    (post_save, post_delete),
    sender=ExternalFinancialRecord,
    dispatch_uid="org_memory_finance_artifact_wake",
)
def finance_artifact_changed(sender, instance, **kwargs):
    if instance.provider in {
        ExternalServiceProvider.STRIPE,
        ExternalServiceProvider.XERO,
    }:
        _defer_wake(str(instance.provider), instance, "")


@receiver(
    (post_save, post_delete),
    sender=LumaEventSelection,
    dispatch_uid="org_memory_luma_event_selection_wake",
)
def luma_event_selection_changed(sender, instance, **kwargs):
    _defer_wake(MemoryProvider.LUMA, instance, instance.event_id)


def _connect_linear_signal(signal, sender, uid):
    signal.connect(
        linear_artifact_changed,
        sender=sender,
        dispatch_uid=uid,
        weak=False,
    )


def linear_artifact_changed(sender, instance, **kwargs):
    _defer_wake(MemoryProvider.LINEAR, instance, _linear_project_id(instance))


for _model, _label in (
    (LinearProjectArtifact, "project"),
    (LinearIssueArtifact, "issue"),
    (LinearProjectUpdateArtifact, "project_update"),
):
    _connect_linear_signal(
        post_save,
        _model,
        f"org_memory_linear_{_label}_save_wake",
    )
    _connect_linear_signal(
        post_delete,
        _model,
        f"org_memory_linear_{_label}_delete_wake",
    )


for _model, _label in (
    (GmailMessageArtifact, "message"),
    (GmailAttachmentArtifact, "attachment"),
):
    post_save.connect(
        gmail_artifact_changed,
        sender=_model,
        dispatch_uid=f"org_memory_gmail_{_label}_save_wake",
        weak=False,
    )
    post_delete.connect(
        gmail_artifact_changed,
        sender=_model,
        dispatch_uid=f"org_memory_gmail_{_label}_delete_wake",
        weak=False,
    )
