"""Retain the separately approved article debit of an admitted research Run."""
from copy import deepcopy

from django.db import transaction

from .run_backend_facts import BackendFactConflict, fact_run_for_job
from .run_ownership import (
    ARTICLE_CHARGE_RESULT_KEY, OWNER_EXECUTION_KEY, current_execution_ticket, owner_receipt, wire_identity,
)
from .website_contract import evidence_digest


def article_charge_receipt(job_or_run):
    """Read a protected, original article charge without replacing research input."""
    from workflow_runs.models import ContentFactoryRun
    run = job_or_run if isinstance(job_or_run, ContentFactoryRun) else fact_run_for_job(job_or_run)
    owner = owner_receipt(run)
    if owner is None:
        return None
    raw = (run.result or {}).get(ARTICLE_CHARGE_RESULT_KEY)
    if raw is None:
        return None
    fields = {"schema_version", "organization_id", "source_admission_digest", "source_execution_id",
              "source_execution_digest", "article_request_key", "ledger_id", "cost_points", "billing_user_id",
              "requested_by_user_id", "request_digest", "request"}
    execution = (run.result or {}).get(OWNER_EXECUTION_KEY, {}).get("tickets", {}).get(raw.get("source_execution_id")) if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or set(raw) != fields or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1 or raw.get("organization_id") != str(run.organization_id)
            or raw.get("source_admission_digest") != evidence_digest(wire_identity(owner))
            or not isinstance(execution, dict) or raw.get("source_execution_digest") != evidence_digest(execution)
            or not isinstance(raw.get("request"), dict) or raw.get("request_digest") != evidence_digest(raw["request"])
            or raw["request"].get("source_run_id") != run.run_id or raw["request"].get("domain") != run.domain
            or evidence_digest(raw["request"].get("scope")) != evidence_digest(execution.get("scope"))
            or raw.get("article_request_key") != raw["request"].get("article_request_key")
            or any(type(raw.get(key)) is not type(raw["request"].get(key)) or raw.get(key) != raw["request"].get(key) for key in
                   ("ledger_id", "cost_points", "billing_user_id", "requested_by_user_id"))
            or type(raw.get("cost_points")) is not int or raw["cost_points"] < 0
            or type(raw.get("article_request_key")) is not str or not 1 <= len(raw["article_request_key"]) <= 255
            or any(type(raw.get(key)) is not str or not raw[key].isdigit() for key in ("billing_user_id", "requested_by_user_id"))
            or (raw.get("ledger_id") is not None and (type(raw["ledger_id"]) is not int or raw["ledger_id"] <= 0))):
        raise BackendFactConflict("The separate article charge receipt is not readable.")
    return deepcopy(raw)


def require_article_charge_choice(job, *, keyword, custom_title, request_key=None):
    """A retry may use only the original separately approved article intention."""
    receipt = article_charge_receipt(job)
    if receipt is None:
        return None
    intended = receipt["request"]
    if (intended.get("confirmed_keyword") != keyword or intended.get("custom_title") != custom_title
            or request_key and request_key != receipt["article_request_key"]):
        raise BackendFactConflict("The article confirmation changed its original payment intention.")
    return receipt


def record_article_charge(job, *, ledger, cost_points, billing_user, requester, article_request_key,
                          confirmed_keyword, custom_title):
    """Seal verified spend before fact projection in the same transaction.

    This function performs no debit or refund. Its caller retains the existing
    actual requester, quote, activation, original-key and PointsService checks.
    """
    if not transaction.get_connection().in_atomic_block:
        raise BackendFactConflict("An article charge requires its original source transaction.")
    run = fact_run_for_job(job, lock=True)
    if run is None:
        return None
    current = article_charge_receipt(run)
    if current is not None:
        if (current["article_request_key"] != article_request_key or current["ledger_id"] != getattr(ledger, "pk", None)
                or current["cost_points"] != cost_points or current["billing_user_id"] != str(getattr(billing_user, "pk", None))
                or current["requested_by_user_id"] != str(getattr(requester, "pk", None))
                or current["request"].get("confirmed_keyword") != confirmed_keyword
                or current["request"].get("custom_title") != custom_title):
            raise BackendFactConflict("The separately approved article charge cannot be replaced.")
        return current
    if type(cost_points) is not int or cost_points < 0 or billing_user is None or requester is None:
        raise BackendFactConflict("A verified original article charge is required.")
    if cost_points:
        from integrations.services.article_generation import _ledger_reference_id
        if (ledger is None or ledger.user_id != billing_user.pk or ledger.kind != "SPEND" or ledger.source != "CONTENT_FACTORY"
                or ledger.idempotency_key != "content_factory:charge:" + article_request_key
                or ledger.reference_type != "CONTENT_FACTORY" or ledger.reference_id != _ledger_reference_id(article_request_key)
                or ledger.delta_microroo != -cost_points * 1_000_000):
            raise BackendFactConflict("The article spend does not match its approved source.")
    elif ledger is not None:
        raise BackendFactConflict("An uncharged article cannot name an unrelated spend.")
    ticket = current_execution_ticket(run)
    meta = job.request_meta or {}
    inputs = {"source_run_id": run.run_id, "domain": run.domain, "confirmed_keyword": confirmed_keyword,
        "custom_title": custom_title, "delivery_mode": meta.get("delivery_mode"),
        "delivery_mode_confirmed": meta.get("delivery_mode_confirmed"), "scope": deepcopy(ticket["scope"]),
        "article_request_key": article_request_key, "ledger_id": getattr(ledger, "pk", None),
        "cost_points": cost_points, "billing_user_id": str(billing_user.pk), "requested_by_user_id": str(requester.pk)}
    receipt = {"schema_version": 1, "organization_id": str(run.organization_id),
        "source_admission_digest": evidence_digest(wire_identity(owner_receipt(run))),
        "source_execution_id": ticket["execution_id"], "source_execution_digest": evidence_digest(ticket),
        **{key: inputs[key] for key in ("article_request_key", "ledger_id", "cost_points", "billing_user_id", "requested_by_user_id")},
        "request_digest": evidence_digest(inputs), "request": inputs}
    from workflow_runs.models import ContentFactoryRun
    ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, ARTICLE_CHARGE_RESULT_KEY: receipt})
    return deepcopy(receipt)
