"""Stop original native generation before crediting an unused first-article hold."""

from copy import deepcopy
import re

from django.utils import timezone

from . import cms_goal_authority as authority
from . import cms_goal_billing as billing
from . import cms_goal_settlement as settlements
from .channel_artifact_contract import canonical_bytes, digest, validate_envelope
from .cms_goal_contract import _time
from .website_contract import WebsiteAuthorityError

REFERENCE = "cms_goal_work_stopped"
STATUSES = {"not_received", "received_not_started", "rejected", "cancelled"}
FIELDS = {"version", "goal_operation_id", "billing_request_id", "request_sha256", "run_id", "status", "checked_at", "digest"}


def _pending():
    return WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting for the original native article work to stop before crediting it.", retryable=True)


def _invalid():
    return WebsiteAuthorityError("website_operation_changed", "The original native work-stop observation changed.")


def original(operation):
    """Resolve financial identity after provider revocation, without provider authority."""
    from .dispatch_models import ContentFactoryDispatchOutbox
    value = authority.historical(operation)
    held = billing.historical_reservation(operation)
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=held["client_request_id"],
        organization_id=value.scope["binding"]["organization_id"]).first()
    if row is not None:
        source = settlements._source(row)
        envelope = validate_envelope(row.payload["request"]["publishing_channel_artifact"])
        binding = value.scope["binding"]
        if (source["goal_operation_id"] != str(operation.pk) or source["payer_user_id"] != held["payer_user_id"] or
                source["spend_ledger_id"] != held["spend_ledger_id"] or source["cost_points"] != held["cost_points"] or
                envelope["scope_sha256"] != digest(binding) or envelope["connection_id"] != binding["connection_id"] or
                envelope["connection_generation"] != binding["generation"] or envelope["company_id"] != binding["company_id"] or
                envelope["owner_user_id"] != binding["owner_user_id"]):
            raise WebsiteAuthorityError("website_operation_changed", "The original native cancellation identity changed.")
    return row


def _receipt(row, *, run_id, status, at):
    if (status not in STATUSES or run_id is not None and (not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) is None) or
            (status == "cancelled") != (run_id is not None)):
        raise _pending()
    source = settlements._source(row)
    value = {"version": 1, "goal_operation_id": source["goal_operation_id"], "billing_request_id": row.client_request_id,
        "request_sha256": digest(row.payload["request"]), "run_id": run_id, "status": status, "checked_at": _time(at.isoformat()).isoformat()}
    return {**value, "digest": digest(value)}


def validate(row, receipt):
    """Require a private original stop observation; local cancelled status is insufficient."""
    try:
        if not isinstance(receipt, dict) or set(receipt) != FIELDS:
            raise _invalid()
        expected = _receipt(row, run_id=receipt["run_id"], status=receipt["status"], at=_time(receipt["checked_at"]))
        if canonical_bytes(receipt) != canonical_bytes(expected):
            raise _invalid()
        return deepcopy(expected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _invalid() from None


def stop_unused(operation, *, now=None):
    """Resolve the original key and obtain explicit CF cancellation outside locks.

    The financial worker owns the durable lease and persists this observation
    only after rechecking the inactive original goal and immutable dispatch.
    Never call for an accepted draft: cancellation clears CF artifacts which
    may still be needed for GET-only provider reconciliation.
    """
    if operation.state not in billing.records.REFUND_TERMINAL:
        raise _pending()
    if billing.retention_subject(operation) is not None:
        # A handoff may have arrived after the coordinator claimed its lease.
        # Let the next financial observation retain it; never clear its source.
        raise _pending()
    row = original(operation)
    if row is None:
        return None
    if REFERENCE in row.payload:
        return validate(row, row.payload[REFERENCE])
    from .vibe_marketing_views import _content_factory_remote_config, _lookup_content_factory_dispatch_by_key, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    from integrations import http_client
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        raise _pending()
    require_unlocked_remote_call()
    outcome, response = _lookup_content_factory_dispatch_by_key(remote, row.client_request_id)
    if outcome in {"not_received", "received_not_started", "rejected"}:
        return _receipt(row, run_id=None, status=outcome, at=now or timezone.now())
    run_id = response.get("run_id") if isinstance(response, dict) else None
    if outcome != "started" or not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) is None:
        raise _pending()
    # Repeated cancellation is idempotent; an absent or generic success reply
    # never proves an existing worker stopped or grants an unused-work credit.
    require_unlocked_remote_call()
    response = http_client.post(f"{remote['base_url']}/api/runs/{run_id}/cancel", headers=_content_factory_headers(),
        json={"reason": "native_articles_goal_ended"}, timeout=(3, 15))
    try:
        body = response.json()
    except (ValueError, AttributeError):
        raise _pending() from None
    if (response.status_code != 200 or not isinstance(body, dict) or body.get("run_id") != run_id or
            body.get("status") != "cancelled" or body.get("cleanup_pending") is True or body.get("cleanupPending") is True):
        raise _pending()
    return _receipt(row, run_id=run_id, status="cancelled", at=now or timezone.now())
