"""Read-only outcomes and learning candidates for statement reconciliation."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from typing import Any

from django.db.models import Count

from integrations.models import (
    ReconciliationDecision,
    ReconciliationRule,
    XeroStatementPosting,
)
from integrations.services.xero_statement_reconciliation import (
    build_statement_reconciliation_context,
)
from integrations.services.reconciliation_catalogs import (
    build_reconciliation_catalog_status,
)
from startup_updates.models import (
    LinearProjectArtifact,
    LinearProjectSelection,
    LumaEventSelection,
)


def _posting_outcome(posting: XeroStatementPosting) -> dict[str, Any]:
    line = posting.statement_line
    suggestion = posting.suggestion
    return {
        "posting_id": posting.id,
        "statement_line_id": line.statement_line_id,
        "transaction_date": line.transaction_date.isoformat(),
        "narration": line.narration,
        "direction": line.direction,
        "amount": str(line.amount),
        "currency": line.currency,
        "operation": posting.operation,
        "status": posting.status,
        "contact_name": suggestion.contact_name,
        "account_code": suggestion.account_code,
        "account_name": suggestion.account_name,
        "tax_type": suggestion.tax_type,
        "description": suggestion.description,
        "event_name": suggestion.event_tracking_option_name,
        "project_name": suggestion.project_tracking_option_name,
        "xero_bank_transaction_id": posting.xero_bank_transaction_id,
        "xero_payment_id": posting.xero_payment_id,
        "posted_at": posting.posted_at.isoformat() if posting.posted_at else None,
        "reconciled_at": posting.reconciled_at.isoformat() if posting.reconciled_at else None,
        "reconciled_scan_id": posting.reconciled_scan_id,
    }


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _description_template(description: str, *, event_name: str, project_name: str) -> str:
    """Generalise only exact catalog names; otherwise retain the accepted text."""

    template = str(description or "").strip()
    replacements = sorted(
        (
            (str(project_name or "").strip(), "{project}"),
            (str(event_name or "").strip(), "{event}"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for value, token in replacements:
        if value:
            template = template.replace(value, token)
    return template


def _candidate_review_states(*, organization) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    decisions = ReconciliationDecision.objects.filter(
        organization=organization,
        decision_type__in=[
            ReconciliationDecision.TYPE_LEARNING_RULE_PROMOTED,
            ReconciliationDecision.TYPE_LEARNING_RULE_REJECTED,
        ],
    ).select_related("rule").order_by("-created_at", "-id")
    for decision in decisions:
        candidate_id = str((decision.outcome or {}).get("candidate_id") or "")
        if not candidate_id or candidate_id in states:
            continue
        states[candidate_id] = {
            "review_status": (
                "promoted"
                if decision.decision_type == ReconciliationDecision.TYPE_LEARNING_RULE_PROMOTED
                else "rejected"
            ),
            "decision_id": decision.id,
            "rule_id": decision.rule_id,
            "reason": str((decision.outcome or {}).get("reason") or ""),
            "reviewed_by_slack_id": decision.actor_id,
            "reviewed_at": decision.created_at.isoformat() if decision.created_at else None,
        }
    return states


def _overlapping_active_rules(
    *,
    organization,
    merchant: str,
    direction: str,
    first_confirmed_date: str,
) -> list[ReconciliationRule]:
    rules = ReconciliationRule.objects.filter(
        organization=organization,
        scope=ReconciliationRule.SCOPE_MERCHANT,
        bank_narration_key=merchant,
        direction=direction,
        status=ReconciliationRule.STATUS_VERIFIED,
        active=True,
    ).order_by("-priority", "-verified_at", "-id")
    return [
        rule
        for rule in rules
        if rule.effective_to is None
        or rule.effective_to.isoformat() >= first_confirmed_date
    ]


def build_learning_candidates(*, organization) -> list[dict[str, Any]]:
    context = build_statement_reconciliation_context(
        organization=organization,
        include_external_evidence=False,
    )
    examples = [
        item
        for item in context.get("prior_xero_examples") or []
        if str(item.get("outcome_source") or "").startswith("confirmed_")
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for example in examples:
        merchant = str(example.get("merchant_key") or "").strip()
        direction = str(example.get("direction") or "").strip()
        if merchant and direction:
            grouped[(merchant, direction)].append(example)

    luma_ids = {
        item.event_name.casefold(): item.event_id
        for item in LumaEventSelection.objects.filter(organization=organization)
        if item.event_name
    }
    project_ids = {
        item.name.casefold(): item.linear_project_id
        for item in LinearProjectArtifact.objects.filter(organization=organization)
        if item.name
    }
    for item in LinearProjectSelection.objects.filter(organization=organization):
        if item.project_name:
            project_ids.setdefault(item.project_name.casefold(), item.linear_project_id)
    review_states = _candidate_review_states(organization=organization)
    catalog_status = build_reconciliation_catalog_status(organization=organization)
    results = []
    for (merchant, direction), merchant_examples in sorted(grouped.items()):
        fingerprints = Counter(
            (
                str(item.get("contact_name") or "").strip(),
                str(item.get("account_code") or "").strip(),
                str(item.get("account_name") or "").strip(),
                str(item.get("tax_type") or "").strip(),
                str(item.get("description") or "").strip(),
                str(item.get("event_name") or "").strip(),
                str(item.get("project_name") or "").strip(),
            )
            for item in merchant_examples
        )
        fingerprint, count = fingerprints.most_common(1)[0]
        (
            contact,
            account_code,
            account_name,
            tax_type,
            description,
            event_name,
            project_name,
        ) = fingerprint
        consistent = len(fingerprints) == 1
        event_source_id = luma_ids.get(event_name.casefold(), "") if event_name else ""
        project_source_id = project_ids.get(project_name.casefold(), "") if project_name else ""
        template = _description_template(
            description,
            event_name=event_name,
            project_name=project_name,
        )
        example_line_ids = sorted(
            str(item.get("statement_line_id") or "")
            for item in merchant_examples
            if str(item.get("statement_line_id") or "")
        )
        dates = sorted(
            str(item.get("transaction_date") or "")
            for item in merchant_examples
            if str(item.get("transaction_date") or "")
        )
        first_confirmed_date = dates[0] if dates else ""
        last_confirmed_date = dates[-1] if dates else ""
        candidate_basis = {
            "merchant_key": merchant,
            "direction": direction,
            "contact_name": contact,
            "account_code": account_code,
            "account_name": account_name,
            "tax_type": tax_type,
            "description_template": template,
            "event_source_id": event_source_id,
            "project_source_id": project_source_id,
        }
        candidate_id = _stable_hash(candidate_basis)[:32]
        candidate_version = _stable_hash({
            "candidate": candidate_basis,
            "catalog_source_hashes": catalog_status["source_hashes"],
            "example_statement_line_ids": example_line_ids,
            "confirmed_example_count": len(merchant_examples),
            "first_confirmed_date": first_confirmed_date,
            "last_confirmed_date": last_confirmed_date,
        })
        blocking_reasons = []
        if not consistent:
            blocking_reasons.append("Confirmed examples disagree on accounting, description, or allocation.")
        if count < 2:
            blocking_reasons.append("At least two matching confirmed examples are required.")
        if event_name and not event_source_id:
            blocking_reasons.append("The Event Name no longer resolves to a current Luma event.")
        if project_name and not project_source_id:
            blocking_reasons.append("The Project Name no longer resolves to a current Linear project.")
        if not template:
            blocking_reasons.append("A verified description template is required.")

        exact_rule = None
        conflicting_rule_ids = []
        if first_confirmed_date:
            for rule in _overlapping_active_rules(
                organization=organization,
                merchant=merchant,
                direction=direction,
                first_confirmed_date=first_confirmed_date,
            ):
                exact = (
                    rule.contact_name == contact
                    and rule.account_code == account_code
                    and rule.account_name == account_name
                    and rule.tax_type == tax_type
                    and rule.description_template == template
                    and rule.event_source_id == event_source_id
                    and rule.project_source_id == project_source_id
                )
                if exact:
                    exact_rule = exact_rule or rule
                else:
                    conflicting_rule_ids.append(rule.id)
        if conflicting_rule_ids:
            blocking_reasons.append(
                "An active merchant rule overlaps this candidate with different fields."
            )

        state = review_states.get(candidate_id, {
            "review_status": "pending",
            "decision_id": None,
            "rule_id": None,
            "reason": "",
            "reviewed_by_slack_id": "",
            "reviewed_at": None,
        })
        if (
            exact_rule
            and not conflicting_rule_ids
            and state["review_status"] != "promoted"
        ):
            state = {
                "review_status": "already_covered",
                "decision_id": state.get("decision_id"),
                "rule_id": exact_rule.id,
                "reason": "",
                "reviewed_by_slack_id": exact_rule.verified_by_slack_id,
                "reviewed_at": (
                    exact_rule.verified_at.isoformat()
                    if exact_rule.verified_at
                    else None
                ),
            }
        structurally_eligible = not blocking_reasons
        candidate = {
            "candidate_id": candidate_id,
            "candidate_version": candidate_version,
            "catalog_source_hashes": catalog_status["source_hashes"],
            "merchant_key": merchant,
            "direction": direction,
            "confirmed_example_count": len(merchant_examples),
            "matching_pattern_count": count,
            "conflicting_pattern_count": max(0, len(fingerprints) - 1),
            "first_confirmed_date": first_confirmed_date or None,
            "last_confirmed_date": last_confirmed_date or None,
            "eligible_for_rule_review": structurally_eligible,
            "eligible_for_promotion": (
                structurally_eligible
                and state["review_status"] in {"pending", "rejected"}
            ),
            "blocking_reasons": blocking_reasons,
            "conflicting_rule_ids": conflicting_rule_ids,
            **state,
            "suggested_rule": {
                "contact_name": contact,
                "account_code": account_code,
                "account_name": account_name,
                "tax_type": tax_type,
                "description_template": template,
                "effective_from": first_confirmed_date or None,
                "effective_to": None,
                "event_name": event_name,
                "event_source_id": event_source_id,
                "project_name": project_name,
                "project_source_id": project_source_id,
            },
            "example_statement_line_ids": example_line_ids[:20],
        }
        results.append(candidate)
    return results


def get_learning_candidate(*, organization, candidate_id: str) -> dict[str, Any] | None:
    requested = str(candidate_id or "").strip().lower()
    return next(
        (
            candidate
            for candidate in build_learning_candidates(organization=organization)
            if candidate["candidate_id"] == requested
        ),
        None,
    )


def build_reconciliation_outcome_summary(*, organization, limit: int = 50) -> dict[str, Any]:
    status_counts = {
        row["status"]: row["count"]
        for row in XeroStatementPosting.objects.filter(organization=organization)
        .values("status")
        .annotate(count=Count("id"))
    }
    confirmed = list(
        XeroStatementPosting.objects.filter(
            organization=organization,
            status=XeroStatementPosting.STATUS_RECONCILED,
        )
        .select_related("statement_line", "suggestion", "reconciled_scan")
        .order_by("-reconciled_at", "-id")[:limit]
    )
    candidates = build_learning_candidates(organization=organization)
    return {
        "posting_status_counts": status_counts,
        "pending_human_match_count": status_counts.get(
            XeroStatementPosting.STATUS_MATCH_READY, 0
        ),
        "confirmed_reconciled_count": status_counts.get(
            XeroStatementPosting.STATUS_RECONCILED, 0
        ),
        "recent_confirmed": [_posting_outcome(posting) for posting in confirmed],
        "learning_candidates": candidates,
        "rule_review_candidate_count": sum(
            1
            for candidate in candidates
            if candidate["eligible_for_promotion"]
            and candidate["review_status"] == "pending"
        ),
        "automatic_rule_creation": False,
    }
