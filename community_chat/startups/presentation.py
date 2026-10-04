"""Small, allowlisted DTOs shared by owner preview and community readers."""
import copy
from startup_updates.disclosure import financial_chart, shared_update
from vibe_raising.views import _serialize_monthly_update

PUBLIC_FIELDS = (
    "id", "updateTitle", "monthSequence", "coverImage", "coverImageUrl", "revisionId", "revisionHash", "isoMonth", "month", "year", "publishedAt",
    "summary", "highlights", "challenges", "learnings", "next30Days", "asks",
    "metrics", "metricEvidence", "displayConfig", "financialChart", "audienceVisibility", "evidenceStatus",
)


def update_payload(draft, *, published=False, community=False):
    """Never expose private evidence, source URLs or uploads to community readers."""
    value = _serialize_monthly_update(draft, published=published, shared=False)
    revision = draft.published_revision if published else draft.current_revision
    memo = getattr(revision, "structured_memo", None) if revision else getattr(draft, "structured_memo", None)
    memo = memo or {}
    if revision:
        for metric in memo.get("kpi_snapshot", []):
            evidence = value.setdefault("metricEvidence", {}).setdefault(metric.get("metric_key"), {})
            evidence.update({"label": metric.get("label"), "unit": metric.get("unit")})
    if community:
        value = shared_update(value, metric_items=memo.get("kpi_snapshot"))
        value = {key: copy.deepcopy(value.get(key)) for key in PUBLIC_FIELDS}
    else:
        value["financialChart"] = financial_chart(value, metric_items=memo.get("kpi_snapshot"))
        value["validation"] = copy.deepcopy(revision.validation) if revision else {"legacy_unverified": True}
        value["agentProvenance"] = copy.deepcopy(memo.get("_agent_provenance"))
        value["agentSources"] = copy.deepcopy(memo.get("_agent_sources", []))
        value["hasNewerDraft"] = bool(draft.published_revision_id and draft.current_revision_id != draft.published_revision_id)
        value["publishedRevisionId"] = draft.published_revision_id
        frozen_manual = (revision.snapshot.payload.get("manual_sources") or {}) if revision else {}
        value["manualSummary"] = memo.get("manual_summary") or frozen_manual.get("summary", "")
        value["manualDocumentIds"] = [str(item["id"]) for item in (memo.get("manual_documents") or frozen_manual.get("documents") or []) if item.get("id")]
        value["inputSources"] = list(memo["selected_input_sources"]) if "selected_input_sources" in memo else ((revision.snapshot.payload.get("source_providers") or []) if revision else [])
    value["startup"] = {"name": draft.organization.name}
    return value
