"""Bounded public-channel reads under the owner's current Slack authority.

No bot credential, mirror cache, private message, arbitrary method, or caller
supplied URL is accepted. Native relay resources remain explicitly unsupported.
"""

from contextlib import contextmanager
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError, APIException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


RESOURCE_RE = re.compile(r"^slack:(T[A-Z0-9]+):(C[A-Z0-9]+)$")
TIMESTAMP_RE = re.compile(r"^[0-9]{1,20}\.[0-9]{1,9}$")


class SourceUnavailable(APIException):
    status_code = 503
    default_detail = "The message source is unavailable."
    default_code = "source_unavailable"


def parse_resource(resource):
    if str(resource).startswith("community:"):
        raise ValidationError({"resource": "Native community relay access is not enabled."})
    match = RESOURCE_RE.fullmatch(str(resource))
    if not match:
        raise ValidationError({"resource": "Choose an exact public Slack channel resource."})
    return match.groups()


def public_member_channel(channel):
    return bool(channel and channel.get("is_member") is True
                and channel.get("is_channel") is True
                and not any(channel.get(key) for key in
                            ("is_private", "is_im", "is_mpim", "is_ext_shared", "is_archived")))


@contextmanager
def slack_authority(owner, resource, *, write=False, expected_slack_user=None):
    """Hold the existing user OAuth fence through each bounded provider read."""
    from integrations.models import ExternalServiceConnection, ExternalServiceProvider, ExternalServiceConnectionStatus
    from integrations.services.slack_oauth_authority import current_slack_oauth_generation, connection_slack_oauth_generation
    workspace, channel_id = parse_resource(resource)
    if not getattr(settings, "AGENT_RUNTIME_SLACK_READ_ENABLED", False):
        raise PermissionDenied("Slack agent reads are disabled.")
    if write and not getattr(settings, "AGENT_RUNTIME_ACTIONS_ENABLED", False):
        raise PermissionDenied("Agent writes are disabled.")
    allowed = set(getattr(settings, "AGENT_RUNTIME_SLACK_WORKSPACES", []))
    if workspace not in allowed:
        raise PermissionDenied("Workspace is outside the agent pilot.")
    if channel_id not in set(getattr(settings, "AGENT_RUNTIME_SLACK_CHANNEL_IDS", [])):
        raise PermissionDenied("Channel is outside the agent pilot.")
    with transaction.atomic():
        current_owner = get_user_model().objects.select_for_update().get(pk=owner.pk)
        if not current_owner.is_active:
            raise PermissionDenied("Account is no longer active.")
        # This is the same user-first lock boundary as OAuth disconnect. Require
        # personal connections; an organisation credential is not member consent.
        connection = ExternalServiceConnection.objects.select_for_update().filter(
            user_id=owner.pk, organization__isnull=True,
            provider=ExternalServiceProvider.SLACK, external_account_id=workspace,
            status__in=(ExternalServiceConnectionStatus.CONNECTED, ExternalServiceConnectionStatus.SYNCING),
        ).order_by("-updated_at", "-id").first()
        if connection is None:
            raise PermissionDenied("Connect your personal Slack account first.")
        metadata = connection.provider_metadata or {}
        slack_user = (metadata.get("authed_user") or {}).get("id")
        team = (metadata.get("team") or {}).get("id") or connection.external_account_id
        required_scopes = {"channels:read", "channels:history"} | ({"chat:write"} if write else set())
        if (not slack_user or (expected_slack_user is not None and slack_user != expected_slack_user)
                or team != workspace or not connection.access_token
                or connection_slack_oauth_generation(connection) != current_slack_oauth_generation(owner.pk)
                or not required_scopes.issubset(set(connection.scopes or []))
                or (connection.token_expires_at and connection.token_expires_at <= timezone.now())):
            raise PermissionDenied("Re-authorize the current Slack account with channel read access.")
        # Retrying a failed write after an ambiguous network acknowledgement can
        # duplicate it. The durable outbox owns retries/reconciliation.
        client = WebClient(token=connection.access_token, timeout=5, retry_handlers=[])
        try:
            identity = _call(client, workspace, "auth_test")
            if identity.get("team_id") != workspace or identity.get("user_id") != slack_user or identity.get("bot_id"):
                raise PermissionDenied("Slack identity does not match the member's user credential.")
            info = _call(client, workspace, "conversations_info", channel=channel_id)
            channel = info.get("channel", {})
            if channel.get("id") != channel_id or not public_member_channel(channel):
                raise PermissionDenied("This public channel is not currently available to the member.")
            yield client, workspace, channel_id, channel
        except SlackApiError as exc:
            raise SourceUnavailable() from exc


