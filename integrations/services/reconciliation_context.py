"""Context contract between Valley and deterministic payout reconciliation."""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction
from django.utils import timezone

from integrations.models import ReconciliationMapping, ReconciliationSuggestion, StripePayoutReconciliation
from startup_updates.models import LinearProjectArtifact, LinearProjectSelection, LumaEventSelection


ALLOWED_EVIDENCE_PROVIDERS = {
    "gmail",
    "slack",
    "linear",
    "luma",
    "stripe",
    "xero",
    "startup_memory",
}


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def serialize_suggestion(suggestion: ReconciliationSuggestion) -> dict[str, Any]:
    return {
        "id": suggestion.id,
        "payout_id": suggestion.payout.payout_id,
        "run_id": suggestion.run_id,
        "source_type": suggestion.source_type,
        "source_id": suggestion.source_id,
        "source_label": suggestion.source_label,
        "event": {
            "source_type": suggestion.event_source_type,
            "source_id": suggestion.event_source_id,
            "tracking_option_name": suggestion.event_tracking_option_name,
        } if suggestion.event_source_id or suggestion.event_tracking_option_name else None,
        "project": {
            "source_type": suggestion.project_source_type,
            "source_id": suggestion.project_source_id,
            "tracking_option_name": suggestion.project_tracking_option_name,
        } if suggestion.project_source_id or suggestion.project_tracking_option_name else None,
        "confidence": suggestion.confidence,
        "rationale": suggestion.rationale,
        "review_note": suggestion.review_note,
        "evidence": suggestion.evidence or [],
        "source_hash": suggestion.source_hash,
        "model_name": suggestion.model_name,
        "status": suggestion.status,
        "reviewed_by_slack_id": suggestion.reviewed_by_slack_id,
        "reviewed_at": suggestion.reviewed_at.isoformat() if suggestion.reviewed_at else None,
        "created_at": suggestion.created_at.isoformat() if suggestion.created_at else None,
    }


def _linear_projects(organization) -> list[dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for project in LinearProjectArtifact.objects.filter(organization=organization).order_by("name", "linear_project_id"):
        projects[project.linear_project_id] = {
            "source_type": "linear",
            "source_id": project.linear_project_id,
            "name": project.name,
            "description": project.description[:2000],
            "status": project.status_name or project.status_type,
            "start_date": project.start_date.isoformat() if project.start_date else None,
            "target_date": project.target_date.isoformat() if project.target_date else None,
            "url": project.url,
        }
    for selection in LinearProjectSelection.objects.filter(organization=organization).order_by(
        "project_name", "linear_project_id"
    ):
        projects.setdefault(
            selection.linear_project_id,
            {
                "source_type": "linear",
                "source_id": selection.linear_project_id,
                "name": selection.project_name or selection.linear_project_id,
                "description": "",
                "status": selection.project_status,
                "start_date": None,
                "target_date": None,
                "url": "",
            },
        )
    return list(projects.values())


def _luma_events(organization) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for event in LumaEventSelection.objects.filter(organization=organization).order_by("-start_at", "event_name"):
        events.setdefault(
            event.event_id,
            {
                "source_type": "luma",
                "source_id": event.event_id,
                "name": event.event_name or event.event_id,
                "start_at": event.start_at.isoformat() if event.start_at else None,
                "url": event.event_url,
            },
        )
    return list(events.values())


def _source_rows(record: StripePayoutReconciliation) -> list[dict[str, Any]]:
    report = record.report_payload or {}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kind, groups in (
        ("revenue", report.get("revenue_groups") or report.get("events") or []),
        ("refund", report.get("refunds") or []),
    ):
        for group in groups:
            if not isinstance(group, dict):
                continue
            source_type = str(group.get("source_type") or "").strip()
            source_id = str(group.get("source_id") or group.get("event_api_id") or group.get("id") or "").strip()
            if not source_type or not source_id or (source_type, source_id) in seen:
                continue
            seen.add((source_type, source_id))
            rows.append(
                {
                    "kind": kind,
                    "source_type": source_type,
                    "source_id": source_id,
                    "source_label": str(group.get("source_label") or group.get("event_name") or "").strip(),
                    "event_api_id": str(group.get("event_api_id") or "").strip(),
                    "gross_cents": int(group.get("gross_cents") or 0),
                    "stripe_fee_cents": int(group.get("stripe_fee_cents") or 0),
                    "net_cents": int(group.get("net_cents") or 0),
                }
            )
    return rows


def _validated_evidence(raw_evidence: Any) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for raw in raw_evidence or []:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("source_provider") or "").strip().lower()
        source_record_id = str(raw.get("source_record_id") or "").strip()
        if provider not in ALLOWED_EVIDENCE_PROVIDERS or not source_record_id:
            continue
        evidence.append(
            {
                "source_provider": provider,
                "source_record_id": source_record_id[:500],
                "summary": str(raw.get("summary") or "").strip()[:1000],
            }
        )
        if len(evidence) >= 20:
            break
    return evidence


