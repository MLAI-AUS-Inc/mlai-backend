"""Apply one app/workspace/method budget to public and owner-token Slack calls."""
import time
from django.conf import settings
from slack_sdk.errors import SlackApiError

from .budgets import admit_request, record_cooldown
from .scheduler import BudgetDeferred
from . import telemetry


def provider_interval(method):
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


def budgeted_client(client, *, workspace_id, app_id=None):
    """Wrap a concrete SDK instance while preserving existing mocked clients."""
    if not getattr(settings, "MESSAGE_SYNC_ENABLED", False):
        return client
    app_id = app_id or str(getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "") or "")
    if not app_id or not workspace_id:
        raise ValueError("message_sync_slack_scope_not_configured")
    original = client.api_call

    def api_call(api_method, **kwargs):
        scope = dict(app_id=app_id, workspace_id=workspace_id, method=api_method)
        metric_scope = telemetry.scope_key(app_id, workspace_id, api_method) if api_method in telemetry.METHODS else None
        def record(counter, amount=1):
            if metric_scope:
                telemetry.record(metric_scope, counter, amount)
        try:
            admit_request(**scope, interval_seconds=provider_interval(api_method))
        except BudgetDeferred:
            record("deferred")
            raise
        record("admitted")
        started = time.monotonic()
        try:
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
                raise BudgetDeferred(seconds) from exc
            record("failed")
            raise
        except Exception:
            record("failed")
            raise
        finally:
            record("finished")
            record("request_ms", round((time.monotonic() - started) * 1000))

    client.api_call = api_call
    # Retries run through the durable worker, not SDK sleeps under consent locks.
    client.retry_handlers = []
    return client
