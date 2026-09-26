"""Small, allowlisted DTOs shared by owner preview and community readers."""
import copy
from vibe_raising.views import _serialize_monthly_update
from vibe_raising.serializers import normalize_vibe_raising_display_config

PUBLIC_FIELDS = (
    "id", "revisionId", "revisionHash", "isoMonth", "month", "year", "publishedAt",
    "summary", "highlights", "challenges", "learnings", "next30Days", "asks",
    "metrics", "metricEvidence", "audienceVisibility", "evidenceStatus",
)


def _selected_metric_review_payload(value, memo):
    """Apply the founder's metric display choice to Chat review fields.

    The revision and its evidence snapshot retain all captured values for the
    editor. Older revisions without an explicit choice keep their prior display.
    """
    config = memo.get("display_config")
    if not isinstance(config, dict):
        config = memo.get("displayConfig")
    if not isinstance(config, dict) or not ({"full_metric_keys", "fullMetricKeys"} & config.keys()):
        return value

    selected = set(normalize_vibe_raising_display_config(config)["full_metric_keys"])
    for field in ("metrics", "metricEvidence", "metricHistory"):
        entries = value.get(field)
        if isinstance(entries, dict):
            value[field] = {key: item for key, item in entries.items() if key in selected}
    # These aggregate views have no per-metric disclosure mapping. Keep raw
    # charts in the frozen revision, but do not return unselected figures in
    # a Chat approval or community preview.
    value["financialSnapshot"] = None
    value["conciseAnalysis"] = None
    value["progressCharts"] = None
    return value


def update_payload(draft, *, published=False, community=False):
    """Never expose private evidence, source URLs or uploads to community readers."""
    value = _serialize_monthly_update(draft, published=published)
    revision = draft.published_revision if published else draft.current_revision
    memo = getattr(revision, "structured_memo", None) if revision else getattr(draft, "structured_memo", None)
    memo = memo or {}
    if revision:
        for metric in memo.get("kpi_snapshot", []):
            evidence = value.setdefault("metricEvidence", {}).setdefault(metric.get("metric_key"), {})
            evidence.update({"label": metric.get("label"), "unit": metric.get("unit")})
    value = _selected_metric_review_payload(value, memo)
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
        frozen_manual = (revision.snapshot.payload.get("manual_sources") or {}) if revision else {}
        value["manualSummary"] = memo.get("manual_summary") or frozen_manual.get("summary", "")
        value["manualDocumentIds"] = [str(item["id"]) for item in (memo.get("manual_documents") or frozen_manual.get("documents") or []) if item.get("id")]
        value["inputSources"] = (revision.snapshot.payload.get("source_providers") or []) if revision else []
    value["startup"] = {"name": draft.organization.name}
    return value
