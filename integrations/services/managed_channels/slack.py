"""Optional workspace-owned event conversation. No Slack history or message reads."""
import os
import re
import unicodedata

from django.conf import settings
from django.db import connection, transaction
from integrations import http_client

from integrations.models import CommunityBridgeChannel, ManagedSlackEventChannel


class SlackHold(Exception):
    pass


def capabilities(config):
    key = os.environ.get(config.get("slack_bot_token_env", "MANAGED_CHANNEL_SLACK_BOT_TOKEN"), "")
    workspace = config.get("slack_workspace_id")
    if not key.startswith("xoxb-") or not workspace:
        raise SlackHold("workspace_installation_unverified")
    result = http_client.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {key}"}, timeout=5, allow_redirects=False)
    result.raise_for_status()
    data = result.json()
    scopes = {scope.strip() for scope in result.headers.get("x-oauth-scopes", "").split(",")}
    if (data.get("ok") is not True or data.get("team_id") != workspace or not data.get("bot_id")
            or "channels:manage" not in scopes):
        raise SlackHold("workspace_management_grant_unverified")
    return key


def handle(title):
    value = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub("[^a-z0-9_-]+", "-", value).strip("-_")[:80].rstrip("-_") or "event"


def metadata_only(workspace, channel_id, name):
    from django.db.models import Q
    if not getattr(settings, "MANAGED_CHANNEL_SOURCES", {}):
        # A feature-off bridge may roll out before its optional registry, or
        # continue after installations are paused and reservations still exist.
        # Only new candidate public rooms reach this compatibility check.
        if ManagedSlackEventChannel._meta.db_table not in connection.introspection.table_names():
            return False
    return ManagedSlackEventChannel.objects.filter(workspace=workspace).filter(
        Q(slack_channel_id=channel_id) | Q(slack_channel_id__isnull=True, handle=name)
    ).exists()


def _call(method, key, payload):
    result = http_client.post(f"https://slack.com/api/conversations.{method}", json=payload,
                           headers={"Authorization": f"Bearer {key}"}, timeout=8, allow_redirects=False)
    result.raise_for_status()
    data = result.json()
    if data.get("ok") is not True:
        raise SlackHold(str(data.get("error", "invalid_slack_response"))[:60])
    return data


def sync(config, room, desired):
    if config.get("slack_events") is not True:
        raise SlackHold("slack_disabled")
    if room.visibility != "public" or desired.get("status") == "restricted":
        raise SlackHold("restricted_audience")
    if room.adopted:
        raise SlackHold("existing_shared_room_needs_explicit_slack_binding")
    key = capabilities(config)
    workspace = config["slack_workspace_id"]
    slug = handle(desired["title"])
    with transaction.atomic():
        row = ManagedSlackEventChannel.objects.select_for_update().filter(room=room).first()
        if not row:
            if desired["status"] in {"removed", "archived", "canceled", "ended"}:
                return
            if (ManagedSlackEventChannel.objects.filter(workspace=workspace, handle=slug).exists()
                    or CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace, slack_channel_name=slug).exists()):
                slug = slug[:70] + "-" + room.channel_id.hex[:8]
            row = ManagedSlackEventChannel.objects.create(room=room, workspace=workspace, handle=slug)
        if row.workspace != workspace:
            raise SlackHold("workspace_identity_changed")
        if row.slack_channel_id:
            return _update(row, key, slug, desired["status"], config)
        if row.create_attempted:
            raise SlackHold("indeterminate_create_requires_verified_slack_id")
        row.create_attempted = True
        row.save(update_fields=["create_attempted"])
    # The durable pre-create reservation is visible to bridge discovery before
    # Slack emits channel_created. Never repeat an indeterminate external write.
    try:
        data = _call("create", key, {"name": row.handle, "is_private": False})
    except SlackHold as exc:
        # First-attempt, explicit Slack failures prove this request did not
        # create a room. A lost response is handled only by positive evidence.
        if str(exc) == "name_taken":
            alternate = handle(desired["title"])[:70] + "-" + room.channel_id.hex[:8]
            if row.handle != alternate:
                ManagedSlackEventChannel.objects.filter(pk=row.pk).update(handle=alternate, create_attempted=False)
        elif str(exc) in {"missing_scope", "not_allowed_token_type", "no_permission", "restricted_action", "invalid_auth"}:
            ManagedSlackEventChannel.objects.filter(pk=row.pk).update(create_attempted=False)
        raise
    except http_client.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ManagedSlackEventChannel.objects.filter(pk=row.pk).update(create_attempted=False)
        raise
    result = data.get("channel") or {}
    if not result.get("id") or result.get("name") != row.handle or result.get("is_private") is not False:
        raise SlackHold("unverified_created_channel")
    ManagedSlackEventChannel.objects.filter(pk=row.pk, slack_channel_id__isnull=True).update(slack_channel_id=result["id"])


def _update(row, key, slug, status, config):
    if status in {"removed", "archived"}:
        if config.get("slack_archive_removed") is True:
            try:
                _call("archive", key, {"channel": row.slack_channel_id})
            except SlackHold as exc:
                if str(exc) != "already_archived":
                    raise
        return
    if config.get("slack_archive_removed") is True:
        try:
            _call("unarchive", key, {"channel": row.slack_channel_id})
        except SlackHold as exc:
            if str(exc) != "not_archived":
                raise
    if slug != row.handle and row.handle != slug[:70] + "-" + row.room.channel_id.hex[:8]:
        data = _call("rename", key, {"channel": row.slack_channel_id, "name": slug})
        if data.get("channel", {}).get("id") != row.slack_channel_id:
            raise SlackHold("unverified_rename")
        row.handle = data["channel"]["name"]
        row.save(update_fields=["handle"])
