from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from integrations import http_client as requests
from integrations.models import ExternalServiceConnection, ExternalServiceProvider, GoogleConnection
from integrations.services.valley_harness import cancel_valley_run
from organizations.models import Organization
from startup_updates.models import (
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailSyncCursor,
    GmailThreadArtifact,
    GoogleAnalyticsPropertySelection,
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
    MonthlyUpdateDraft,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupDataDeletionRequest,
    StartupDataDeletionStatus,
    StartupEvent,
    StartupMetricObservation,
    UserStartupBinding,
)
from startup_updates.services import (
    OPEN_RUN_STATUSES,
    STARTUP_UPDATE_WORKFLOW,
    cancel_startup_update_run,
    get_startup_update_run_google_connection_id,
    gmail_required_for_sources,
    startup_update_run_input_sources,
)
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

logger = logging.getLogger(__name__)

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_PERMISSIONS_URL = "https://myaccount.google.com/permissions"
GMAIL_PROVIDER = "gmail"
STARTUP_PROVIDER = "startup"

DELETED_COUNT_KEYS = (
    "gmailMessages",
    "gmailThreads",
    "gmailAttachments",
    "gmailCursors",
    "slackMessages",
    "slackThreads",
    "slackChannelSelections",
    "linearProjects",
    "linearIssues",
    "linearProjectUpdates",
    "linearProjectSelections",
    "notionRunStores",
    "googleAnalyticsRunStores",
    "googleAnalyticsPropertySelections",
    "externalConnectionCursors",
    "startupRunsScrubbed",
    "startupEvents",
    "startupMetrics",
    "monthlyDrafts",
)


def _zero_deleted_counts() -> dict[str, int]:
    return {key: 0 for key in DELETED_COUNT_KEYS}


