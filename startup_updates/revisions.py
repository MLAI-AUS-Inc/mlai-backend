"""Canonical evidence and exact-content approval for monthly updates."""
from __future__ import annotations

import copy
from datetime import timedelta
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import APIException, ValidationError

from startup_updates.evidence_contract import (
    REVENUE_DEFINITION, content_hash, financial_snapshot_from_metrics,
    health_assessment, reporting_period, render_metric_claims,
)
from startup_updates.models import (
    MonthlyEvidenceSnapshot, MonthlyUpdateApproval, MonthlyUpdateDraft,
    MonthlyUpdateRevision, StartupEvent, StartupMetricObservation, StartupProfile,
)


class RevisionConflict(APIException):
    status_code = 409
    default_detail = "This update changed. Reload and review the latest revision."


def capture_snapshot(organization, month, *, run=None, manual_metrics=None, base_snapshot=None):
    profile, _ = StartupProfile.objects.get_or_create(organization=organization)
    try:
        request = (run.run_request or {}) if run else {}
        zone = request.get("reporting_timezone") or profile.reporting_timezone
        window_end = request.get("backfill_window_end")
        if base_snapshot is not None:
            period = copy.deepcopy(base_snapshot.payload["period"])
        elif window_end:
            cutoff = parse_datetime(str(window_end))
            if cutoff is None or timezone.is_naive(cutoff):
                raise ValueError("The source cutoff must be a valid timestamp with a timezone.")
            # Source windows include their final instant; snapshot cutoffs are
            # exclusive. A delayed or resumed run must retain its original MTD.
            period = reporting_period(month, zone, as_of=cutoff + timedelta(microseconds=1))
        else:
            period = reporting_period(month, zone)
    except (ValueError, KeyError) as exc:
        raise ValidationError(str(exc)) from exc
    definitions = [REVENUE_DEFINITION, *[item for item in profile.kpi_definitions if isinstance(item, dict) and item.get("key") != "revenue"]]
    keys = {"revenue", "monthlyCosts"}
    keys.update(StartupMetricObservation.objects.filter(organization=organization, period_month=month).values_list("metric_key", flat=True))
    keys.difference_update({"mrr", "arr", "burnRate", "runway", "invoiceRevenue", "cashCollected"})
    keys.update(str(item.get("key") or item.get("metric_key")) for item in definitions)
    observations = StartupMetricObservation.objects.filter(
        organization=organization, period_month=month, metric_key__in=keys,
    ).order_by("-observed_at", "-id")
    if run is not None:
        sources = set((run.run_request or {}).get("input_sources") or [])
        if sources:
            providers = sources | ({"financial"} if "stripe" in sources else set())
            observations = observations.filter(source_provider__in=providers)
    by_key = {}
    for observation in observations:
        by_key.setdefault(observation.metric_key, []).append(observation)
    definitions_by_key = {item.get("key"): item for item in definitions}
    metrics = []
    for key in sorted(keys):
        candidates = by_key.get(key, [])
        # Revenue is only from deterministic financial publishers.
        if key == "revenue":
            candidates = [item for item in candidates if (
                item.source_provider == "xero" and (item.source_metadata or {}).get("source_metric") == "xero_profit_and_loss_revenue"
            ) or (
                item.source_provider == "financial" and (item.source_metadata or {}).get("definition_version") == 2
                and (item.source_metadata or {}).get("basis") == "paid_stripe_invoice_sales_excluding_tax"
            )]
            xero = [item for item in candidates if item.source_provider == "xero"]
            candidates = xero or candidates
        candidates = [item for item in candidates if (item.unit == profile.default_currency if key in {"revenue", "monthlyCosts", "netProfitLoss", "operatingExpenses", "costOfSales"} else not item.unit or item.unit in {"count", "ratio", "months", "%"} or item.unit == profile.default_currency)]
        observation = candidates[0] if candidates else None
        metrics.append({
            "key": key, "label": "Revenue" if key == "revenue" else (observation.metric_name if observation else definitions_by_key.get(key, {}).get("label", key)),
            "value": str(observation.value_number) if observation and observation.value_number is not None else None,
            "display_value": (observation.value_text or (f"{observation.unit} {observation.value_number}".strip() if observation.value_number is not None else None)) if observation else None,
            "unit": observation.unit if observation else "",
            "quality": ("partial" if observation.source_provider == "financial" else "source_reported" if observation.source_provider in {"xero", "stripe", "google_analytics"} else "model_extracted") if observation and observation.value_number is not None else "unknown",
            "observation_id": observation.id if observation else None,
            "observed_at": observation.observed_at.isoformat() if observation and observation.observed_at else None,
            "source_provider": observation.source_provider if observation else None,
            "source_record_ids": observation.source_record_ids if observation else [],
            "metadata": copy.deepcopy(observation.source_metadata) if observation else {},
        })
    if base_snapshot is not None:
        metrics = copy.deepcopy(base_snapshot.payload["metrics"])
    for key, value in (manual_metrics or {}).items():
        if not str(value or "").strip():
            metrics = [item for item in metrics if item["key"] != key]
            continue
        # Founder assertions are explicit evidence, not silently provider-verified.
        metrics = [item for item in metrics if item["key"] != key]
        from startup_updates.evidence_contract import decimal_value
        number = decimal_value(str(value).replace(",", ""))
        unit = profile.default_currency if key in {"revenue", "monthlyCosts"} else str(definitions_by_key.get(key, {}).get("unit") or "")
        metrics.append({"key": key, "label": "Revenue" if key == "revenue" else definitions_by_key.get(key, {}).get("label", key), "value": str(number) if number is not None else None, "display_value": str(value), "unit": unit, "quality": "founder_asserted", "source_provider": "founder", "observed_at": timezone.now().isoformat()})
    payload = financial_snapshot_from_metrics(period, metrics, definitions)
    payload.pop("hash", None)
    payload["organization_id"] = organization.pk
    payload["startup"] = {"id": organization.pk, "name": organization.name, "stage": profile.stage, "description": profile.short_description}
    payload["config_version"] = profile.reporting_config_version
    payload["source_providers"] = sorted(set((run.run_request or {}).get("input_sources", [])) | {item.get("source_provider") for item in metrics if item.get("source_provider")}) if run else sorted({item.get("source_provider") for item in metrics if item.get("source_provider")})
    from startup_updates.services import build_monthly_financial_snapshot
    chart_sources = None
    if run and (run.run_request or {}).get("input_sources"):
        chart_sources = ({"xero"} if "xero" in sources else set()) | ({"financial"} if "stripe" in sources else set())
    payload["charts"] = copy.deepcopy(base_snapshot.payload.get("charts", {})) if base_snapshot else build_monthly_financial_snapshot(organization=organization, target_month=month, source_providers=chart_sources)
    # A manual change must affect charts as well as cards. Unparseable assertions produce gaps.
    from startup_updates.evidence_contract import decimal_value
    if not payload.get("charts") and manual_metrics and any(
        item["key"] in {"revenue", "monthlyCosts"} and decimal_value(item.get("value")) is not None for item in metrics
    ):
        payload["charts"] = {
            "schema_version": 2, "target_month": month.isoformat(), "as_of_date": period["cutoff"][:10],
            "currency": profile.default_currency, "performance": [{"month": month.isoformat()}],
            "revenue_mix": [], "event_contribution": [], "overhead": [],
            "data_quality": {"warnings": ["Founder-entered values require source confirmation."], "calculation_basis": "Founder assertions"},
        }
    for point in (payload.get("charts") or {}).get("performance", []):
        if point["month"] == month.isoformat():
            for key, field in (("revenue", "income"), ("monthlyCosts", "expenses")):
                metric = next((item for item in metrics if item["key"] == key), None)
                value = decimal_value(metric.get("value")) if metric else None
                point[field] = float(value) if value is not None else None
            net = next((item for item in metrics if item["key"] == "netProfitLoss"), None)
            amount = decimal_value(net.get("value")) if net else None
            point["net"] = float(amount) if amount is not None and not manual_metrics else None
            point["is_partial"] = period["is_partial"]
    events = StartupEvent.objects.filter(organization=organization, month_bucket=month)
    if run is not None:
        from startup_updates.api_views import _run_result_candidates
        approved = {int(item["event_id"]) for item in _run_result_candidates(run)
            if item.get("event_id") and item.get("founder_status") in {"approved", "auto_approved"}}
        events = events.filter(run=run)
        payload["editorial_event_ids"] = sorted(approved)
    payload["events"] = list(events.order_by("id").values(
        "id", "title", "summary", "event_date", "status", "evidence_message_ids", "evidence_attachment_ids", "quantitative_facts", "source_thread_ids", "needs_review", "confidence",
    ))
    for event in payload["events"]:
        if event["event_date"]:
            event["event_date"] = event["event_date"].isoformat()
    from startup_updates.source_evidence import frozen_manual_sources, EXTRACTION_VERSION
    payload["extraction_version"] = EXTRACTION_VERSION
    payload["source_evidence"] = copy.deepcopy((run.result or {}).get("source_evidence", {})) if run else {}
    payload["manual_sources"] = frozen_manual_sources(organization, run) if run else {"summary": "", "documents": []}
    external = (run.run_request or {}).get("external_context", {}) if run else {}
    payload["source_coverage"] = {key: {field: copy.deepcopy(value[field]) for field in ("warnings", "index_partial", "needs_review", "pages_indexed") if field in value}
        for key, value in external.items() if isinstance(value, dict)}
    evidence_warnings = []
    for receipt in payload["source_evidence"].values():
        bundle = receipt.get("bundle", {})
        if bundle.get("temporal_warning"):
            evidence_warnings.append(bundle["temporal_warning"])
        for attachment in bundle.get("attachments", []):
            if attachment.get("extraction_status") != "processed":
                evidence_warnings.append(f"Attachment {attachment.get('filename', '')}: text is unavailable; review the original source.")
    for document in payload["manual_sources"].get("documents", []):
        if document.get("status") != "processed" or "OCR" in document.get("parse_notes", ""):
            evidence_warnings.append(f"{document['filename']}: {document.get('parse_notes') or 'text unavailable'}")
    payload["source_coverage"]["evidence"] = {"warnings": sorted(set(evidence_warnings))}
    warnings = [warning for coverage in payload["source_coverage"].values() for warning in coverage.get("warnings", [])]
    for metric in metrics:
        metadata = metric.get("metadata") or {}
        if metric.get("source_provider") == "financial" and run and not (run.run_request or {}).get("source_evidence_refreshed", {}).get("stripe_complete"):
            metric["quality"] = "stale"
            metric["reason"] = "Stripe did not complete a fresh sync for this reporting run."
        if metric.get("source_provider") == "xero" and run:
            # A cached point remains visible but cannot masquerade as refreshed.
            if not metadata.get("report_hash") or any("unavailable for " + month.isoformat() in str(warning) or "currency could not be verified" in str(warning) for warning in warnings):
                metric["quality"] = "stale"
                metric["reason"] = "Xero evidence was not refreshed for this reporting period."
        if metric.get("source_provider") in {"gmail", "notion", "slack", "linear"} and run:
            current = next((item for item in by_key.get(metric["key"], []) if item.pk == metric.get("observation_id")), None)
            if current and current.run_id != run.pk:
                metric["quality"] = "stale"
                metric["reason"] = "This metric predates the current extraction contract; confirm its definition and period."
    payload["metrics"] = metrics
    if payload.get("charts"):
        payload["charts"]["data_quality"]["warnings"].extend(warnings)
    from vibe_raising.metric_history import build_metric_history
    prior = MonthlyUpdateDraft.objects.filter(organization=organization, month__lt=month).select_related("published_revision__snapshot", "current_revision__snapshot")
    pairs = []
    for item in prior:
        revision = item.published_revision or item.current_revision
        if revision and not revision.snapshot.payload.get("legacy_unverified"):
            pairs.append((item.month, copy.deepcopy(revision.structured_memo)))
    current_memo = {"kpi_snapshot": [{"metric_key": item["key"], "value": item.get("display_value"), "value_number": item.get("value"), "unit": item.get("unit"), "label": item["label"], "snapshot_id": "captured"} for item in metrics]}
    payload["metric_history"] = build_metric_history([*pairs, (month, current_memo)])
    if base_snapshot is not None:
        payload["amended_at"] = timezone.now().isoformat()
        for field in ("events", "startup", "period", "definitions", "config_version", "source_providers", "manual_sources", "source_evidence", "source_coverage", "extraction_version", "editorial_event_ids"):
            payload[field] = copy.deepcopy(base_snapshot.payload.get(field))
    payload["hash"] = content_hash(payload)
    snapshot, _ = MonthlyEvidenceSnapshot.objects.get_or_create(
        organization=organization, content_hash=payload["hash"],
        defaults={"month": month, "payload": payload},
    )
    return snapshot


