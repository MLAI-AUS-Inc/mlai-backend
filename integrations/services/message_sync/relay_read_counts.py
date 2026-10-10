"""Cut over mapped-room counts only after a current source-cursor capability check."""
from uuid import UUID
from django.conf import settings


def enabled_for(target, inbox_context):
    """Source inventory IDs are not relay rooms, even when wrapped as conversations."""
    if (not getattr(settings, 'MESSAGE_SYNC_RELAY_READ_COUNTS', False)
            or not isinstance(inbox_context, dict)
            or inbox_context.get('source_id') != target.slack_id):
        return False
    mapped = getattr(target.conversation, 'mlai_channel_id', None)
    if mapped is None:
        mapped = getattr(target.bridge, 'destination_channel_id', None)
    if not mapped:
        return False
    try:
        return UUID(str(target.channel_id)) == UUID(str(mapped)) == UUID(str(inbox_context.get('channel_id')))
    except (ValueError, TypeError, AttributeError):
        return False