def build_reconciliation_enrichment_context(*, organization, run_id: str = "") -> dict[str, Any]:
    """Return immutable Stripe candidates plus Luma events and Linear projects.

    Gmail and Slack evidence is intentionally supplied by Valley's canonical
    timeline/startup memory rather than duplicating raw private messages here.
    """
    luma_events = _luma_events(organization)
    linear_projects = _linear_projects(organization)
    linear_by_name: dict[str, list[dict[str, Any]]] = {}
    for project in linear_projects:
        linear_by_name.setdefault(_normalized_name(project["name"]), []).append(project)
    for event in luma_events:
        event["exact_linear_matches"] = [
            {"source_id": project["source_id"], "name": project["name"]}
            for project in linear_by_name.get(_normalized_name(event["name"]), [])
        ]
    luma_by_name: dict[str, list[dict[str, Any]]] = {}
    for event in luma_events:
        luma_by_name.setdefault(_normalized_name(event["name"]), []).append(event)
    for project in linear_projects:
        project["matching_luma_events"] = [
            {"source_id": event["source_id"], "name": event["name"]}
            for event in luma_by_name.get(_normalized_name(project["name"]), [])
        ]
        project["dimension_hint"] = "event_mirror" if project["matching_luma_events"] else "project"

    mappings = {
        (mapping.source_type, mapping.source_id): mapping
        for mapping in ReconciliationMapping.objects.filter(organization=organization, active=True)
    }
    candidates: list[dict[str, Any]] = []
    records = StripePayoutReconciliation.objects.filter(
        organization=organization,
        status__in=[
            StripePayoutReconciliation.STATUS_NEEDS_REVIEW,
            StripePayoutReconciliation.STATUS_READY,
            StripePayoutReconciliation.STATUS_FAILED,
        ],
    ).order_by("-arrival_date", "-id")[:250]
    for record in records:
        latest_suggestions: dict[tuple[str, str], dict[str, Any]] = {}
        for suggestion in record.suggestions.exclude(status=ReconciliationSuggestion.STATUS_SUPERSEDED).order_by(
            "source_type", "source_id", "-created_at"
        ):
            latest_suggestions.setdefault(
                (suggestion.source_type, suggestion.source_id),
                serialize_suggestion(suggestion),
            )
        for row in _source_rows(record):
            key = (row["source_type"], row["source_id"])
            mapping = mappings.get(key)
            candidates.append(
                {
                    "payout_id": record.payout_id,
                    "arrival_date": record.arrival_date.isoformat() if record.arrival_date else None,
                    "currency": record.currency,
                    "payout_amount_cents": record.amount_cents,
                    "source_hash": record.source_hash,
                    **row,
                    "current_mapping": {
                        "event_tracking_option_name": mapping.event_tracking_option_name,
                        "project_source_type": mapping.project_source_type,
                        "project_source_id": mapping.project_source_id,
                        "project_tracking_option_name": mapping.project_tracking_option_name,
                        "reconciliation_note": mapping.reconciliation_note,
                    } if mapping else None,
                    "latest_suggestion": latest_suggestions.get(key),
                }
            )

    return {
        "organization_id": organization.id,
        "domain": organization.domain,
        "run_id": run_id,
        "source_policy": {
            "events": "Luma is canonical; a same-name Linear record is the event cross-reference, not automatically a Project Name.",
            "projects": "Linear is canonical for Project Name. Prefer Linear-only records marked dimension_hint=project.",
            "narrative_context": "Gmail and Slack may support the review note but cannot establish amounts, tax, or accounts.",
        },
        "candidates": candidates,
        "luma_events": luma_events,
        "linear_projects": linear_projects,
    }