def _add_counts(base: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    for key in DELETED_COUNT_KEYS:
        base[key] = int(base.get(key) or 0) + int(extra.get(key) or 0)
    return base


def _delete_count(queryset: QuerySet) -> int:
    count = queryset.count()
    queryset.delete()
    return int(count)


def _request_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _latest_deletion_request(organization: Organization) -> StartupDataDeletionRequest | None:
    return (
        StartupDataDeletionRequest.objects.filter(
            organization=organization,
            delete_derived_data=True,
            status__in=[
                StartupDataDeletionStatus.DELETING,
                StartupDataDeletionStatus.DELETED,
            ],
        )
        .order_by("-updated_at", "-id")
        .first()
    )


def serialize_startup_data_status(organization: Organization) -> dict[str, Any]:
    latest = _latest_deletion_request(organization)
    status = latest.status if latest else "active"
    return {
        "organization_id": organization.id,
        "domain": organization.domain,
        "deletion_status": status,
        "status": status,
        "request_id": latest.request_id if latest else None,
        "provider": latest.provider if latest else None,
        "deleted_counts": latest.deleted_counts if latest else {},
        "warnings": latest.warnings if latest else [],
        "completed_at": latest.completed_at.isoformat() if latest and latest.completed_at else None,
    }


def _create_deletion_request(
    *,
    organization: Organization,
    user,
    provider: str,
    delete_derived_data: bool,
    google_account: str = "",
    reason: str = "",
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> StartupDataDeletionRequest:
    now = timezone.now()
    deletion_request, _created = StartupDataDeletionRequest.objects.update_or_create(
        request_id=request_id or _request_id(f"{provider}-delete"),
        defaults={
            "organization": organization,
            "user": user if getattr(user, "is_authenticated", False) else None,
            "provider": provider,
            "status": StartupDataDeletionStatus.DELETING,
            "delete_derived_data": bool(delete_derived_data),
            "google_account": google_account or "",
            "reason": reason or "",
            "deleted_counts": _zero_deleted_counts(),
            "warnings": [],
            "metadata": metadata or {},
            "started_at": now,
            "completed_at": None,
        },
    )
    return deletion_request


def _complete_deletion_requests(
    deletion_requests: Iterable[StartupDataDeletionRequest],
    *,
    status: str,
    deleted_counts: dict[str, int],
    warnings: list[str],
) -> None:
    now = timezone.now()
    for deletion_request in deletion_requests:
        deletion_request.status = status
        deletion_request.deleted_counts = dict(deleted_counts)
        deletion_request.warnings = list(warnings)
        deletion_request.completed_at = now
        deletion_request.save(update_fields=["status", "deleted_counts", "warnings", "completed_at", "updated_at"])


def _organizations_for_google_connection(user, connection: GoogleConnection) -> list[Organization]:
    organization_ids = set(
        UserStartupBinding.objects.filter(user=user, google_connection=connection).values_list("organization_id", flat=True)
    )
    organization_ids.update(
        GmailSyncCursor.objects.filter(google_connection=connection).values_list("organization_id", flat=True)
    )
    organization_ids.update(
        GmailMessageArtifact.objects.filter(google_connection=connection).values_list("organization_id", flat=True)
    )
    organization_ids.update(
        GmailThreadArtifact.objects.filter(google_connection=connection).values_list("organization_id", flat=True)
    )
    organization_ids.update(
        GmailAttachmentArtifact.objects.filter(message_artifact__google_connection=connection).values_list(
            "organization_id",
            flat=True,
        )
    )
    return list(Organization.objects.filter(id__in=organization_ids).order_by("id"))


def _revoke_google_refresh_token(refresh_token: str) -> dict[str, Any]:
    if not refresh_token:
        return {
            "requested": False,
            "succeeded": False,
            "warning": "No Google refresh token was stored locally.",
        }

    try:
        response = requests.post(
            GOOGLE_REVOKE_URL,
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=(3, 10),
        )
    except requests.RequestException as exc:
        logger.warning("Google token revocation request failed: %s", exc)
        return {
            "requested": True,
            "succeeded": False,
            "warning": "MLAI removed local Gmail access, but Google did not confirm token revocation. Revoke MLAI in Google Account permissions.",
        }

    if 200 <= response.status_code < 300:
        return {"requested": True, "succeeded": True, "warning": None}

    logger.warning("Google token revocation returned status=%s body=%r", response.status_code, response.text[:200])
    return {
        "requested": True,
        "succeeded": False,
        "warning": "MLAI removed local Gmail access, but Google did not confirm token revocation. Revoke MLAI in Google Account permissions.",
    }


def _startup_update_runs_for_organizations(organizations: Iterable[Organization]) -> list[ContentFactoryRun]:
    domains = [organization.domain for organization in organizations if organization.domain]
    if not domains:
        return []

    return list(
        ContentFactoryRun.objects.filter(workflow=STARTUP_UPDATE_WORKFLOW, domain__in=domains).order_by(
            "-updated_at",
            "-id",
        )
    )


def _gmail_runs_for_organizations(
    organizations: Iterable[Organization],
    *,
    google_connection_id: int | None = None,
) -> list[ContentFactoryRun]:
    runs = _startup_update_runs_for_organizations(organizations)
    matched: list[ContentFactoryRun] = []
    for run in runs:
        if "gmail" not in set(startup_update_run_input_sources(run)):
            continue
        run_google_connection_id = get_startup_update_run_google_connection_id(run)
        if google_connection_id is not None and run_google_connection_id not in (None, int(google_connection_id)):
            continue
        matched.append(run)
    return matched


def _cancel_open_startup_runs(runs: Iterable[ContentFactoryRun]) -> list[dict[str, Any]]:
    cancelled_runs: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for run in runs:
        if run.run_id in seen_run_ids or run.status not in OPEN_RUN_STATUSES:
            continue
        seen_run_ids.add(run.run_id)
        cancelled_runs.append(
            {
                "runId": run.run_id,
                "run_id": run.run_id,
                "status": run.status,
                "valley": cancel_valley_run(run.run_id),
            }
        )
    return cancelled_runs


def _cancel_open_gmail_runs(*, user, connection: GoogleConnection, bindings: list[UserStartupBinding]) -> list[dict[str, Any]]:
    cancelled_runs: list[dict[str, Any]] = []
    for binding in bindings:
        organization = binding.organization
        if not organization.domain:
            continue
        runs = ContentFactoryRun.objects.filter(
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=organization.domain,
            status__in=OPEN_RUN_STATUSES,
        ).order_by("-updated_at", "-id")
        for run in runs:
            if not gmail_required_for_sources((run.run_request or {}).get("input_sources")):
                continue
            run_google_connection_id = get_startup_update_run_google_connection_id(run)
            if run_google_connection_id not in (None, connection.id):
                continue

            try:
                cancel_result = cancel_startup_update_run(
                    run_id=run.run_id,
                    organization=organization,
                    binding_id=binding.id,
                    google_connection_id=connection.id,
                    cancelled_by_user_id=user.id,
                )
            except (ContentFactoryRun.DoesNotExist, PermissionError) as exc:
                logger.warning("Skipping Gmail run cancellation for %s: %s", run.run_id, exc)
                continue

            valley_payload = {
                "revoke_requested": False,
                "revoke_succeeded": False,
                "revoked_job_ids": [],
                "missing_job_ids": [],
            }
            if cancel_result.get("cancel_applied"):
                valley_payload = cancel_valley_run(run.run_id)
            cancelled_runs.append(
                {
                    "runId": run.run_id,
                    "run_id": run.run_id,
                    "status": cancel_result["run"].status,
                    "cancelApplied": bool(cancel_result.get("cancel_applied")),
                    "cancel_applied": bool(cancel_result.get("cancel_applied")),
                    "cleanup": cancel_result.get("cleanup") or {},
                    "valley": valley_payload,
                }
            )
    return cancelled_runs


def _delete_gmail_artifacts(
    *,
    organization_ids: list[int] | None = None,
    google_connection_id: int | None = None,
) -> dict[str, int]:
    counts = _zero_deleted_counts()
    message_filter: dict[str, Any] = {}
    thread_filter: dict[str, Any] = {}
    cursor_filter: dict[str, Any] = {}
    attachment_filter: dict[str, Any] = {}
    if organization_ids is not None:
        message_filter["organization_id__in"] = organization_ids
        thread_filter["organization_id__in"] = organization_ids
        cursor_filter["organization_id__in"] = organization_ids
        attachment_filter["organization_id__in"] = organization_ids
    if google_connection_id is not None:
        message_filter["google_connection_id"] = google_connection_id
        thread_filter["google_connection_id"] = google_connection_id
        cursor_filter["google_connection_id"] = google_connection_id
        attachment_filter["message_artifact__google_connection_id"] = google_connection_id

    counts["gmailAttachments"] = _delete_count(GmailAttachmentArtifact.objects.filter(**attachment_filter))
    counts["gmailThreads"] = _delete_count(GmailThreadArtifact.objects.filter(**thread_filter))
    counts["gmailMessages"] = _delete_count(GmailMessageArtifact.objects.filter(**message_filter))
    counts["gmailCursors"] = _delete_count(GmailSyncCursor.objects.filter(**cursor_filter))
    return counts


def _delete_slack_artifacts(*, organization_ids: list[int]) -> dict[str, int]:
    counts = _zero_deleted_counts()
    counts["slackThreads"] = _delete_count(SlackThreadArtifact.objects.filter(organization_id__in=organization_ids))
    counts["slackMessages"] = _delete_count(SlackMessageArtifact.objects.filter(organization_id__in=organization_ids))
    counts["slackChannelSelections"] = _delete_count(
        SlackChannelSelection.objects.filter(organization_id__in=organization_ids)
    )
    return counts


def _delete_linear_artifacts(*, organization_ids: list[int]) -> dict[str, int]:
    counts = _zero_deleted_counts()
    counts["linearProjectUpdates"] = _delete_count(
        LinearProjectUpdateArtifact.objects.filter(organization_id__in=organization_ids)
    )
    counts["linearIssues"] = _delete_count(LinearIssueArtifact.objects.filter(organization_id__in=organization_ids))
    counts["linearProjects"] = _delete_count(LinearProjectArtifact.objects.filter(organization_id__in=organization_ids))
    counts["linearProjectSelections"] = _delete_count(
        LinearProjectSelection.objects.filter(organization_id__in=organization_ids)
    )
    return counts


def _scrub_external_connection_cursors(*, organization_ids: list[int], providers: Iterable[str]) -> dict[str, int]:
    counts = _zero_deleted_counts()
    provider_values = [str(provider) for provider in providers]
    queryset = ExternalServiceConnection.objects.filter(
        organization_id__in=organization_ids,
        provider__in=provider_values,
    )
    for connection in queryset:
        if not connection.sync_cursor and connection.last_synced_at is None:
            continue
        connection.sync_cursor = {}
        connection.last_synced_at = None
        connection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])
        counts["externalConnectionCursors"] += 1
    return counts