def _call(client, workspace, method, **kwargs):
    from rest_framework.exceptions import Throttled
    key = f"agent-runtime:slack-rate:{workspace}:{method}"
    until = cache.get(key)
    if until and float(until) > timezone.now().timestamp():
        raise Throttled(wait=max(1, int(float(until) - timezone.now().timestamp())))
    try:
        result = getattr(client, method)(**kwargs)
        if result.get("ok") is not True:
            raise SourceUnavailable()
        return result
    except SlackApiError as exc:
        if exc.response.status_code == 429:
            try:
                retry = max(1, min(3600, int(exc.response.headers.get("Retry-After", 60))))
            except (ValueError, TypeError):
                retry = 60
            cache.set(key, timezone.now().timestamp() + retry, timeout=retry)
            raise Throttled(wait=retry) from exc
        raise


def live_access(owner, scope, resource):
    if scope not in {"messages:read", "messages:write", "events:read", "actions:propose"}:
        return False
    try:
        with slack_authority(owner, resource, write=scope == "messages:write"):
            return True
    except (PermissionDenied, ValidationError):
        return False


def channel_summary(owner, resource):
    with slack_authority(owner, resource) as (_, _, channel_id, channel):
        return {"resource": resource, "id": channel_id, "name": str(channel.get("name", ""))[:255]}


def safe_message(message):
    # Provider text is untrusted content. Attachments, files, and hidden message
    # metadata are deliberately not copied into the agent's context.
    return {"id": str(message.get("ts", "")), "thread_id": str(message.get("thread_ts") or message.get("ts", "")),
            "author_id": str(message.get("user") or message.get("bot_id") or ""),
            "text": str(message.get("text", ""))[:20000], "trust": "untrusted_source_content"}


def read_messages(owner, resource, *, thread_id=None, query=None, cursor="", limit=50):
    limit = min(100, max(1, int(limit)))
    if len(cursor) > 2048 or (query is not None and (not query.strip() or len(query) > 200)):
        raise ValidationError("Invalid source cursor or search query.")
    if thread_id is not None and not TIMESTAMP_RE.fullmatch(str(thread_id)):
        raise ValidationError("Invalid Slack thread timestamp.")
    with slack_authority(owner, resource) as (client, workspace, channel_id, _):
        kwargs = {"channel": channel_id, "limit": limit}
        if cursor:
            kwargs["cursor"] = cursor
        if thread_id is not None:
            result = _call(client, workspace, "conversations_replies", ts=thread_id, **kwargs)
        else:
            result = _call(client, workspace, "conversations_history", **kwargs)
        messages = [safe_message(item) for item in result.get("messages", []) if isinstance(item, dict)]
        if query is not None:
            messages = [m for m in messages if query.casefold() in m["text"].casefold()]
        return {"resource": resource, "messages": messages,
                "next_cursor": str((result.get("response_metadata") or {}).get("next_cursor", "")),
                "search_mode": "bounded_channel_page" if query is not None else None}


def execute_approved_action(action, *, idempotency_key):
    """Only the core outbox calls this inside its locked approval fence."""
    if action.kind != "message.send" or action.status != "sending":
        raise PermissionDenied("The action is not an approved message send.")
    payload = action.payload
    if (not isinstance(payload, dict) or set(payload) - {"text", "thread_id"}
            or not isinstance(payload.get("text"), str) or not payload["text"].strip()
            or len(payload["text"]) > 12000
            or any(mention in payload["text"] for mention in ("<!channel", "<!here", "<!everyone"))):
        raise ValidationError("Invalid approved message.")
    kwargs = {"text": payload["text"], "unfurl_links": False, "unfurl_media": False,
              "parse": "none", "client_msg_id": str(action.pk),
              "metadata": {"event_type": "mlai_agent_action", "event_payload": {
                  "action_id": str(action.pk), "agent_id": str(action.task.grant.installation_id)}}}
    if payload.get("thread_id"):
        if not TIMESTAMP_RE.fullmatch(str(payload["thread_id"])):
            raise ValidationError("Invalid approved thread.")
        kwargs["thread_ts"] = payload["thread_id"]
    with slack_authority(action.task.grant.installation.owner, action.resource, write=True) as (client, workspace, channel_id, _):
        result = _call(client, workspace, "chat_postMessage", channel=channel_id, **kwargs)
        if result.get("channel") != channel_id or not TIMESTAMP_RE.fullmatch(str(result.get("ts", ""))):
            raise SourceUnavailable("Slack did not return an unambiguous message receipt.")
        return {"external_id": result["ts"], "provider": "slack", "workspace_id": workspace, "channel_id": channel_id,
                "message_ts": result["ts"], "idempotency_key": str(idempotency_key)}
