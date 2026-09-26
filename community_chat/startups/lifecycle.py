"""Owner-controlled update deletion and explicit connector capabilities."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import NotFound, ValidationError

from organizations.models import Organization
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft
from startup_updates.revisions import RevisionConflict
from startup_updates.services import OPEN_RUN_STATUSES, STARTUP_UPDATE_WORKFLOW
from workflow_runs.models import ContentFactoryRun


OAUTH_PROVIDERS = frozenset({
    "gmail", "stripe", "xero", "bank_feed", "notion", "google_drive",
    "slack", "linear", "google_analytics",
})
API_KEY_PROVIDERS = frozenset({"humanitix", "luma"})
UPDATE_PROVIDERS = OAUTH_PROVIDERS | API_KEY_PROVIDERS


def source_capabilities(source, *, preferences=None):
    """Describe connection actions separately from automatic source inclusion."""
    source = dict(source)
    provider = source.get("provider") or source.get("key")
    mode = "oauth" if provider in OAUTH_PROVIDERS else "api_key" if provider in API_KEY_PROVIDERS else None
    connected = source.get("status") in {"connected", "syncing"}
    source.update({
        "connectMode": mode,
        "canConnect": bool(mode and source.get("configured", source.get("status") != "unavailable")),
        "canDisconnect": bool(source.get("connectionId") or (provider == "gmail" and connected)),
        "usableForUpdates": bool(provider in UPDATE_PROVIDERS and connected and provider != "google_drive"),
    })
    if provider == "google_drive" and connected:
        source["warning"] = "Google Drive is connected. Update imports are not available yet."
    source["enabled"] = bool(source["usableForUpdates"] and (preferences or {}).get(provider, True))
    source["activityWindowDays"] = 30
    source["selectionMode"] = "recent_activity"
    if source.get("warning") in {
        "Select Slack channels before using Slack in a monthly update.",
        "Select a Google Analytics property before using Google Analytics in a monthly update.",
    }:
        source["warning"] = None
    return source


@transaction.atomic
def delete_update(*, organization, update_id, revision_id, revision_hash):
    """Delete the owner's exact reviewed revision and its community publication."""
    if organization is None:
        raise NotFound("Update not found.")
    # Independent-update creation also locks the organization. Read active runs
    # without taking their locks: workers acquire run before organization.
    Organization.objects.select_for_update().get(pk=organization.pk)
    active = ContentFactoryRun.objects.filter(
        workflow=STARTUP_UPDATE_WORKFLOW, domain=organization.domain,
        status__in=OPEN_RUN_STATUSES,
    )
    if active.exists():
        raise RevisionConflict("Finish or cancel draft generation before deleting an update.")
    draft = get_object_or_404(
        MonthlyUpdateDraft.objects.select_for_update(of=("self",)).select_related("current_revision"),
        pk=update_id, organization=organization,
    )
    revision = draft.current_revision
    if revision:
        if revision_id != revision.pk or revision_hash != revision.content_hash:
            raise RevisionConflict()
    elif revision_id is not None or revision_hash not in (None, ""):
        raise RevisionConflict()
    snapshot_ids = list(draft.revisions.values_list("snapshot_id", flat=True))
    draft.delete()  # Revisions and approval receipts cascade with the publication.
    MonthlyEvidenceSnapshot.objects.filter(
        organization=organization, pk__in=snapshot_ids,
        monthlyupdaterevision__isnull=True,
    ).delete()


def validate_generation_sources(data):
    """An empty selection must never silently opt a founder into Gmail."""
    sources = data.get("inputSources", data.get("input_sources"))
    manual = bool(str(data.get("manualSummary", data.get("manual_summary", "")) or "").strip()
        or data.get("manualDocumentIds", data.get("manual_document_ids")))
    if not isinstance(sources, list) or (not sources and not manual):
        raise ValidationError({"inputSources": "Select at least one connection or add notes."})
    if any(not isinstance(source, str) or source not in UPDATE_PROVIDERS | {"manual_documents"} for source in sources):
        raise ValidationError({"inputSources": "Choose supported startup update sources."})