def _delete_notion_run_stores(*, organization_ids: list[int], run_ids: list[str] | None = None) -> dict[str, int]:
    counts = _zero_deleted_counts()
    queryset = ExternalServiceConnection.objects.filter(
        organization_id__in=organization_ids,
        provider=ExternalServiceProvider.NOTION,
    )
    run_id_set = set(run_ids or [])
    for connection in queryset:
        cursor = dict(connection.sync_cursor or {})
        run_stores = cursor.get("startup_update_runs")
        if not isinstance(run_stores, dict) or not run_stores:
            continue

        if run_id_set:
            deleted_count = 0
            next_run_stores = dict(run_stores)
            for run_id in run_id_set:
                if run_id in next_run_stores:
                    deleted_count += 1
                    next_run_stores.pop(run_id, None)
        else:
            deleted_count = len(run_stores)
            next_run_stores = {}

        if deleted_count <= 0:
            continue
        if next_run_stores:
            cursor["startup_update_runs"] = next_run_stores
        else:
            cursor.pop("startup_update_runs", None)
            cursor["startup_update_index_partial"] = False
        connection.sync_cursor = cursor
        connection.last_synced_at = None
        connection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])
        counts["notionRunStores"] += deleted_count
    return counts


def _delete_google_analytics_artifacts(*, organization_ids: list[int]) -> dict[str, int]:
    counts = _zero_deleted_counts()
    counts["googleAnalyticsPropertySelections"] = _delete_count(
        GoogleAnalyticsPropertySelection.objects.filter(organization_id__in=organization_ids)
    )
    return counts


