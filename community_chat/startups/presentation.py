"""Small, allowlisted DTOs shared by owner preview and community readers."""
import copy
from vibe_raising.views import _serialize_monthly_update

PUBLIC_FIELDS = (
    "id", "revisionId", "revisionHash", "isoMonth", "month", "year", "publishedAt",
    "summary", "highlights", "challenges", "learnings", "next30Days", "asks",
    "metrics", "metricEvidence", "audienceVisibility", "evidenceStatus",
)


def update_payload(draft, *, published=False, community=False):
    """Never expose private evidence, source URLs or uploads to community readers."""
    value = _serialize_monthly_update(draft, published=published)
    revision = draft.published_revision if published else draft.current_revision
    if revision:
        for metric in getattr(revision, "structured_memo", {}).get("kpi_snapshot", []):
            evidence = value.setdefault("metricEvidence", {}).setdefault(metric.get("metric_key"), {})
            evidence.update({"label": metric.get("label"), "unit": metric.get("unit")})
    if community:
        value = {key: copy.deepcopy(value.get(key)) for key in PUBLIC_FIELDS}
        # Detailed metric metadata can contain internal provider record identifiers.
        value["metricEvidence"] = {
            key: {field: item.get(field) for field in ("quality", "label", "unit") if item.get(field) is not None}
            for key, item in (value.get("metricEvidence") or {}).items()
        }
    else:
        value["validation"] = copy.deepcopy(revision.validation) if revision else {"legacy_unverified": True}
        value["hasNewerDraft"] = bool(draft.published_revision_id and draft.current_revision_id != draft.published_revision_id)
        value["publishedRevisionId"] = draft.published_revision_id
        memo = revision.structured_memo if revision else draft.structured_memo or {}
        frozen_manual = (revision.snapshot.payload.get("manual_sources") or {}) if revision else {}
        value["manualSummary"] = memo.get("manual_summary") or frozen_manual.get("summary", "")
        value["manualDocumentIds"] = [str(item["id"]) for item in (memo.get("manual_documents") or frozen_manual.get("documents") or []) if item.get("id")]
        value["inputSources"] = (revision.snapshot.payload.get("source_providers") or []) if revision else []
    value["startup"] = {"name": draft.organization.name}
    return value
