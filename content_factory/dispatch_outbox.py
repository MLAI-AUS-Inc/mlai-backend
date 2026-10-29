"""Retry charged content starts with the same request identity, then refund."""
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .dispatch_models import ContentFactoryDispatchOutbox
from .website_contract import WebsiteAuthorityError, evidence_digest, sanitized_evidence
from .section_issues import _safe_text

MAX_ATTEMPTS = 5


def retry_delay(attempt):
    """Bound retries to the one, two, five and ten minute delivery ladder."""
    return (60, 120, 300, 600)[min(max(1, attempt) - 1, 3)]


def dispatch_outcome(status_code, body, *, known_key_outcome="unknown"):
    """Distinguish explicit refusal from an ambiguous HTTP delivery outcome."""
    if status_code in {200, 201, 202} and isinstance(body, dict) and (body.get("run_id") or body.get("runId")):
        return "delivered", False
    if status_code in {400, 401, 403, 409, 410, 422} and (
            known_key_outcome in {"not_received", "received_not_started"} or isinstance(body, dict) and body.get("code") == "dispatch_rejected"):
        return "failed", False
    return "pending", True


def exhausted_dispatch_outcome(key_outcome):
    """An exhausted POST budget does not prove that the worker owns no run."""
    return {"started": "delivered", "not_received": "failed", "received_not_started": "failed", "rejected": "failed"}.get(key_outcome, "awaiting_resolution")


def dispatch_failure_details(outcome, payload, *, refund=True):
    """Name the proven no-work outcome and the caller's actual billing action."""
    ending = "Any Roo points charged are being refunded — try again when ready." if refund else "Try again when ready."
    if outcome == "not_received":
        return "dispatch_child_missing", (
            "Content Factory did not receive this request (confirmed by dispatch-key lookup). "
            + ending)
    if outcome == "received_not_started":
        reason = _safe_text(payload.get("error"), 240) or "work could not start"
        return "received_not_started", (
            f"Content Factory received this request but couldn't start it ({reason}). "
            + ending)
    if outcome == "rejected":
        return "dispatch_rejected", (
            "The article start was rejected before entering the queue. "
            + ending)
    raise ValueError("A dispatch failure requires a confirmed no-work outcome.")


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
    changed = ContentFactoryDispatchOutbox.objects.filter(client_request_id=client_request_id,
        state__in=["pending", "delivering"]).update(state="delivered", run_id=run_id, last_error="", updated_at=timezone.now())
    if changed:
        transaction.on_commit(lambda: wake_dispatch_settlement_by_key(client_request_id), robust=True)
    return changed


def wake_dispatch_settlement_by_key(client_request_id):
    """Resolve the committed synchronous dispatch before waking its parent."""
    from .website_first_article_refunds import wake_dispatch_settlement
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=client_request_id).first()
    if row and row.payload.get("request", {}).get("first_article_goal_operation_id"):
        wake_dispatch_settlement(row.pk)


def _research_dispatch(row):
    """Identify the supported discovery contract without an article authority bypass."""
    return row.payload.get("endpoint") == "discovery" and row.payload.get("workflow") == "auto_discovery"


def refund_dispatch(row, *, reason):
    """The original spend identity makes retries of refund safe."""
    request = row.payload.get("request", {})
    if request.get("first_article_goal_operation_id"):
        # The parent allocation can fund more than one repair child. Even a
        # proven rejected dispatch cannot credit it around another child's
        # publication or delivery obligation. Its terminal outbox owns credit.
        from .website_first_article_billing import defer_goal_settlement
        defer_goal_settlement(request, resolved_domain=row.organization.domain)
        return
    from django.contrib.auth import get_user_model
    from integrations.services.article_generation import (
        refund_content_factory_request_for_user, refund_content_factory_topic_generation_for_user,
    )
    from .first_article_entitlement import release_entitlement_for_request, release_topic_batch
    # A free first article or topic batch has cost 0 and no payer to credit.
    # A proven no-work dispatch gives the free attempt back instead.
    if _research_dispatch(row):
        release_topic_batch(client_request_id=row.client_request_id, organization=row.organization_id)
    else:
        release_entitlement_for_request(organization=row.organization_id, request=request, reason=reason)
    payer_id = row.payload.get("billing_user_id")
    if not payer_id or not row.payload.get("request", {}).get("roo_points_cost"):
        return
    payer = get_user_model().objects.get(pk=payer_id)
    refund = refund_content_factory_topic_generation_for_user if _research_dispatch(row) else refund_content_factory_request_for_user
    refund(user=payer, actor_id=row.payload.get("actor_id") or "",
        article_request=row.payload["request"], resolved_domain=row.organization.domain, reason=reason)


