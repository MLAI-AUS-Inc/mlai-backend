"""Apply one app/workspace/method budget to public and owner-token Slack calls."""
import time
import hashlib
from collections.abc import Mapping
from django.conf import settings
from slack_sdk.errors import SlackApiError

from .budgets import admit_request, record_cooldown
from .scheduler import BudgetDeferred
from .request_priority import current_priority
from . import telemetry

POSTING_METHODS = frozenset({
    "chat.postMessage", "chat.update", "chat.delete", "reactions.add", "reactions.remove",
})


def channel_budget_method(method, kwargs):
    """Resolve the SDK's channel argument without using message text or tokens."""
    if method not in POSTING_METHODS:
        return None
    for key in ("json", "data", "params"):
        arguments = kwargs.get(key)
        channel = arguments.get("channel") if isinstance(arguments, Mapping) else None
        if isinstance(channel, str) and channel.strip():
            channel = channel.strip()
            key = f"{method}:{channel}"
            # The persisted method column is bounded to 100 characters, while
            # source IDs can independently be 100. Preserve separate gates.
            return key if len(key) <= 100 else f"{method}:sha256:{hashlib.sha256(channel.encode()).hexdigest()}"
    return None


def provider_interval(method):
    if method.partition(":")[0] in POSTING_METHODS and ":" in method:
        return 1.0
    if method in {"conversations.history", "conversations.replies"}:
        # Restricted distribution is the safe default; the release manifest
        # must verify internal/Marketplace status before raising this budget.
        return 1.2 if getattr(settings, "MESSAGE_SYNC_SLACK_DISTRIBUTION", "restricted") in {"internal", "marketplace"} else 60.0
    # Both directory and metadata methods are documented Tier 3 (50+/min).
    if method in {"conversations.info", "users.conversations", "conversations.mark"}:
        return 1.2
    if method in {"conversations.members", "users.info", "files.info"}:
        return 0.6  # Tier 4 (100+/min), still shared across every owner/device.
    if method == "apps.event.authorizations.list":
        return 0.1
    return 3.0  # conservative tier-2 baseline; Retry-After always wins


def budgeted_client(client, *, workspace_id, app_id=None, before_request=None):
    """Wrap one concrete SDK instance; an optional fence is server-owned.

    A member page supplies its current lease check for this newly constructed
    instance/attempt only. It runs after quota admission, directly before SDK
    I/O. No callback or authority is stored in provider kwargs or global state.
    """
    if before_request is not None and not callable(before_request):
        raise TypeError("invalid_slack_request_fence")
    if not getattr(settings, "MESSAGE_SYNC_ENABLED", False):
        if before_request is not None:
            raise ValueError("fenced_slack_request_requires_durable_sync")
        return client
    app_id = app_id or str(getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "") or "")
    if not app_id or not workspace_id:
        raise ValueError("message_sync_slack_scope_not_configured")
    original = client.api_call

    def api_call(api_method, **kwargs):
        scope = dict(app_id=app_id, workspace_id=workspace_id, method=api_method)
        channel_method = channel_budget_method(api_method, kwargs)
        channel_scope = {**scope, "method": channel_method} if channel_method else None
        metric_scope = telemetry.scope_key(app_id, workspace_id, api_method) if api_method in telemetry.METHODS else None
        def record(counter, amount=1):
            if metric_scope:
                telemetry.record(metric_scope, counter, amount)
        try:
            admit_request(**scope, interval_seconds=provider_interval(api_method), priority=current_priority())
            if channel_scope:
                # Retain the existing method-wide floor and add the channel
                # allowance; token rotation cannot multiply either budget.
                admit_request(**channel_scope, interval_seconds=1.0, priority=current_priority())
        except BudgetDeferred:
            record("deferred")
            raise
        record("admitted")
        started = time.monotonic()
        try:
            if before_request is not None:
                before_request()
            return original(api_method, **kwargs)
        except SlackApiError as exc:
            headers = getattr(exc.response, "headers", {}) or {}
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if getattr(exc.response, "status_code", None) == 429 or exc.response.get("error") == "ratelimited":
                record("rate_limited")
                try:
                    seconds = max(1, int(raw or 60))
                except (TypeError, ValueError):
                    seconds = 60
                record_cooldown(**scope, retry_after=seconds)
                if channel_scope:
                    record_cooldown(**channel_scope, retry_after=seconds)
                raise BudgetDeferred(seconds) from exc
            record("failed")
            raise
        except Exception:
            record("failed")
            raise
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            record("finished")
            record("request_ms", elapsed_ms)

    client.api_call = api_call
    # Retries run through the durable worker, not SDK sleeps under consent locks.
    client.retry_handlers = []
    return client