def _delete_google_analytics_run_stores(*, organization_ids: list[int], run_ids: list[str] | None = None) -> dict[str, int]:
    counts = _zero_deleted_counts()
    queryset = ExternalServiceConnection.objects.filter(
        organization_id__in=organization_ids,
        provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
    )
    run_id_set = set(run_ids or [])
    for connection in queryset:
        cursor = dict(connection.sync_cursor or {})
        run_stores = cursor.get("startup_update_runs")
        if not isinstance(run_stores, dict) or not run_stores:
            continue

        if run_id_set:
            deleted_count = 0
            next_run_stores = dict(run_stores)
            for run_id in run_id_set:
                if run_id in next_run_stores:
                    deleted_count += 1
                    next_run_stores.pop(run_id, None)
        else:
            deleted_count = len(run_stores)
            next_run_stores = {}

        if deleted_count <= 0:
            continue
        if next_run_stores:
            cursor["startup_update_runs"] = next_run_stores
        else:
            cursor.pop("startup_update_runs", None)
        connection.sync_cursor = cursor
        connection.last_synced_at = None
        connection.save(update_fields=["sync_cursor", "last_synced_at", "updated_at"])
        counts["googleAnalyticsRunStores"] += deleted_count
    return counts


def _delete_gmail_derived_outputs(
    *,
    organization_ids: list[int],
    gmail_runs: list[ContentFactoryRun],
    gmail_only: bool,
) -> dict[str, int]:
    counts = _zero_deleted_counts()
    run_ids = [run.id for run in gmail_runs]
    if gmail_only:
        counts["monthlyDrafts"] = _delete_count(
            MonthlyUpdateDraft.objects.filter(organization_id__in=organization_ids, run_id__in=run_ids)
        )
        counts["startupEvents"] = _delete_count(
            StartupEvent.objects.filter(organization_id__in=organization_ids, run_id__in=run_ids)
        )
        counts["startupMetrics"] = _delete_count(
            StartupMetricObservation.objects.filter(organization_id__in=organization_ids).filter(
                run_id__in=run_ids,
            )
            | StartupMetricObservation.objects.filter(
                organization_id__in=organization_ids,
                source_provider=GMAIL_PROVIDER,
            )
        )
        return counts

    counts["monthlyDrafts"] = _delete_count(MonthlyUpdateDraft.objects.filter(organization_id__in=organization_ids))
    counts["startupEvents"] = _delete_count(StartupEvent.objects.filter(organization_id__in=organization_ids))
    counts["startupMetrics"] = _delete_count(StartupMetricObservation.objects.filter(organization_id__in=organization_ids))
    return counts


