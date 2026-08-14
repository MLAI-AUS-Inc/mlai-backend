"""Sanitized, versioned knowledge export for the reconciliation agent.

The export is deliberately read-only and contains only durable admin-approved
or explicitly selected reconciliation context.  Connector credentials, raw
provider payloads, message bodies, email addresses, bank-account details and
attachment contents must never enter this contract.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from integrations.models import (
    ReconciliationMapping,
    ReconciliationPartyIdentity,
    ReconciliationProfile,
    ReconciliationRule,
    XeroStatementPosting,
)
from integrations.services.reconciliation_outcomes import build_learning_candidates
from startup_updates.models import (
    LinearProjectArtifact,
    LinearProjectMemberArtifact,
    LinearProjectSelection,
    LumaEventSelection,
)


KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_POLICY_VERSION = "reconciliation-knowledge-v1"
KNOWLEDGE_SOURCE = "mlai-backend"


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_value(value: Any) -> Any:
    """Remove observation times before hashing otherwise every pull conflicts."""

    if isinstance(value, dict):
        return {
            key: _semantic_value(item)
            for key, item in value.items()
            if key not in {"exported_at", "fetched_at", "source_hash"}
        }
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    return value


def _iso(value) -> str | None:
    if not value:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _record(
    *,
    fetched_at: str,
    record_type: str,
    record_id: str,
    data: dict[str, Any],
    effective_from=None,
    effective_to=None,
    verified_by: str = "",
    verified_at=None,
    version: str = "",
) -> dict[str, Any]:
    semantic = {
        "record_type": record_type,
        "record_id": str(record_id),
        "effective_from": _iso(effective_from),
        "effective_to": _iso(effective_to),
        "verified_by": str(verified_by or ""),
        "verified_at": _iso(verified_at),
        "data": data,
    }
    return {
        "source_backend": KNOWLEDGE_SOURCE,
        **semantic,
        "version": version or _stable_hash(semantic),
        "fetched_at": fetched_at,
    }


def _profile_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    profile = ReconciliationProfile.objects.filter(organization=organization).first()
    if profile is None:
        return []
    data = {
        "xero_contact_id": profile.xero_contact_id,
        "xero_contact_name": profile.xero_contact_name,
        "humanitix_contact_id": profile.humanitix_contact_id,
        "humanitix_contact_name": profile.humanitix_contact_name,
        "revenue_account_code": profile.revenue_account_code,
        "fee_account_code": profile.fee_account_code,
        "refund_account_code": profile.refund_account_code,
        "revenue_tax_type": profile.revenue_tax_type,
        "fee_tax_type": profile.fee_tax_type,
        "refund_tax_type": profile.refund_tax_type,
        "line_amount_types": profile.line_amount_types,
        "event_tracking_category_id": profile.event_tracking_category_id,
        "event_tracking_category_name": profile.event_tracking_category_name,
        "project_tracking_category_id": profile.project_tracking_category_id,
        "project_tracking_category_name": profile.project_tracking_category_name,
        "require_statement_tracking": profile.require_statement_tracking,
        "default_project_tracking_option_id": profile.default_project_tracking_option_id,
        "default_project_tracking_option_name": profile.default_project_tracking_option_name,
        "standalone_fee_project_option_id": profile.standalone_fee_project_option_id,
        "standalone_fee_project_option_name": profile.standalone_fee_project_option_name,
        "enabled": profile.enabled,
    }
    return [_record(
        fetched_at=fetched_at,
        record_type="reconciliation_profile",
        record_id=str(profile.id),
        data=data,
        version=_iso(profile.updated_at) or _stable_hash(data),
    )]


def _mapping_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    mappings = ReconciliationMapping.objects.filter(organization=organization).order_by(
        "source_type", "source_id", "id"
    )
    for mapping in mappings:
        data = {
            "source_type": mapping.source_type,
            "source_id": mapping.source_id,
            "source_label": mapping.source_label,
            "accounting_treatment": mapping.accounting_treatment,
            "event_tracking_option_id": mapping.event_tracking_option_id,
            "event_tracking_option_name": mapping.event_tracking_option_name,
            "project_tracking_option_id": mapping.project_tracking_option_id,
            "project_tracking_option_name": mapping.project_tracking_option_name,
            "project_source_type": mapping.project_source_type,
            "project_source_id": mapping.project_source_id,
            "account_code": mapping.account_code,
            "tax_type": mapping.tax_type,
            "active": mapping.active,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="reconciliation_mapping",
            record_id=str(mapping.id),
            data=data,
            version=_iso(mapping.updated_at) or _stable_hash(data),
        ))
    return records


def _identity_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    identities = ReconciliationPartyIdentity.objects.filter(
        organization=organization,
        status=ReconciliationPartyIdentity.STATUS_VERIFIED,
        active=True,
    ).order_by("id")
    for identity in identities:
        data = {
            "bank_narration_key": identity.bank_narration_key,
            "direction": identity.direction,
            "canonical_name": identity.canonical_name,
            "xero_contact_id": identity.xero_contact_id,
            "xero_contact_name": identity.xero_contact_name,
            "linear_user_id": identity.linear_user_id,
            "linear_name": identity.linear_name,
            "confidence": identity.confidence,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="verified_party_identity",
            record_id=str(identity.id),
            data=data,
            verified_by=identity.verified_by_slack_id,
            verified_at=identity.verified_at,
            version=_iso(identity.updated_at) or _stable_hash(data),
        ))
    return records


def _rule_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    rules = ReconciliationRule.objects.filter(
        organization=organization,
        status=ReconciliationRule.STATUS_VERIFIED,
        active=True,
    ).select_related("statement_line").order_by("-priority", "id")
    for rule in rules:
        data = {
            "name": rule.name,
            "scope": rule.scope,
            "statement_line_id": (
                rule.statement_line.statement_line_id if rule.statement_line_id else ""
            ),
            "bank_narration_key": rule.bank_narration_key,
            "direction": rule.direction,
            "proposed_action": rule.proposed_action,
            "contact_name": rule.contact_name,
            "account_code": rule.account_code,
            "account_name": rule.account_name,
            "tax_type": rule.tax_type,
            "description_template": rule.description_template,
            "event_source_type": rule.event_source_type,
            "event_source_id": rule.event_source_id,
            "event_tracking_option_name": rule.event_tracking_option_name,
            "allocation_mode": rule.allocation_mode,
            "project_source_type": rule.project_source_type,
            "project_source_id": rule.project_source_id,
            "project_tracking_option_id": rule.project_tracking_option_id,
            "project_tracking_option_name": rule.project_tracking_option_name,
            "priority": rule.priority,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="verified_reconciliation_rule",
            record_id=str(rule.id),
            data=data,
            effective_from=rule.effective_from,
            effective_to=rule.effective_to,
            verified_by=rule.verified_by_slack_id,
            verified_at=rule.verified_at,
            version=_iso(rule.updated_at) or _stable_hash(data),
        ))
    return records


def _tracking_option_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    profile = ReconciliationProfile.objects.filter(organization=organization).first()
    if profile is None:
        return []
    values: set[tuple[str, str, str]] = set()
    if profile.standalone_fee_project_option_id or profile.standalone_fee_project_option_name:
        values.add((
            "project",
            profile.standalone_fee_project_option_id,
            profile.standalone_fee_project_option_name,
        ))
    if profile.default_project_tracking_option_id or profile.default_project_tracking_option_name:
        values.add((
            "project",
            profile.default_project_tracking_option_id,
            profile.default_project_tracking_option_name,
        ))
    for mapping in ReconciliationMapping.objects.filter(
        organization=organization,
        active=True,
    ):
        if mapping.event_tracking_option_id or mapping.event_tracking_option_name:
            values.add((
                "event",
                mapping.event_tracking_option_id,
                mapping.event_tracking_option_name,
            ))
        if mapping.project_tracking_option_id or mapping.project_tracking_option_name:
            values.add((
                "project",
                mapping.project_tracking_option_id,
                mapping.project_tracking_option_name,
            ))
    categories = {
        "event": (
            profile.event_tracking_category_id,
            profile.event_tracking_category_name,
        ),
        "project": (
            profile.project_tracking_category_id,
            profile.project_tracking_category_name,
        ),
    }
    records = []
    for kind, option_id, option_name in sorted(values):
        category_id, category_name = categories[kind]
        data = {
            "kind": kind,
            "tracking_category_id": category_id,
            "tracking_category_name": category_name,
            "tracking_option_id": option_id,
            "tracking_option_name": option_name,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="xero_tracking_option",
            record_id=_stable_hash(data)[:32],
            data=data,
        ))
    return records


def _selected_event_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    events = LumaEventSelection.objects.filter(
        organization=organization,
        selected=True,
    ).order_by("event_id", "id")
    for event in events:
        data = {
            "event_id": event.event_id,
            "event_name": event.event_name,
            "start_at": _iso(event.start_at),
            "selected": True,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="selected_luma_event",
            record_id=str(event.id),
            data=data,
            effective_from=event.start_at,
            version=_iso(event.updated_at) or _stable_hash(data),
        ))
    return records


def _selected_project_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    selected = list(LinearProjectSelection.objects.filter(
        organization=organization,
        selected=True,
    ).order_by("linear_project_id", "id"))
    artifacts = {}
    for item in LinearProjectArtifact.objects.filter(
        organization=organization,
        linear_project_id__in=[selection.linear_project_id for selection in selected],
    ).order_by("linear_project_id", "-updated_at", "-id"):
        artifacts.setdefault(item.linear_project_id, item)
    for selection in selected:
        artifact = artifacts.get(selection.linear_project_id)
        data = {
            "linear_project_id": selection.linear_project_id,
            "project_name": (
                artifact.name if artifact is not None else selection.project_name
            ),
            "project_status": (
                artifact.status_name if artifact is not None else selection.project_status
            ),
            "project_health": (
                artifact.health if artifact is not None else selection.project_health
            ),
            "start_date": _iso(artifact.start_date) if artifact is not None else None,
            "target_date": _iso(artifact.target_date) if artifact is not None else None,
            "selected": True,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="selected_linear_project",
            record_id=str(selection.id),
            data=data,
            effective_from=artifact.start_date if artifact is not None else None,
            effective_to=artifact.target_date if artifact is not None else None,
            version=_iso(selection.updated_at) or _stable_hash(data),
        ))
    return records


def _selected_project_member_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    selected_ids = set(LinearProjectSelection.objects.filter(
        organization=organization,
        selected=True,
    ).values_list("linear_project_id", flat=True))
    records = []
    members = LinearProjectMemberArtifact.objects.filter(
        organization=organization,
        project__linear_project_id__in=selected_ids,
        active=True,
    ).select_related("project").order_by("project__linear_project_id", "linear_user_id", "id")
    for member in members:
        data = {
            "linear_project_id": member.project.linear_project_id,
            "linear_user_id": member.linear_user_id,
            "name": member.name,
            "membership_source": member.membership_source,
            "active": True,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="selected_linear_project_member",
            record_id=str(member.id),
            data=data,
            version=_iso(member.updated_at) or _stable_hash(data),
        ))
    return records


def _confirmed_outcome_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    postings = XeroStatementPosting.objects.filter(
        organization=organization,
        status=XeroStatementPosting.STATUS_RECONCILED,
    ).select_related("statement_line", "suggestion").order_by("id")
    for posting in postings:
        suggestion = posting.suggestion
        data = {
            "operation": posting.operation,
            "contact_name": suggestion.contact_name,
            "account_code": suggestion.account_code,
            "account_name": suggestion.account_name,
            "tax_type": suggestion.tax_type,
            "allocation_mode": suggestion.allocation_mode,
            "event_tracking_option_name": suggestion.event_tracking_option_name,
            "project_tracking_option_name": suggestion.project_tracking_option_name,
            "status": posting.status,
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="confirmed_reconciliation_outcome",
            record_id=str(posting.id),
            data=data,
            effective_from=posting.statement_line.transaction_date,
            verified_by=posting.requested_by_slack_id,
            verified_at=posting.reconciled_at,
            version=_iso(posting.updated_at) or posting.source_hash,
        ))
    return records


def _learning_candidate_records(*, organization, fetched_at: str) -> list[dict[str, Any]]:
    records = []
    for candidate in build_learning_candidates(organization=organization):
        suggested = candidate.get("suggested_rule") or {}
        data = {
            "candidate_id": candidate.get("candidate_id"),
            "merchant_key": candidate.get("merchant_key"),
            "direction": candidate.get("direction"),
            "confirmed_example_count": candidate.get("confirmed_example_count"),
            "matching_pattern_count": candidate.get("matching_pattern_count"),
            "conflicting_pattern_count": candidate.get("conflicting_pattern_count"),
            "eligible_for_rule_review": candidate.get("eligible_for_rule_review"),
            "eligible_for_promotion": candidate.get("eligible_for_promotion"),
            "review_status": candidate.get("review_status"),
            "blocking_reasons": candidate.get("blocking_reasons") or [],
            "conflicting_rule_ids": candidate.get("conflicting_rule_ids") or [],
            "suggested_rule": {
                "contact_name": suggested.get("contact_name"),
                "account_code": suggested.get("account_code"),
                "account_name": suggested.get("account_name"),
                "tax_type": suggested.get("tax_type"),
                "description_template": suggested.get("description_template"),
                "effective_from": suggested.get("effective_from"),
                "effective_to": suggested.get("effective_to"),
                "event_name": suggested.get("event_name"),
                "event_source_id": suggested.get("event_source_id"),
                "project_name": suggested.get("project_name"),
                "project_source_id": suggested.get("project_source_id"),
            },
        }
        records.append(_record(
            fetched_at=fetched_at,
            record_type="reconciliation_learning_candidate",
            record_id=str(candidate.get("candidate_id") or ""),
            data=data,
            effective_from=candidate.get("first_confirmed_date"),
            effective_to=candidate.get("last_confirmed_date"),
            verified_by=str(candidate.get("reviewed_by_slack_id") or ""),
            verified_at=candidate.get("reviewed_at"),
            version=str(candidate.get("candidate_version") or ""),
        ))
    return records


def _approved_tuple_records(
    *,
    organization,
    fetched_at: str,
    rules: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    records = []
    seen = set()
    for source in [*rules, *outcomes]:
        data = source["data"]
        accounting_tuple = {
            "operation": data.get("proposed_action") or data.get("operation"),
            "contact_name": data.get("contact_name"),
            "account_code": data.get("account_code"),
            "account_name": data.get("account_name"),
            "tax_type": data.get("tax_type"),
            "event_tracking_option_name": data.get("event_tracking_option_name"),
            "project_tracking_option_name": data.get("project_tracking_option_name"),
        }
        tuple_hash = _stable_hash(accounting_tuple)
        if tuple_hash in seen:
            continue
        seen.add(tuple_hash)
        records.append(_record(
            fetched_at=fetched_at,
            record_type="approved_accounting_tuple",
            record_id=tuple_hash[:32],
            data=accounting_tuple,
            effective_from=source.get("effective_from"),
            effective_to=source.get("effective_to"),
            verified_by=source.get("verified_by", ""),
            verified_at=source.get("verified_at"),
            version=tuple_hash,
        ))
    return records


def build_reconciliation_knowledge_export(*, organization, fetched_at=None) -> dict[str, Any]:
    """Return the stable, sanitized production knowledge snapshot."""

    observed_at = fetched_at or datetime.now(timezone.utc)
    fetched_at_iso = _iso(observed_at)
    rules = _rule_records(organization=organization, fetched_at=fetched_at_iso)
    outcomes = _confirmed_outcome_records(
        organization=organization,
        fetched_at=fetched_at_iso,
    )
    collections = {
        "profile": _profile_records(organization=organization, fetched_at=fetched_at_iso),
        "mappings": _mapping_records(organization=organization, fetched_at=fetched_at_iso),
        "party_identities": _identity_records(
            organization=organization,
            fetched_at=fetched_at_iso,
        ),
        "rules": rules,
        "xero_tracking_options": _tracking_option_records(
            organization=organization,
            fetched_at=fetched_at_iso,
        ),
        "selected_luma_events": _selected_event_records(
            organization=organization,
            fetched_at=fetched_at_iso,
        ),
        "selected_linear_projects": _selected_project_records(
            organization=organization,
            fetched_at=fetched_at_iso,
        ),
        "selected_linear_project_members": _selected_project_member_records(
            organization=organization,
            fetched_at=fetched_at_iso,
        ),
        "confirmed_outcomes": outcomes,
        "learning_candidates": _learning_candidate_records(
            organization=organization,
            fetched_at=fetched_at_iso,
        ),
        "approved_accounting_tuples": _approved_tuple_records(
            organization=organization,
            fetched_at=fetched_at_iso,
            rules=rules,
            outcomes=outcomes,
        ),
    }
    counts = {name: len(records) for name, records in collections.items()}
    source_hashes = {
        name: _stable_hash(_semantic_value(records))
        for name, records in collections.items()
    }
    semantic_bundle = {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "source_backend": KNOWLEDGE_SOURCE,
        "organization": {
            "id": organization.id,
            "domain": organization.domain,
        },
        "policy_version": KNOWLEDGE_POLICY_VERSION,
        "collections": _semantic_value(collections),
    }
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "source_backend": KNOWLEDGE_SOURCE,
        "organization": semantic_bundle["organization"],
        "exported_at": fetched_at_iso,
        "policy": {
            "version": KNOWLEDGE_POLICY_VERSION,
            "source_hashes": source_hashes,
        },
        "counts": counts,
        "collections": collections,
        "source_hash": _stable_hash(semantic_bundle),
    }
