"""Settle only an exact dispatch durably withdrawn before any CF enqueue.

A missing-key GET is a candidate, never a financial proof. CF's immutable
tombstone shares its decision-to-enqueue lock and refuses every delayed POST.
The existing original-charge controllers retain all money/entitlement policy.
"""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import re
from time import monotonic
from uuid import uuid4

import requests
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .run_event_authority import RunEventInvalid, canonical_bytes
from .run_ownership import (
    EVENT_RESULT_KEY, RECONCILIATION_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, RunOwnershipConflict,
    current_execution_ticket, owner_receipt, require_event_execution,
    require_event_identity, wire_identity,
)

WITHDRAWAL_PATH = "/api/runs/withdraw-dispatch"
MAX_WITHDRAWAL_BYTES = 131_072
MAX_WITHDRAWAL_SECONDS = 15
CLAIM_SECONDS = 60
MAX_CHARGE_FAMILY = 100
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_RECEIPT_KEYS = frozenset({"schema_version", "status", "client_request_id", WIRE_REQUEST_KEY,
                           WIRE_EXECUTION_KEY, "closed_at", "receipt_sha256"})
_refund_authority = ContextVar("run_original_dispatch_refund_authority", default=None)


class DispatchSettlementPending(ValueError):
    """The original charge must stay held; no replacement payer is inferred."""


def withdrawal_digest(receipt):
    """Exact CF receipt hash, without sanitizing or dropping internal echoes."""
    return hashlib.sha256(canonical_bytes({key: value for key, value in receipt.items()
                                          if key != "receipt_sha256"})).hexdigest()


