"""Keep Members bridge sources out of independent AI/analytics consumers.

A Slack public-channel ID is not Community permission. This policy is keyed by
the exact workspace and source channel, including held/disabled mappings. It
does not authorize Chat reads or alter the encrypted owner-private queue.
"""
from django.db.models import Exists, OuterRef

from integrations.models import CommunityBridgeChannel


def is_members_source(workspace_id, channel_id):
    if not workspace_id or not channel_id:
        return False
    return CommunityBridgeChannel.objects.filter(
        slack_workspace_id=workspace_id, slack_channel_id=channel_id,
        audience="members",
    ).exists()


def exclude_members_sources(queryset, *, workspace_field="connection__external_account_id",
                            channel_field="channel_id"):
    """A current SQL fence, also covering artifacts retained before a mapping."""
    blocked = CommunityBridgeChannel.objects.filter(
        audience="members", slack_workspace_id=OuterRef(workspace_field),
        slack_channel_id=OuterRef(channel_field),
    )
    return queryset.alias(_members_bridge_source=Exists(blocked)).filter(
        _members_bridge_source=False,
    )


def slack_source_queryset(model):
    """Independent connector artifacts/selections, never personal mirror rows."""
    return exclude_members_sources(model.objects.all())