def revision_payload(revision, *, include_evidence=True):
    return {
        "revisionId": revision.pk, "revision": revision.number,
        "revisionHash": revision.content_hash, "snapshotId": revision.snapshot_id,
        "evidenceSnapshot": revision.snapshot.payload if include_evidence else None,
        "audience": revision.audience,
    }


def frozen_memo(draft, *, published=False):
    revision = draft.published_revision if published else draft.current_revision
    return copy.deepcopy(revision.structured_memo if revision else draft.structured_memo or {})


@transaction.atomic
def save_revision(draft, memo, *, snapshot, audience="private", expected_revision=None, require_match=True):
    draft = MonthlyUpdateDraft.objects.select_for_update().get(pk=draft.pk)
    if snapshot.organization_id != draft.organization_id or snapshot.month != draft.month:
        raise ValidationError("The evidence snapshot belongs to a different startup or period.")
    current = draft.current_revision
    if require_match and (str(expected_revision or "") != str(current.pk if current else "")):
        raise RevisionConflict()
    if draft.published_at and not draft.published_revision_id:
        # Preserve historical publication verbatim on the first edit. This is
        # archival evidence only: no fresh provider values or approval are implied.
        legacy_memo = copy.deepcopy(draft.structured_memo or {})
        legacy_payload = {"legacy_unverified": True, "organization_id": draft.organization_id,
            "month": draft.month.isoformat(), "metrics": [], "events": [],
            "published_memo": legacy_memo}
        legacy_snapshot, _ = MonthlyEvidenceSnapshot.objects.get_or_create(
            organization=draft.organization, content_hash=content_hash(legacy_payload),
            defaults={"month": draft.month, "payload": legacy_payload})
        legacy_revision = MonthlyUpdateRevision.objects.create(
            draft=draft, snapshot=legacy_snapshot,
            number=(draft.revisions.aggregate(n=Max("number"))["n"] or 0) + 1,
            audience="community" if "community" in draft.audience_visibility else "private",
            structured_memo=legacy_memo, rendered_markdown=draft.rendered_markdown,
            content_hash=content_hash({"legacy_memo": legacy_memo, "snapshot": legacy_snapshot.content_hash}),
            validation={"legacy_unverified": True})
        draft.published_revision = legacy_revision
        draft.save(update_fields=["published_revision"])
    from startup_updates.covers import inherit_cover
    try:
        memo = inherit_cover(copy.deepcopy(memo), current.structured_memo if current else (draft.structured_memo or {}), draft.organization_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    try:
        memo = render_metric_claims(memo, snapshot.payload["metrics"])
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    selected_keys = {item.get("metric_key") for item in memo.get("kpi_snapshot", [])}
    # The snapshot supplies all metric displays. No read-time provider enrichment.
    memo["kpi_snapshot"] = [
        {"metric_key": item["key"], "label": item["label"], "value": item["display_value"], "value_number": item.get("value"), "unit": item.get("unit", ""), "quality": item.get("quality"), "source_provider": item.get("source_provider"), "basis": (item.get("metadata") or {}).get("basis") or (item.get("metadata") or {}).get("source_metric"), "limitations": (item.get("metadata") or {}).get("limitations", []), "snapshot_id": snapshot.pk}
        for item in snapshot.payload["metrics"] if item.get("display_value") is not None
    ]
    if audience == "community":
        config = memo.get("display_config") or {}
        if "full_metric_keys" in config:
            selected_keys &= set(config["full_metric_keys"])
        memo["kpi_snapshot"] = [item for item in memo["kpi_snapshot"] if item["metric_key"] in selected_keys]
    memo["metric_history"] = {key: value for key, value in copy.deepcopy(snapshot.payload.get("metric_history", {})).items() if audience == "private" or key in selected_keys}
    memo["financial_snapshot"] = copy.deepcopy(snapshot.payload.get("charts")) if audience == "private" else None
    memo["_audience_visibility"] = ["community" if audience == "community" else "just_me"]
    memo["reporting_period"] = copy.deepcopy(snapshot.payload.get("period"))
    memo["evidence_warnings"] = sorted({str(warning) for source in snapshot.payload.get("source_coverage", {}).values() for warning in source.get("warnings", [])}) if audience == "private" else []
    memo["evidence_snapshot_id"] = snapshot.pk
    memo["evidence_snapshot_hash"] = snapshot.content_hash
    from startup_updates.services import render_monthly_update_markdown
    rendered = render_monthly_update_markdown(memo)
    digest = content_hash({"memo": memo, "snapshot": snapshot.content_hash, "audience": audience})
    if current and current.content_hash == digest:
        return current
    revision = MonthlyUpdateRevision.objects.create(
        draft=draft, snapshot=snapshot,
        number=(draft.revisions.aggregate(n=Max("number"))["n"] or 0) + 1,
        audience=audience, content_hash=digest,
        structured_memo=memo, rendered_markdown=rendered,
        validation=copy.deepcopy(current.validation) if current and current.validation.get("groundedness_status") in {"failed", "needs_review", "pending"} else {},
    )
    # Compatibility fields mirror only the current private working copy.
    draft.current_revision = revision
    draft.structured_memo = memo
    draft.rendered_markdown = rendered
    draft.evidence_event_ids = [item["id"] for item in snapshot.payload.get("events", [])]
    draft.evidence_metric_ids = [item["observation_id"] for item in snapshot.payload["metrics"] if item.get("observation_id")]
    draft.carry_forward_event_ids = []
    draft.groundedness_status = "pending"
    draft.status = "draft"
    draft.save(update_fields=["current_revision", "structured_memo", "rendered_markdown", "groundedness_status", "status", "evidence_event_ids", "evidence_metric_ids", "carry_forward_event_ids", "updated_at"])
    return revision


@transaction.atomic
def approve_and_publish(draft, *, actor, revision_id, revision_hash, audience_visibility):
    draft = MonthlyUpdateDraft.objects.select_for_update().get(pk=draft.pk)
    revision = draft.current_revision
    if not revision or str(revision.pk) != str(revision_id) or revision.content_hash != revision_hash:
        raise RevisionConflict()
    if audience_visibility != revision.structured_memo.get("_audience_visibility"):
        raise RevisionConflict("Disclosure changed. Save and review a new revision.")
    if revision.validation.get("groundedness_status") in {"failed", "needs_review", "pending"}:
        raise ValidationError("Resolve the evidence review before publishing.")
    approval, created = MonthlyUpdateApproval.objects.get_or_create(
        revision=revision,
        defaults={"actor": actor, "content_hash": revision_hash, "audience_visibility": audience_visibility},
    )
    if not created and (approval.content_hash != revision_hash or approval.audience_visibility != audience_visibility):
        raise RevisionConflict("Disclosure changed. Create and review a new revision.")
    previous_published_id = draft.published_revision_id
    draft.published_revision = revision
    draft.audience_visibility = audience_visibility
    draft.published_at = timezone.now() if previous_published_id != revision.pk or not draft.published_at else draft.published_at
    draft.status = "ready"
    draft.run = None  # A cancelled worker cannot delete an explicitly approved publication.
    draft.save(update_fields=["published_revision", "audience_visibility", "published_at", "status", "run", "updated_at"])
    return draft
