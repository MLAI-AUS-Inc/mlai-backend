"""Retry charged article starts with the same request identity, then refund."""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .dispatch_models import ContentFactoryDispatchOutbox
from .website_contract import WebsiteAuthorityError, evidence_digest, sanitized_evidence

MAX_ATTEMPTS = 5


def retry_delay(attempt):
    """Bound retries to the one, two, five and ten minute delivery ladder."""
    return (60, 120, 300, 600)[min(max(1, attempt) - 1, 3)]


def dispatch_outcome(status_code, body, *, known_key_outcome="unknown"):
    """Distinguish explicit refusal from an ambiguous HTTP delivery outcome."""
    if status_code in {200, 201, 202} and isinstance(body, dict) and (body.get("run_id") or body.get("runId")):
        return "delivered", False
    if status_code in {400, 401, 403, 409, 410, 422} and (
            known_key_outcome == "absent" or isinstance(body, dict) and body.get("code") == "dispatch_rejected"):
        return "failed", False
    return "pending", True


def exhausted_dispatch_outcome(key_outcome):
    """An exhausted POST budget does not prove that the worker owns no run."""
    return {"dispatched": "delivered", "absent": "failed", "rejected": "failed"}.get(key_outcome, "awaiting_resolution")


def reserve_dispatch(*, organization, payload, endpoint="article", workflow="article_generation", billing_user_id=None, actor_id=""):
    """Write this row inside the transaction that creates its Roo spend."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Article dispatch and its Roo charge must share a transaction.")
    key = str(payload.get("client_request_id") or "")
    if not key or len(key) > 255:
        raise ValueError("A bounded article request identity is required.")
    saved = sanitized_evidence(payload)
    identity = {key: value for key, value in saved.items() if not key.startswith("roo_points_") and key not in {"dispatch_outbox_reserved", "roo_points_gate"}}
    envelope = {"request": saved, "endpoint": endpoint, "workflow": workflow,
        "billing_user_id": str(billing_user_id or ""), "actor_id": actor_id,
        "request_digest": evidence_digest(identity)}
    row, created = ContentFactoryDispatchOutbox.objects.get_or_create(client_request_id=key,
        defaults={"organization": organization, "payload": envelope})
    if row.organization_id != organization.pk or row.payload.get("request_digest") != envelope["request_digest"]:
        raise WebsiteAuthorityError("dispatch_key_conflict", "That article request identity has different inputs.")
    if not created and row.state in {"failed", "cancelled"}:
        raise WebsiteAuthorityError("dispatch_already_refunded", "That article start was refunded. Start a fresh article request.")
    return row


def mark_dispatch_delivered(client_request_id, run_id):
    """Record a synchronous delivery without creating a duplicate scheduler run."""
    return ContentFactoryDispatchOutbox.objects.filter(client_request_id=client_request_id,
        state__in=["pending", "delivering"]).update(state="delivered", run_id=run_id, last_error="", updated_at=timezone.now())


def refund_dispatch(row, *, reason):
    """The original spend identity makes retries of refund safe."""
    from django.contrib.auth import get_user_model
    from integrations.services.article_generation import refund_content_factory_request_for_user
    payer_id = row.payload.get("billing_user_id")
    if not payer_id or not row.payload.get("request", {}).get("roo_points_cost"):
        return
    payer = get_user_model().objects.get(pk=payer_id)
    refund_content_factory_request_for_user(user=payer, actor_id=row.payload.get("actor_id") or "",
        article_request=row.payload["request"], resolved_domain=row.organization.domain, reason=reason)


def _deliver(row):
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers, _lookup_content_factory_dispatch_by_key, _refresh_article_editorial_payload
    from .website_connections import authority_guard, require_unlocked_remote_call
    from .portable_drafts import explicit_portable_request
    request = dict(row.payload["request"])
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        return "pending", "dispatch_worker_unavailable", ""
    # The previous POST may already own a run. Identity lookup precedes both
    # new policy checks and re-dispatch so no second child is created.
    known, existing = _lookup_content_factory_dispatch_by_key(remote, row.client_request_id)
    if known == "dispatched":
        return _accept_delivery(row, request, existing, recovered=True)
    if known == "rejected":
        return "failed", "dispatch_rejected", ""
    if not explicit_portable_request(request):
        with authority_guard(request, action="read"):
            pass
    policy_error = _refresh_article_editorial_payload(organization=row.organization, payload=request)
    if policy_error is not None:
        return "pending", str(policy_error.data.get("code") or "editorial_policy_recheck_required")[:100], ""
    from django.contrib.auth import get_user_model
    from integrations.services.article_generation import require_article_activation
    requester = get_user_model().objects.get(pk=request["roo_points_requested_by_user_id"], is_active=True)
    require_article_activation(domain=row.organization.domain, actor_id=row.payload.get("actor_id") or "",
        user=requester, expected_repo=request.get("github_repo", ""), article_request=request)
    require_unlocked_remote_call()
    response = http_client.post(f"{remote['base_url']}/api/runs/{row.payload['endpoint']}",
        headers=_content_factory_headers(), json=request, timeout=(3, 30))
    try:
        body = response.json()
    except (ValueError, AttributeError):
        body = {}
    state, _ = dispatch_outcome(response.status_code, body, known_key_outcome=known)
    run_id = str(body.get("run_id") or body.get("runId") or "") if isinstance(body, dict) else ""
    if state == "delivered":
        return _accept_delivery(row, request, body)
    code = str(body.get("code") or "dispatch_unconfirmed")[:100] if isinstance(body, dict) else "dispatch_unconfirmed"
    return state, code, run_id


def _accept_delivery(row, request, body, *, recovered=False):
    from .vibe_marketing_views import _create_local_run
    from .dispatch_binding import bind_dispatch_token_run
    run_id = str(body.get("run_id") or body.get("runId") or "")
    bind_dispatch_token_run(client_request_id=row.client_request_id, remote_run_id=run_id)
    run = _create_local_run(workflow=row.payload["workflow"], domain=row.organization.domain,
        github_repo=request.get("github_repo", ""), actor_id=row.payload.get("actor_id") or "",
        payload=request, remote_data={**body, "dispatch_recovered_by_key": recovered})
    if request.get("operation_id"):
        from .website_models import WebsiteConnectionOperation
        from .website_operations import bind_operation_run
        operation = WebsiteConnectionOperation.objects.filter(pk=request["operation_id"],
            connection__organization_id=row.organization_id).first()
        if operation:
            bind_operation_run(operation, run)
    if run.status == "cancelled":
        return "failed", "website_connection_changed", run_id
    return "delivered", "", run_id


def _resolve_exhausted_dispatch(row):
    """Keep read-only reconciliation alive after the bounded POST budget ends."""
    from .vibe_marketing_views import _content_factory_remote_config, _lookup_content_factory_dispatch_by_key
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        return "awaiting_resolution", "dispatch_outcome_unconfirmed", ""
    known, body = _lookup_content_factory_dispatch_by_key(remote, row.client_request_id)
    state = exhausted_dispatch_outcome(known)
    if state == "delivered":
        return _accept_delivery(row, dict(row.payload["request"]), body, recovered=True)
    return state, "dispatch_never_received" if known == "absent" else (
        "dispatch_rejected" if known == "rejected" else "dispatch_outcome_unconfirmed"), ""


def process_dispatch_outbox(*, limit=20, now=None):
    """Deliver due entries without holding billing or authority locks across HTTP."""
    now = now or timezone.now()
    due = ContentFactoryDispatchOutbox.objects.filter(state__in=["pending", "delivering", "refund_pending", "awaiting_resolution"],
        next_attempt_at__lte=now).order_by("next_attempt_at")
    result = {"processed": 0, "delivered": 0, "pending": 0, "failed": 0}
    for identifier in list(due.values_list("pk", flat=True)[:max(1, min(limit, 100))]):
        with transaction.atomic():
            row = ContentFactoryDispatchOutbox.objects.select_for_update().select_related("organization").get(pk=identifier)
            if row.state not in {"pending", "delivering", "refund_pending", "awaiting_resolution"} or row.next_attempt_at > now:
                continue
            refund_only = row.state == "refund_pending"
            resolution_only = row.state == "awaiting_resolution"
            row.state = "delivering" if not refund_only else "refund_pending"
            if not refund_only and not resolution_only:
                row.attempts += 1
            row.next_attempt_at = now + timedelta(minutes=5)
            row.save(update_fields=["state", "attempts", "next_attempt_at", "updated_at"])
            claimed_at = row.updated_at
        result["processed"] += 1
        state, code, run_id = "failed" if refund_only else "pending", row.last_error, ""
        if not refund_only:
            try:
                state, code, run_id = _resolve_exhausted_dispatch(row) if resolution_only else _deliver(row)
            except WebsiteAuthorityError as exc:
                # Consent can disappear after an earlier ambiguous POST. Only
                # key evidence may release that charge; authority denial alone
                # cannot establish that no worker accepted it.
                state, code = "pending", exc.code
                try:
                    observed_state, observed_code, run_id = _resolve_exhausted_dispatch(row)
                    if observed_state != "awaiting_resolution":
                        state, code = observed_state, observed_code
                except Exception:
                    pass
            except Exception:
                state, code = "pending", "dispatch_transport_unavailable"
            if state == "pending" and row.attempts >= MAX_ATTEMPTS:
                try:
                    state, code, run_id = _resolve_exhausted_dispatch(row)
                except Exception:
                    state, code = "awaiting_resolution", "dispatch_outcome_unconfirmed"
        if state == "failed":
            try:
                refund_dispatch(row, reason=code)
            except Exception:
                state = "refund_pending"
        applied = ContentFactoryDispatchOutbox.objects.filter(pk=row.pk, updated_at=claimed_at,
            state__in=["delivering", "refund_pending"]).update(state=state, run_id=run_id or row.run_id,
                last_error=code, next_attempt_at=now + timedelta(seconds=900 if state == "awaiting_resolution" else retry_delay(row.attempts)), updated_at=timezone.now())
        result[state if applied and state in result else "pending"] += 1
    return result