def validate_closed_receipt(run, payload):
    """Validate an authenticated CF response against the exact current owner.

    The digest establishes immutable receipt bytes; authentication comes from
    the configured CF service connection, not from this unkeyed SHA256.
    This function alone does not authorize a refund or entitlement release.
    """
    receipt = owner_receipt(run)
    if (receipt is None or not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("status") != "closed_no_effects"
            or type(payload.get("client_request_id")) is not str
            or payload["client_request_id"] != receipt["identity"]["dispatch_key"]
            or type(payload.get("closed_at")) is not str or not 1 <= len(payload["closed_at"]) <= 40
            or type(payload.get("receipt_sha256")) is not str
            or _SHA256.fullmatch(payload["receipt_sha256"]) is None):
        raise DispatchSettlementPending("withdrawal_receipt_invalid")
    try:
        closed = datetime.fromisoformat(payload["closed_at"].replace("Z", "+00:00"))
        if closed.utcoffset() is None:
            raise ValueError("Naive closure timestamp")
        require_event_identity(run, payload)
        require_event_execution(run, payload, current=True)
        digest = withdrawal_digest(payload)
    except (RunOwnershipConflict, RunEventInvalid, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise DispatchSettlementPending("withdrawal_receipt_invalid") from exc
    if not hmac.compare_digest(payload["receipt_sha256"], digest):
        raise DispatchSettlementPending("withdrawal_receipt_digest_mismatch")
    return deepcopy(payload)


def _withdraw(run):
    """Request the atomic exact-key fence outside every database transaction."""
    from .reconciliation import _remote_base_url, _remote_headers
    from .website_connections import require_unlocked_remote_call
    if connection.in_atomic_block:
        raise DispatchSettlementPending("withdrawal_requires_unlocked_http")
    require_unlocked_remote_call()
    base_url, headers = _remote_base_url(), _remote_headers()
    if not base_url or not headers.get("X-API-KEY"):
        raise DispatchSettlementPending("withdrawal_unavailable")
    receipt = owner_receipt(run)
    if receipt is None:
        raise DispatchSettlementPending("withdrawal_admission_missing")
    body = {"schema_version": 1, "client_request_id": receipt["identity"]["dispatch_key"],
            WIRE_REQUEST_KEY: wire_identity(receipt), WIRE_EXECUTION_KEY: current_execution_ticket(run)}
    deadline = monotonic() + MAX_WITHDRAWAL_SECONDS
    response = requests.post(base_url + WITHDRAWAL_PATH, json=body, headers=headers,
                             timeout=(3, 10), stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            # Claimed, failed, queued, started, uncertain and old endpoints all
            # remain held. Their error body can never become no-effect proof.
            raise DispatchSettlementPending("withdrawal_unconfirmed")
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=1):
            if len(raw) + len(chunk) > MAX_WITHDRAWAL_BYTES or monotonic() > deadline:
                raise DispatchSettlementPending("withdrawal_response_unbounded")
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DispatchSettlementPending("withdrawal_response_invalid") from exc
        return validate_closed_receipt(run, payload)
    finally:
        response.close()


def _meta(run):
    value = (run.result or {}).get(RECONCILIATION_RESULT_KEY)
    return deepcopy(value) if isinstance(value, dict) else {}


def _original_request_intact(run):
    from .website_contract import evidence_digest
    receipt = owner_receipt(run)
    return receipt is not None and evidence_digest({key: value for key, value in (run.run_request or {}).items()
        if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}) == receipt["identity"]["request_digest"]


def _save_meta(run, meta):
    from workflow_runs.models import ContentFactoryRun
    result = {**run.result, RECONCILIATION_RESULT_KEY: meta}
    ContentFactoryRun.objects.filter(pk=run.pk).update(result=result)
    run.result = result


def _closed(run):
    ticket = current_execution_ticket(run)
    closures = _meta(run).get("dispatch_withdrawals", {})
    if not isinstance(closures, dict):
        raise DispatchSettlementPending("withdrawal_history_invalid")
    raw = closures.get(ticket["execution_id"])
    return validate_closed_receipt(run, raw) if raw is not None else None


def _locked_run(pk, organization_id):
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .run_reconciliation import authoritative_run
    organization = Organization.objects.select_for_update().filter(pk=organization_id).first()
    run = ContentFactoryRun.objects.select_for_update().filter(pk=pk).first()
    if (organization is None or run is None or run.organization_id != organization.pk
            or not authoritative_run(run) or owner_receipt(run)["identity"]["organization_id"] != str(organization.pk)
            or not _original_request_intact(run)):
        raise DispatchSettlementPending("withdrawal_owner_changed")
    return run


def _claim(run, now):
    from .run_reconciliation import no_effect_dispatch_proof, _pending, _time
    with transaction.atomic():
        run = _locked_run(run.pk, run.organization_id)
        meta = _meta(run)
        if _closed(run) is not None:
            return run, None
        if _spend(run) is None and _free_obligation_state(run) not in {"reserved", "released"}:
            raise DispatchSettlementPending("withdrawal_original_obligation_unverified")
        candidate = no_effect_dispatch_proof(run)
        if candidate is None or not _pending(run):
            raise DispatchSettlementPending("withdrawal_candidate_missing")
        previous = meta.get("withdrawal_claim")
        if isinstance(previous, dict):
            until = _time(previous.get("until"))
            if until is not None and until > now:
                raise DispatchSettlementPending("withdrawal_claim_active")
        ticket = current_execution_ticket(run)
        claim = {"nonce": uuid4().hex, "proof_id": candidate["proof_id"], "execution_id": ticket["execution_id"],
                 "until": (now + timedelta(seconds=CLAIM_SECONDS)).isoformat()}
        meta["withdrawal_claim"] = claim
        _save_meta(run, meta)
        return run, claim


def _accept(run, claim, receipt):
    from .run_reconciliation import no_effect_dispatch_proof
    with transaction.atomic():
        run = _locked_run(run.pk, run.organization_id)
        meta = _meta(run)
        candidate = no_effect_dispatch_proof(run)
        if (meta.get("withdrawal_claim") != claim or candidate is None
                or candidate["proof_id"] != claim["proof_id"]
                or current_execution_ticket(run)["execution_id"] != claim["execution_id"]):
            raise DispatchSettlementPending("withdrawal_claim_superseded")
        receipt = validate_closed_receipt(run, receipt)
        closures = meta.setdefault("dispatch_withdrawals", {})
        old = closures.get(claim["execution_id"])
        if old is not None and canonical_bytes(old) != canonical_bytes(receipt):
            raise DispatchSettlementPending("withdrawal_receipt_conflict")
        closures[claim["execution_id"]] = receipt
        meta.pop("withdrawal_claim", None)
        _save_meta(run, meta)
        return run


def _failed_claim(run, claim):
    with transaction.atomic():
        run = _locked_run(run.pk, run.organization_id)
        meta = _meta(run)
        if meta.get("withdrawal_claim") == claim:
            meta.pop("withdrawal_claim", None)
            _save_meta(run, meta)


def _spend(run):
    from types import SimpleNamespace
    from .billing_lineage import original_spend_for_job
    # The canonical resolver checks the actual owner itself; this shape avoids
    # requiring compatibility output to exist before an initial dispatch fails.
    return original_spend_for_job(SimpleNamespace(run=run, domain=run.domain))


def _family_query(ledger_id):
    from .billing_lineage import _ledger_id
    ledger_id = _ledger_id(ledger_id)
    if ledger_id is None:
        raise DispatchSettlementPending("withdrawal_original_obligation_invalid")
    return (Q(run_request__roo_points_ledger_id=ledger_id)
        | Q(run_request__roo_points_ledger_id=str(ledger_id))
        | Q(billing_ledger_id=ledger_id)
        | Q(result__run_owner_article_charge_v1__ledger_id=ledger_id))


def _free_original(run):
    from .no_delivery_refunds import _original_free_run
    source = _original_free_run(run)
    if (source is None or source.organization_id != run.organization_id or source.domain != run.domain
            or str((source.run_request or {}).get("roo_points_ledger_id")) != str((run.run_request or {}).get("roo_points_ledger_id"))):
        raise DispatchSettlementPending("withdrawal_entitlement_lineage_invalid")
    return source


def _free_obligation_state(run):
    from .first_article_entitlement import _reference, _rows, entitled_request, entitlement_payload
    saved = run.run_request or {}
    if not entitled_request(saved):
        return None
    if saved.get("roo_points_action") == "content_island_topic_generation":
        saved = _free_original(run).run_request
        rows = _rows(run.organization_id)
        if (rows.topics is not None and str(rows.topics.pk) == str(saved.get("roo_points_ledger_id"))
                and rows.topics.reference_id == _reference(saved.get("client_request_id"))):
            return "released" if rows.topics_released else "reserved"
        # Existing policy permits only one free re-run. No new release marker
        # or additional re-run is invented for a different/retry grant.
        return None
    return entitlement_payload(run).get("entitlementStatus")


@contextmanager
def original_dispatch_refund_authority(run, *, closed_receipt, original_spend):
    """Permit the existing controller only for this proved original obligation.

    This internal scope is created by the locked settlement consumer after its
    family checks. It cannot be granted by a request boolean or a worker event.
    """
    from workflow_runs.models import ContentFactoryRun
    from .billing_lineage import ARTICLE_CHARGE_PREFIX, TOPIC_CHARGE_PREFIX
    from .first_article_entitlement import entitled_request
    from .website_contract import evidence_digest
    if not connection.in_atomic_block:
        raise DispatchSettlementPending("withdrawal_authority_requires_transaction")
    fresh = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
    if not _original_request_intact(fresh):
        raise DispatchSettlementPending("withdrawal_authority_request_changed")
    retained = _closed(fresh)
    if retained is None or canonical_bytes(retained) != canonical_bytes(closed_receipt):
        raise DispatchSettlementPending("withdrawal_authority_missing")
    actual = _spend(fresh)
    saved = fresh.run_request or {}
    if original_spend is not None:
        if (actual is None or actual.pk != original_spend.pk or actual.user_id != original_spend.user_id
                or actual.idempotency_key != original_spend.idempotency_key
                or actual.delta_microroo != original_spend.delta_microroo):
            raise DispatchSettlementPending("withdrawal_authority_charge_changed")
        prefix = TOPIC_CHARGE_PREFIX if actual.idempotency_key.startswith(TOPIC_CHARGE_PREFIX) else ARTICLE_CHARGE_PREFIX
        if not actual.idempotency_key.startswith(prefix):
            raise DispatchSettlementPending("withdrawal_authority_charge_invalid")
        key, ledger_id, user_id = actual.idempotency_key.removeprefix(prefix), actual.pk, actual.user_id
    else:
        if (actual is not None or not entitled_request(saved)
                or _free_obligation_state(fresh) not in {"reserved", "released"}):
            raise DispatchSettlementPending("withdrawal_authority_entitlement_invalid")
        key, ledger_id, user_id = _free_original(fresh).run_request.get("client_request_id"), saved.get("roo_points_ledger_id"), None
    family = list(ContentFactoryRun.objects.select_for_update().filter(_family_query(ledger_id))
                  .order_by("pk")[:MAX_CHARGE_FAMILY + 1])
    if len(family) > MAX_CHARGE_FAMILY or not any(other.pk == fresh.pk for other in family):
        raise DispatchSettlementPending("withdrawal_family_unbounded")
    _no_effect_family(fresh, family, ledger_id=ledger_id)
    scope = {"pk": fresh.pk, "organization_id": fresh.organization_id, "domain": fresh.domain,
        "execution_id": current_execution_ticket(fresh)["execution_id"], "closure_sha256": retained["receipt_sha256"],
        "admission_digest": evidence_digest(wire_identity(owner_receipt(fresh))), "original_key": key,
        "ledger_id": str(ledger_id), "user_id": user_id, "free": original_spend is None}
    token = _refund_authority.set(scope)
    try:
        yield
    finally:
        _refund_authority.reset(token)


def refund_authority_matches(*, user, article_request, resolved_domain):
    """Read an exact active internal refund scope; absent scope always refuses."""
    from workflow_runs.models import ContentFactoryRun
    from .billing import normalize_content_factory_domain
    from .website_contract import evidence_digest
    scope = _refund_authority.get()
    if scope is None:
        from .run_terminal_settlement import terminal_refund_authority_matches
        return terminal_refund_authority_matches(user=user, article_request=article_request, resolved_domain=resolved_domain)
    if not isinstance(scope, dict) or not connection.in_atomic_block or not isinstance(article_request, dict):
        return False
    if (normalize_content_factory_domain(resolved_domain) != normalize_content_factory_domain(scope["domain"])
            or normalize_content_factory_domain(article_request.get("domain")) != normalize_content_factory_domain(scope["domain"])
            or article_request.get("client_request_id") != scope["original_key"]
            or str(article_request.get("roo_points_ledger_id")) != scope["ledger_id"]
            or getattr(user, "pk", None) != scope["user_id"]
            or not scope["free"] and str(article_request.get("roo_points_billing_user_id")) != str(scope["user_id"])):
        return False
    run = ContentFactoryRun.objects.filter(pk=scope["pk"], organization_id=scope["organization_id"]).first()
    if run is None:
        return False
    try:
        retained = _closed(run)
        return (_original_request_intact(run) and retained is not None and retained["receipt_sha256"] == scope["closure_sha256"]
            and current_execution_ticket(run)["execution_id"] == scope["execution_id"]
            and evidence_digest(wire_identity(owner_receipt(run))) == scope["admission_digest"])
    except (DispatchSettlementPending, RunOwnershipConflict, ValueError, TypeError, KeyError):
        return False


def dispatch_closed_without_effects(run):
    """Whether retained exact closure can replace a provisional active label.

    This read never authorizes a financial effect or supplies a lifecycle
    status. Legacy and reservation-only executions cannot opt into it.
    """
    from .article_publication_evidence import publication_state
    from .no_delivery_refunds import _direct_delivery, points_receipt
    from .run_terminal_settlement import terminal_closed_without_effects
    if terminal_closed_without_effects(run):
        return True
    try:
        return (_original_request_intact(run) and _closed(run) is not None
            and not (run.result or {}).get(EVENT_RESULT_KEY, {}).get("events")
            and not _direct_delivery(run) and points_receipt(run).get("state") not in {"captured", "kept"}
            and publication_state(run) == "none")
    except (DispatchSettlementPending, RunOwnershipConflict, ValueError, TypeError, KeyError):
        return False


def _no_effect_family(run, family, *, ledger_id):
    from .article_publication_evidence import publication_state
    from .billing_lineage import original_spend_for_run
    from .no_delivery_refunds import _direct_delivery, points_receipt
    from .run_article_charge import article_charge_receipt
    from .run_reconciliation import authoritative_run
    for other in family:
        if (other.organization_id != run.organization_id or other.domain.casefold() != run.domain.casefold()
                or not authoritative_run(other) or not _original_request_intact(other)):
            raise DispatchSettlementPending("withdrawal_family_unverified")
        charge = article_charge_receipt(other)
        if charge is not None and charge["ledger_id"] == ledger_id:
            initial = original_spend_for_run(other)
            # A research source holds a separately approved article payment;
            # its earlier topic work is not evidence that article work started.
            if (initial is not None and initial.pk != ledger_id
                    and (other.run_request or {}).get("roo_points_action") == "content_island_topic_generation"):
                continue
        if (_direct_delivery(other) or points_receipt(other).get("state") in {"captured", "kept"}
                or publication_state(other) != "none" or (other.result or {}).get(EVENT_RESULT_KEY, {}).get("events")
                or _closed(other) is None):
            raise DispatchSettlementPending("withdrawal_family_effects_unresolved")


def _settle(run, now):
    """Call existing effects only after current tenant and all original lineage locks."""
    from organizations.models import Organization
    from roo.models import Ledger
    from workflow_runs.models import ContentFactoryRun
    from .billing_lineage import ARTICLE_CHARGE_PREFIX, TOPIC_CHARGE_PREFIX, paid_article_request
    from .first_article_entitlement import entitled_request
    from .run_backend_facts import BackendFactConflict
    from .run_ownership import owns_backend_facts
    from .website_contract import evidence_digest
    from integrations.services.article_generation import (
        refund_content_factory_request_for_user, refund_content_factory_topic_generation_for_user,
    )
    with transaction.atomic():
        organization = Organization.objects.select_for_update().filter(pk=run.organization_id).first()
        fresh = ContentFactoryRun.objects.filter(pk=run.pk).first()
        if (organization is None or fresh is None or fresh.organization_id != organization.pk
                or not owns_backend_facts(fresh) or _closed(fresh) is None or not _original_request_intact(fresh)):
            raise DispatchSettlementPending("withdrawal_owner_changed")
        spend = _spend(fresh)
        saved = deepcopy(fresh.run_request or {})
        if spend is None and paid_article_request(saved):
            raise DispatchSettlementPending("withdrawal_original_charge_unverified")
        stamp = saved.get("roo_points_ledger_id") if entitled_request(saved) else getattr(spend, "pk", None)
        if stamp is None:
            # No obligation is not a fabricated successful refund.
            raise DispatchSettlementPending("withdrawal_original_obligation_missing")
        ids = list(ContentFactoryRun.objects.filter(_family_query(stamp)).order_by("pk")
                   .values_list("pk", flat=True)[:MAX_CHARGE_FAMILY + 1])
        if fresh.pk not in ids or len(ids) > MAX_CHARGE_FAMILY:
            raise DispatchSettlementPending("withdrawal_family_unbounded")
        family = list(ContentFactoryRun.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        run = next(other for other in family if other.pk == fresh.pk)
        if _closed(run) is None or current_execution_ticket(run) != current_execution_ticket(fresh):
            raise DispatchSettlementPending("withdrawal_owner_changed")
        meta = _meta(run)
        previous = meta.get("dispatch_settlement")
        if isinstance(previous, dict) and previous.get("state") in {"refunded", "released"}:
            return deepcopy(previous)
        _no_effect_family(run, family, ledger_id=stamp)
        immutable_fields = (
            "run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at",
            "status", "current_step", "organization_id", "domain", "approval_state")
        immutable = {other.pk: {name: deepcopy(getattr(other, name)) for name in immutable_fields} for other in family}
        # No provider call occurs in this transaction. The existing controller
        # alone owns amount/bucket restoration and original-key idempotency.
        if spend is not None:
            current = _spend(run)
            if current is None or current.pk != spend.pk or current.user_id is None:
                raise DispatchSettlementPending("withdrawal_original_charge_changed")
            refunds = list(Ledger.objects.filter(refund_of_id=spend.pk, kind="REFUND", user_id=spend.user_id,
                source=spend.source).order_by("pk")[:2])
            if len(refunds) > 1:
                raise DispatchSettlementPending("withdrawal_refund_history_ambiguous")
            if refunds:
                effect = refunds[0]
            else:
                topic = spend.idempotency_key.startswith(TOPIC_CHARGE_PREFIX)
                prefix = TOPIC_CHARGE_PREFIX if topic else ARTICLE_CHARGE_PREFIX
                original_request = {**saved, "client_request_id": spend.idempotency_key.removeprefix(prefix),
                    "roo_points_ledger_id": str(spend.pk), "roo_points_billing_user_id": spend.user_id}
                controller = refund_content_factory_topic_generation_for_user if topic else refund_content_factory_request_for_user
                with original_dispatch_refund_authority(run, closed_receipt=_closed(run), original_spend=spend):
                    effect = controller(user=spend.user, actor_id="system", article_request=original_request,
                        resolved_domain=run.domain, reason="Exact original dispatch durably withdrawn before enqueue")
            if (effect is None or effect.kind != "REFUND" or effect.refund_of_id != spend.pk
                    or effect.user_id != spend.user_id or effect.source != spend.source
                    or effect.delta_microroo != -spend.delta_microroo):
                raise DispatchSettlementPending("withdrawal_refund_unconfirmed")
            from .models import ContentFactoryJob
            from .run_backend_facts import update_job_facts
            update_job_facts(ContentFactoryJob.objects.filter(job_id__in=[other.legacy_job_id or other.run_id for other in family]),
                             billing_status="refunded", billing_ledger_id=effect.pk)
            state, effect_id = "refunded", effect.pk
        else:
            if not entitled_request(saved):
                raise DispatchSettlementPending("withdrawal_entitlement_unverified")
            with original_dispatch_refund_authority(run, closed_receipt=_closed(run), original_spend=None):
                controller = (refund_content_factory_topic_generation_for_user
                    if saved.get("roo_points_action") == "content_island_topic_generation"
                    else refund_content_factory_request_for_user)
                controller(user=None, actor_id="system", article_request=deepcopy(_free_original(run).run_request),
                    resolved_domain=run.domain, reason="Exact original dispatch durably withdrawn before enqueue")
            if _free_obligation_state(run) != "released":
                raise DispatchSettlementPending("withdrawal_release_unconfirmed")
            state, effect_id = "released", None
        # The existing controller may have projected billing facts. Preserve
        # those fresh fields and every original source/execution clock.
        run.refresh_from_db()
        current_family = ContentFactoryRun.objects.filter(pk__in=immutable).values("pk", *immutable_fields)
        if any(any(row[name] != value for name, value in immutable[row["pk"]].items()) for row in current_family):
            raise DispatchSettlementPending("withdrawal_controller_changed_original_execution")
        meta = _meta(run)
        receipt = {"schema_version": 1, "state": state, "at": now.isoformat(), "effect_id": effect_id,
            "original_ledger_id": getattr(spend, "pk", None), "withdrawal_digest": _closed(run)["receipt_sha256"],
            "execution_id": current_execution_ticket(run)["execution_id"],
            "admission_digest": evidence_digest(wire_identity(owner_receipt(run)))}
        if not owns_backend_facts(run):
            raise BackendFactConflict("The canonical settlement owner changed.")
        meta["dispatch_settlement"] = receipt
        _save_meta(run, meta)
        return receipt


def settle_authoritative_dispatch(run_id, *, now=None):
    """Withdraw one exact untouched dispatch, then reuse its original settlement.

    Unknown transport, old/foreign scope, live relatives and unconfirmed effects
    retain the obligation. Every remote request precedes the financial locks.
    """
    from workflow_runs.models import ContentFactoryRun
    if connection.in_atomic_block:
        raise DispatchSettlementPending("withdrawal_requires_unlocked_http")
    now = now or timezone.now()
    run = ContentFactoryRun.objects.filter(run_id=run_id).first()
    if run is None:
        return {"state": "pending", "reason": "withdrawal_run_missing"}
    claim = None
    try:
        run, claim = _claim(run, now)
        if claim is not None:
            run = _accept(run, claim, _withdraw(run))
        return _settle(run, now)
    except (DispatchSettlementPending, RunOwnershipConflict, requests.RequestException, ValueError, TypeError, KeyError) as exc:
        if claim is not None:
            try:
                _failed_claim(run, claim)
            except (DispatchSettlementPending, RunOwnershipConflict, ValueError, TypeError, KeyError):
                pass
        return {"state": "pending", "reason": str(exc) if isinstance(exc, DispatchSettlementPending) else "withdrawal_unresolved"}
