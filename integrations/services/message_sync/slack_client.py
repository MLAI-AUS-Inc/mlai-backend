"""Apply one app/workspace/method budget to public and owner-token Slack calls."""
from django.conf import settings
from slack_sdk.errors import SlackApiError

from .budgets import admit_request, record_cooldown
from .scheduler import BudgetDeferred


def provider_interval(method):
    if method in {"conversations.history", "conversations.replies"}:
        # Restricted distribution is the safe default; the release manifest
        # must verify internal/Marketplace status before raising this budget.
        return 1.2 if getattr(settings, "MESSAGE_SYNC_SLACK_DISTRIBUTION", "restricted") in {"internal", "marketplace"} else 60.0
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
        admit_request(**scope, interval_seconds=provider_interval(api_method))
        try:
            return original(api_method, **kwargs)
        except SlackApiError as exc:
            headers = getattr(exc.response, "headers", {}) or {}
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if getattr(exc.response, "status_code", None) == 429 or exc.response.get("error") == "ratelimited":
                try:
                    seconds = max(1, int(raw or 60))
                except (TypeError, ValueError):
                    seconds = 60
                record_cooldown(**scope, retry_after=seconds)
                raise BudgetDeferred(seconds) from exc
            raise

    client.api_call = api_call
    # Retries run through the durable worker, not SDK sleeps under consent locks.
    client.retry_handlers = []
    return client