def _cancelled_run_deleted_counts(cancelled_runs: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = _zero_deleted_counts()
    for cancelled_run in cancelled_runs:
        cleanup = cancelled_run.get("cleanup") if isinstance(cancelled_run, dict) else {}
        if not isinstance(cleanup, dict):
            continue
        counts["monthlyDrafts"] += int(cleanup.get("drafts_deleted") or 0)
        counts["startupEvents"] += int(cleanup.get("events_deleted") or 0)
        counts["startupMetrics"] += int(cleanup.get("metrics_deleted") or 0)
    return counts


def _scrub_runs(runs: Iterable[ContentFactoryRun], *, request_id: str, reason: str) -> int:
    scrubbed = 0
    deleted_at = timezone.now().isoformat()
    for run in runs:
        run_request = dict(run.run_request or {})
        for key in ("startup_context", "startup_memory", "external_context"):
            run_request.pop(key, None)
        run_request["data_deleted"] = True
        run_request["data_deletion_request_id"] = request_id
        run.result = {
            "data_deleted": True,
            "data_deletion_request_id": request_id,
            "deleted_at": deleted_at,
            "reason": reason or "user_request",
        }
        run.run_request = run_request
        run.resume_available = False
        update_fields = ["run_request", "result", "resume_available", "updated_at"]
        if run.status in OPEN_RUN_STATUSES:
            run.status = ContentFactoryRunStatus.CANCELLED
            run.error = "Cancelled because startup data was deleted."
            update_fields.extend(["status", "error"])
        run.save(update_fields=update_fields)
        scrubbed += 1
    return scrubbed


def _scrub_runs_with_counts(
    runs: Iterable[ContentFactoryRun],
    *,
    request_id: str,
    reason: str,
) -> dict[str, int]:
    counts = _zero_deleted_counts()
    counts["startupRunsScrubbed"] = _scrub_runs(runs, request_id=request_id, reason=reason)
    return counts


def disconnect_gmail_for_user(
    user,
    *,
    delete_derived_data: bool = False,
    reason: str = "user_request",
) -> dict[str, Any]:
    # Disconnect the active startup's mailbox (a founder may have one Gmail per
    # startup). Falls back to the user's only/legacy connection.
    from integrations.services.external_connectors import active_google_connection

    connection = active_google_connection(user)
    if connection is None:
        return {
            "status": "not_connected",
            "googleAccount": None,
            "google_account": None,
            "googleRevocation": {"requested": False, "succeeded": False, "warning": None},
            "google_revocation": {"requested": False, "succeeded": False, "warning": None},
            "deleted": _zero_deleted_counts(),
            "cancelledRuns": [],
            "cancelled_runs": [],
            "googlePermissionsUrl": GOOGLE_PERMISSIONS_URL,
            "google_permissions_url": GOOGLE_PERMISSIONS_URL,
        }

    google_account = connection.google_email
    google_connection_id = connection.id
    refresh_token = connection.refresh_token
    organizations = _organizations_for_google_connection(user, connection)
    organization_ids = [organization.id for organization in organizations]
    bindings = list(
        UserStartupBinding.objects.select_related("organization").filter(
            user=user,
            google_connection=connection,
        )
    )

    deletion_requests = [
        _create_deletion_request(
            organization=organization,
            user=user,
            provider=GMAIL_PROVIDER,
            delete_derived_data=delete_derived_data,
            google_account=google_account,
            reason=reason,
            metadata={"action": "disconnect_gmail"},
        )
        for organization in organizations
    ]

    warnings: list[str] = []
    cancelled_runs = _cancel_open_gmail_runs(user=user, connection=connection, bindings=bindings)
    google_revocation = _revoke_google_refresh_token(refresh_token)
    if google_revocation.get("warning"):
        warnings.append(str(google_revocation["warning"]))

    gmail_runs = _gmail_runs_for_organizations(organizations, google_connection_id=google_connection_id)
    deleted = _zero_deleted_counts()
    request_id = deletion_requests[0].request_id if deletion_requests else _request_id("gmail-delete")
    if delete_derived_data:
        _add_counts(deleted, _cancelled_run_deleted_counts(cancelled_runs))

    try:
        with transaction.atomic():
            _add_counts(
                deleted,
                _delete_gmail_artifacts(
                    organization_ids=organization_ids,
                    google_connection_id=google_connection_id,
                ),
            )
            if delete_derived_data and organization_ids:
                _add_counts(
                    deleted,
                    _delete_gmail_derived_outputs(
                        organization_ids=organization_ids,
                        gmail_runs=gmail_runs,
                        gmail_only=True,
                    ),
                )
                _scrub_runs(gmail_runs, request_id=request_id, reason=reason)
            GoogleConnection.objects.filter(id=google_connection_id, user=user).delete()
    except Exception:
        _complete_deletion_requests(
            deletion_requests,
            status=StartupDataDeletionStatus.FAILED,
            deleted_counts=deleted,
            warnings=warnings or ["Gmail deletion failed before completion."],
        )
        raise

    _complete_deletion_requests(
        deletion_requests,
        status=StartupDataDeletionStatus.DELETED,
        deleted_counts=deleted,
        warnings=warnings,
    )

    return {
        "status": "disconnected",
        "googleAccount": google_account,
        "google_account": google_account,
        "googleRevocation": google_revocation,
        "google_revocation": google_revocation,
        "deleted": deleted,
        "cancelledRuns": cancelled_runs,
        "cancelled_runs": cancelled_runs,
        "googlePermissionsUrl": GOOGLE_PERMISSIONS_URL,
        "google_permissions_url": GOOGLE_PERMISSIONS_URL,
    }


def delete_startup_data_for_organization(
    organization: Organization,
    *,
    requested_by_user_id: int | None = None,
    reason: str = "user_request",
    request_id: str | None = None,
) -> dict[str, Any]:
    User = get_user_model()
    requested_by_user = User.objects.filter(id=requested_by_user_id).first() if requested_by_user_id else None
    deletion_request = _create_deletion_request(
        organization=organization,
        user=requested_by_user,
        provider=STARTUP_PROVIDER,
        delete_derived_data=True,
        reason=reason,
        request_id=request_id,
        metadata={"action": "delete_startup_data"},
    )
    deleted = _zero_deleted_counts()
    warnings: list[str] = []
    organization_ids = [organization.id]
    runs = _startup_update_runs_for_organizations([organization])
    run_ids = [run.run_id for run in runs]
    cancelled_runs = _cancel_open_startup_runs(runs)

    try:
        with transaction.atomic():
            _add_counts(deleted, _delete_gmail_artifacts(organization_ids=organization_ids))
            _add_counts(deleted, _delete_slack_artifacts(organization_ids=organization_ids))
            _add_counts(deleted, _delete_linear_artifacts(organization_ids=organization_ids))
            _add_counts(
                deleted,
                _scrub_external_connection_cursors(
                    organization_ids=organization_ids,
                    providers=[ExternalServiceProvider.SLACK, ExternalServiceProvider.LINEAR],
                ),
            )
            _add_counts(deleted, _delete_notion_run_stores(organization_ids=organization_ids, run_ids=run_ids))
            _add_counts(deleted, _delete_google_analytics_artifacts(organization_ids=organization_ids))
            _add_counts(
                deleted,
                _delete_google_analytics_run_stores(organization_ids=organization_ids, run_ids=run_ids),
            )
            _add_counts(
                deleted,
                _delete_gmail_derived_outputs(
                    organization_ids=organization_ids,
                    gmail_runs=[],
                    gmail_only=False,
                ),
            )
            _add_counts(
                deleted,
                _scrub_runs_with_counts(runs, request_id=deletion_request.request_id, reason=reason),
            )
    except Exception:
        _complete_deletion_requests(
            [deletion_request],
            status=StartupDataDeletionStatus.FAILED,
            deleted_counts=deleted,
            warnings=["Startup data deletion failed before completion."],
        )
        raise

    _complete_deletion_requests(
        [deletion_request],
        status=StartupDataDeletionStatus.DELETED,
        deleted_counts=deleted,
        warnings=warnings,
    )
    return {
        "status": "deleted",
        "deletion_status": "deleted",
        "request_id": deletion_request.request_id,
        "organization_id": organization.id,
        "domain": organization.domain,
        "deleted": deleted,
        "cancelledRuns": cancelled_runs,
        "cancelled_runs": cancelled_runs,
        "warnings": warnings,
    }