def _deliver(row):
    from integrations import http_client
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers, _lookup_content_factory_dispatch_by_key, _refresh_article_editorial_payload
    from .website_connections import authority_guard, require_unlocked_remote_call
    from .portable_drafts import original_portable_run
    from .website_operations import OPERATION_FIELDS
    request = dict(row.payload["request"])
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        return "pending", "dispatch_worker_unavailable", ""
    # The previous POST may already own a run. Identity lookup precedes both
    # new policy checks and re-dispatch so no second child is created.
    known, existing = _lookup_content_factory_dispatch_by_key(remote, row.client_request_id)
    if known == "started":
        return _accept_delivery(row, request, existing, recovered=True)
    if known == "rejected":
        return "failed", "dispatch_rejected", ""
    portable = (original_portable_run(SimpleNamespace(run_request=row.payload["request"]))
        and not any(key in request for key in OPERATION_FIELDS))
    from django.contrib.auth import get_user_model
    if _research_dispatch(row):
        # Discovery needs only the current owning founder/domain, plus the
        # selected approved audience when one was requested. It never inherits
        # repository permissions or grants editorial/publication approval.
        from founder_tools.models import VibeRaisingCompany
        from .editorial_catalog import discovery_audience_context
        from .vibe_marketing_views import _get_config
        requester = get_user_model().objects.get(pk=request["roo_points_requested_by_user_id"], is_active=True)
        if request.get("domain") != row.organization.domain or not VibeRaisingCompany.objects.filter(
                pk=request.get("mcp_company_id"), organization=row.organization,
                profile__user=requester, profile__role="founder").exists():
            raise WebsiteAuthorityError("research_owner_changed", "The original research startup is no longer available.")
        try:
            audience = discovery_audience_context(_get_config(row.organization).pillar_strategy, request)
        except ValueError as exc:
            raise WebsiteAuthorityError("research_audience_changed", "The approved research audience changed.") from exc
        if audience != request.get("research_audience"):
            raise WebsiteAuthorityError("research_audience_changed", "The approved research audience changed.")
    else:
        if not portable:
            with authority_guard(request, action="read"):
                pass
        policy_error = _refresh_article_editorial_payload(organization=row.organization, payload=request)
        if policy_error is not None:
            return "pending", str(policy_error.data.get("code") or "editorial_policy_recheck_required")[:100], ""
        from integrations.services.article_generation import require_article_activation
        requester = get_user_model().objects.get(pk=request["roo_points_requested_by_user_id"], is_active=True)
        require_article_activation(domain=row.organization.domain, actor_id=row.payload.get("actor_id") or "",
            user=requester, expected_repo=request.get("github_repo", ""), article_request=request)
        if not portable:
            from .vibe_marketing_views import _validate_article_start_reservation
            # Provider admission examines today's configuration outside SQL.
            # Retry only the original target/source, with current stored proof;
            # do not upgrade a historical paid request to today's selection.
            with transaction.atomic():
                _validate_article_start_reservation(SimpleNamespace(organization=row.organization),
                    request, require_website=True)
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
    return state, {"not_received": "dispatch_never_received", "received_not_started": "received_not_started",
        "rejected": "dispatch_rejected"}.get(known, "dispatch_outcome_unconfirmed"), ""


def process_dispatch_outbox(*, limit=20, now=None):
    """Deliver due entries without holding billing or authority locks across HTTP."""
    now = now or timezone.now()
    due = ContentFactoryDispatchOutbox.objects.filter(state__in=["pending", "delivering", "refund_pending", "awaiting_resolution"],
        next_attempt_at__lte=now).order_by("next_attempt_at")
    result = {"status": "completed", "processed": 0, "delivered": 0, "pending": 0, "failed": 0}
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
        if applied and state in {"delivered", "failed"} and row.payload.get("request", {}).get("first_article_goal_operation_id"):
            from .website_first_article_refunds import wake_dispatch_settlement
            transaction.on_commit(lambda identifier=row.pk: wake_dispatch_settlement(identifier), robust=True)
        result[state if applied and state in result else "pending"] += 1
    return result