def save_reconciliation_suggestions(
    *, organization, run_id: str, suggestions: list[dict[str, Any]], model_name: str = ""
) -> list[ReconciliationSuggestion]:
    event_by_id = {event["source_id"]: event for event in _luma_events(organization)}
    project_by_id = {project["source_id"]: project for project in _linear_projects(organization)}
    payout_by_id = {
        record.payout_id: record
        for record in StripePayoutReconciliation.objects.filter(
            organization=organization,
            payout_id__in=[str(item.get("payout_id") or "") for item in suggestions if isinstance(item, dict)],
        )
    }
    saved: list[ReconciliationSuggestion] = []
    with transaction.atomic():
        for item in suggestions:
            if not isinstance(item, dict):
                raise ValueError("Each reconciliation suggestion must be an object.")
            payout_id = str(item.get("payout_id") or "").strip()
            source_type = str(item.get("source_type") or "").strip()
            source_id = str(item.get("source_id") or "").strip()
            payout = payout_by_id.get(payout_id)
            if payout is None:
                raise ValueError(f"Unknown payout: {payout_id}")
            source_rows = {(row["source_type"], row["source_id"]): row for row in _source_rows(payout)}
            source = source_rows.get((source_type, source_id))
            if source is None:
                raise ValueError(f"{source_type}:{source_id} is not part of payout {payout_id}.")

            event_payload = item.get("event") if isinstance(item.get("event"), dict) else {}
            event_id = str(event_payload.get("source_id") or "").strip()
            if not event_id and source_type == ReconciliationMapping.SOURCE_LUMA_EVENT:
                event_id = source_id
            event = event_by_id.get(event_id) if event_id else None
            if event_id and event is None:
                raise ValueError(f"Unknown Luma event: {event_id}")

            project_payload = item.get("project") if isinstance(item.get("project"), dict) else {}
            project_id = str(project_payload.get("source_id") or "").strip()
            project = project_by_id.get(project_id) if project_id else None
            if project_id and project is None:
                raise ValueError(f"Unknown Linear project: {project_id}")

            confidence = max(0.0, min(float(item.get("confidence") or 0.0), 1.0))
            evidence = _validated_evidence(item.get("evidence"))
            review_note = str(item.get("review_note") or "").strip()[:4000]
            if review_note and not evidence:
                raise ValueError(
                    f"A reconciliation review note for {source_type}:{source_id} must cite source evidence."
                )
            ReconciliationSuggestion.objects.filter(
                organization=organization,
                payout=payout,
                source_type=source_type,
                source_id=source_id,
                status=ReconciliationSuggestion.STATUS_PROPOSED,
            ).exclude(run_id=run_id).update(status=ReconciliationSuggestion.STATUS_SUPERSEDED, updated_at=timezone.now())
            lookup = {
                "organization": organization,
                "payout": payout,
                "run_id": run_id,
                "source_type": source_type,
                "source_id": source_id,
            }
            existing = ReconciliationSuggestion.objects.select_for_update().filter(**lookup).first()
            if existing is not None and existing.status != ReconciliationSuggestion.STATUS_PROPOSED:
                # A worker retry must never undo a founder's approve/reject decision.
                saved.append(existing)
                continue
            defaults = {
                "source_label": source["source_label"][:500],
                "event_source_type": "luma" if event else "",
                "event_source_id": event_id if event else "",
                "event_tracking_option_name": str(event.get("name") if event else "")[:255],
                "project_source_type": "linear" if project else "",
                "project_source_id": project_id if project else "",
                "project_tracking_option_name": str(project.get("name") if project else "")[:255],
                "confidence": confidence,
                "rationale": str(item.get("rationale") or "")[:4000],
                "review_note": review_note,
                "evidence": evidence,
                "source_hash": payout.source_hash,
                "model_name": str(item.get("model_name") or model_name or "")[:255],
                "status": ReconciliationSuggestion.STATUS_PROPOSED,
                "reviewed_by_slack_id": "",
                "reviewed_at": None,
            }
            suggestion, _created = ReconciliationSuggestion.objects.update_or_create(
                **lookup,
                defaults=defaults,
            )
            saved.append(suggestion)
    return saved


def approve_reconciliation_suggestion(
    suggestion: ReconciliationSuggestion, *, reviewed_by_slack_id: str
) -> tuple[ReconciliationSuggestion, ReconciliationMapping]:
    with transaction.atomic():
        locked = ReconciliationSuggestion.objects.select_for_update().select_related("payout").get(pk=suggestion.pk)
        if locked.status != ReconciliationSuggestion.STATUS_PROPOSED:
            raise ValueError("Only a proposed reconciliation suggestion can be approved.")
        if locked.source_hash and locked.source_hash != locked.payout.source_hash:
            raise ValueError("The Stripe payout changed after this suggestion was generated. Generate a fresh suggestion.")
        mapping, _created = ReconciliationMapping.objects.get_or_create(
            organization=locked.organization,
            source_type=locked.source_type,
            source_id=locked.source_id,
        )
        mapping.source_label = locked.source_label
        if locked.event_tracking_option_name:
            mapping.event_tracking_option_name = locked.event_tracking_option_name
        if locked.project_tracking_option_name:
            mapping.project_source_type = locked.project_source_type
            mapping.project_source_id = locked.project_source_id
            mapping.project_tracking_option_name = locked.project_tracking_option_name
        if locked.review_note:
            mapping.reconciliation_note = locked.review_note
        mapping.active = True
        mapping.save()

        locked.status = ReconciliationSuggestion.STATUS_APPROVED
        locked.reviewed_by_slack_id = reviewed_by_slack_id
        locked.reviewed_at = timezone.now()
        locked.save(update_fields=["status", "reviewed_by_slack_id", "reviewed_at", "updated_at"])
        ReconciliationSuggestion.objects.filter(
            organization=locked.organization,
            payout=locked.payout,
            source_type=locked.source_type,
            source_id=locked.source_id,
            status=ReconciliationSuggestion.STATUS_PROPOSED,
        ).exclude(pk=locked.pk).update(status=ReconciliationSuggestion.STATUS_SUPERSEDED, updated_at=timezone.now())
    return locked, mapping
