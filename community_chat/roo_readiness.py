"""Whether Public Roo can act on this member's messages sent from MLAI Chat.

Roo resolves a Chat-origin Slack message through
``integrations.services.community_bridge.roo_actor.resolve_roo_actor``, which
requires a Slack identity link in Roo's workspace and current AI consent.
This reports the same two facts so Chat can warn the member before they send
a request that Roo would have to refuse. It grants no authority itself.
"""

from django.conf import settings

READY = "ready"
SLACK_NOT_LINKED = "slack_not_linked"
AI_SHARING_OFF = "ai_sharing_off"


def roo_request_readiness(user):
    """Return ``ready``, ``slack_not_linked``, ``ai_sharing_off`` or ``None``.

    ``None`` means Public Roo is not configured for this deployment.
    """
    from community_chat.privacy import has_ai_consent
    from integrations.models import CommunityBridgeIdentityLink
    from integrations.services.slack_roo import public_roo_target

    target = public_roo_target()
    if not target:
        return None
    linked = (
        CommunityBridgeIdentityLink.objects.filter(
            user_id=user.pk,
            slack_workspace_id=target[0],
            revoked_at__isnull=True,
        )
        .exclude(slack_user_id="")
        .exists()
    )
    if not linked:
        return SLACK_NOT_LINKED
    if getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True) and not has_ai_consent(user.pk):
        return AI_SHARING_OFF
    return READY
