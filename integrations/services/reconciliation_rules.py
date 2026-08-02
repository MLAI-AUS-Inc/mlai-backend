"""Verified rules and durable decisions for Xero statement reconciliation."""

from __future__ import annotations

import hashlib
import json
import string
from typing import Any

from django.db.models import Q

from integrations.models import (
    ReconciliationDecision,
    ReconciliationRule,
    XeroStatementLineSnapshot,
)


ALLOWED_DESCRIPTION_FIELDS = {
    "amount",
    "contact",
    "date",
    "event",
    "merchant",
    "narration",
    "project",
    "reference",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _decision_key(*parts: Any) -> str:
    return hashlib.sha256(_stable_json(parts).encode("utf-8")).hexdigest()


def validate_description_template(value: Any) -> str:
    template = str(value or "").strip()
    if not template:
        raise ValueError("description_template is required")
    parsed = list(string.Formatter().parse(template))
    unknown = {
        field_name
        for _literal, field_name, _format_spec, _conversion in parsed
        if field_name and field_name not in ALLOWED_DESCRIPTION_FIELDS
    }
    if unknown:
        raise ValueError(
            "description_template contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    if any(format_spec or conversion for _literal, _field, format_spec, conversion in parsed):
        raise ValueError("description_template does not support format specifiers or conversions")
    return template[:4000]


def render_rule_description(
    rule: ReconciliationRule,
    line: XeroStatementLineSnapshot,
) -> str:
    values = {
        "amount": f"{line.amount:.2f}",
        "contact": rule.contact_name,
        "date": line.transaction_date.isoformat(),
        "event": rule.event_tracking_option_name,
        "merchant": rule.bank_narration_key or line.narration,
        "narration": line.narration,
        "project": rule.project_tracking_option_name,
        "reference": line.reference,
    }
    return rule.description_template.format_map(values).strip()[:4000]


def serialize_reconciliation_rule(rule: ReconciliationRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "scope": rule.scope,
        "statement_line_id": rule.statement_line.statement_line_id if rule.statement_line_id else "",
        "bank_narration_key": rule.bank_narration_key,
        "direction": rule.direction,
        "effective_from": rule.effective_from.isoformat() if rule.effective_from else None,
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
        "proposed_action": rule.proposed_action,
        "contact_name": rule.contact_name,
        "account_code": rule.account_code,
        "account_name": rule.account_name,
        "tax_type": rule.tax_type,
        "description_template": rule.description_template,
        "event": {
            "source_type": rule.event_source_type or "luma",
            "source_id": rule.event_source_id,
            "tracking_option_name": rule.event_tracking_option_name,
        } if rule.event_source_id else None,
        "project": {
            "source_type": "linear",
            "source_id": rule.project_source_id,
            "tracking_option_name": rule.project_tracking_option_name,
        } if rule.project_source_id else None,
        "priority": rule.priority,
        "status": rule.status,
        "active": rule.active,
        "evidence": rule.evidence or [],
        "notes": rule.notes,
        "verified_by_slack_id": rule.verified_by_slack_id,
        "verified_at": rule.verified_at.isoformat() if rule.verified_at else None,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


def serialize_reconciliation_decision(decision: ReconciliationDecision) -> dict[str, Any]:
    return {
        "id": decision.id,
        "statement_line_id": decision.statement_line.statement_line_id,
        "suggestion_id": decision.suggestion_id,
        "rule_id": decision.rule_id,
        "run_id": decision.run_id,
        "decision_type": decision.decision_type,
        "actor_type": decision.actor_type,
        "actor_id": decision.actor_id,
        "outcome": decision.outcome or {},
        "evidence": decision.evidence or [],
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
    }


def latest_admin_reconciliation_decision(suggestion) -> ReconciliationDecision | None:
    return suggestion.reconciliation_decisions.filter(
        decision_type__in=[
            ReconciliationDecision.TYPE_ADMIN_APPROVED,
            ReconciliationDecision.TYPE_ADMIN_REJECTED,
        ]
    ).order_by("-created_at", "-id").first()


def serialize_suggestion_approval(suggestion) -> dict[str, Any]:
    decision = latest_admin_reconciliation_decision(suggestion)
    if decision is None:
        return {"status": "pending", "decision": None}
    return {
        "status": (
            "approved"
            if decision.decision_type == ReconciliationDecision.TYPE_ADMIN_APPROVED
            else "rejected"
        ),
        "decision": serialize_reconciliation_decision(decision),
    }


def _rule_accounting_fingerprint(rule: ReconciliationRule) -> tuple[str, ...]:
    return (
        rule.proposed_action,
        rule.contact_name.casefold(),
        rule.account_code.casefold(),
        rule.account_name.casefold(),
        rule.tax_type.casefold(),
        rule.description_template,
        rule.event_source_type,
        rule.event_source_id,
        rule.project_source_id,
    )


def resolve_reconciliation_rule(
    line: XeroStatementLineSnapshot,
) -> tuple[ReconciliationRule | None, list[ReconciliationRule]]:
    """Return one unambiguous verified rule and any same-rank conflicts."""

    from integrations.services.xero_statement_reconciliation import merchant_key

    narration_key = merchant_key(line.narration)
    rules = list(
        ReconciliationRule.objects.filter(
            organization=line.organization,
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
        )
        .filter(
            Q(scope=ReconciliationRule.SCOPE_STATEMENT_LINE, statement_line=line)
            | Q(
                scope=ReconciliationRule.SCOPE_MERCHANT,
                bank_narration_key=narration_key,
                direction=line.direction,
            )
        )
        .filter(Q(effective_from__isnull=True) | Q(effective_from__lte=line.transaction_date))
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=line.transaction_date))
        .select_related("statement_line")
    )
    if not rules:
        return None, []
    rules.sort(
        key=lambda rule: (
            1 if rule.scope == ReconciliationRule.SCOPE_STATEMENT_LINE else 0,
            rule.priority,
            rule.verified_at.isoformat() if rule.verified_at else "",
            rule.id,
        ),
        reverse=True,
    )
    winner = rules[0]
    winner_rank = (
        winner.scope == ReconciliationRule.SCOPE_STATEMENT_LINE,
        winner.priority,
    )
    same_rank = [
        rule
        for rule in rules
        if (
            rule.scope == ReconciliationRule.SCOPE_STATEMENT_LINE,
            rule.priority,
        ) == winner_rank
    ]
    conflicts = [
        rule
        for rule in same_rank[1:]
        if _rule_accounting_fingerprint(rule) != _rule_accounting_fingerprint(winner)
    ]
    if conflicts:
        return None, [winner, *conflicts]
    return winner, []


def rule_evidence(rule: ReconciliationRule) -> list[dict[str, str]]:
    evidence = [
        {
            "source_provider": "admin_rule",
            "source_record_id": str(rule.id),
            "summary": f"Admin-verified reconciliation rule: {rule.name}",
        }
    ]
    for item in rule.evidence or []:
        if isinstance(item, dict):
            evidence.append(item)
    return evidence[:20]


def apply_verified_rule(
    *,
    rule: ReconciliationRule,
    line: XeroStatementLineSnapshot,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Overlay authoritative admin fields before model output is validated."""

    result = dict(item)
    try:
        prior_confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        prior_confidence = 0.0
    result.update({
        "proposed_action": rule.proposed_action,
        "contact_name": rule.contact_name,
        "account_code": rule.account_code,
        "account_name": rule.account_name,
        "tax_type": rule.tax_type,
        "description": render_rule_description(rule, line),
        "event": {
            "source_type": rule.event_source_type or "luma",
            "source_id": rule.event_source_id,
        } if rule.event_source_id else None,
        "project": {"source_type": "linear", "source_id": rule.project_source_id} if rule.project_source_id else None,
        "identity_confidence": 1.0,
        "accounting_confidence": 1.0,
        "allocation_confidence": 1.0 if (rule.event_source_id or rule.project_source_id) else 0.0,
        "confidence": max(prior_confidence, 0.99),
    })
    supplied_evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    result["evidence"] = [*rule_evidence(rule), *supplied_evidence][:20]
    return result


def record_reconciliation_decision(
    *,
    statement_line: XeroStatementLineSnapshot,
    decision_type: str,
    run_id: str = "",
    suggestion=None,
    rule: ReconciliationRule | None = None,
    actor_type: str = ReconciliationDecision.ACTOR_SYSTEM,
    actor_id: str = "",
    outcome: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    discriminator: str = "",
) -> ReconciliationDecision:
    payload = outcome or {}
    key = _decision_key(
        statement_line.organization_id,
        statement_line.id,
        statement_line.source_hash,
        decision_type,
        run_id,
        getattr(suggestion, "id", None),
        getattr(rule, "id", None),
        discriminator,
        payload,
    )
    decision, _created = ReconciliationDecision.objects.get_or_create(
        decision_key=key,
        defaults={
            "organization": statement_line.organization,
            "statement_line": statement_line,
            "suggestion": suggestion,
            "rule": rule,
            "run_id": run_id[:255],
            "decision_type": decision_type,
            "actor_type": actor_type,
            "actor_id": actor_id[:100],
            "outcome": payload,
            "evidence": evidence or [],
        },
    )
    return decision
