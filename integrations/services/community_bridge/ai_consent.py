"""Consent boundary for Chat content mirrored into public Slack channels."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from community_chat.privacy import has_ai_consent
from community_chat.models import CommunityChatDevice
from integrations.models import CommunityBridgePlatform
from .identity import community_account_for_key, verified_identity_for_buzz


class PublicBridgeConsentRequired(RuntimeError):
    """A queued public send lacks current account permission for AI sharing."""

    permanent = True


def send_with_ai_consent(delivery, send, **kwargs):
    """Recheck the signed sender and hold their consent lock through Slack I/O.

    Public Slack history can become Roo context without an explicit mention.
    Require permission for every Chat-origin content write rather than relying
    on bot membership or message text. Call only for creates, edits and reaction
    additions; deletion and reaction removal must remain possible after opt-out.
    The synchronous wrapper keeps the check, lock and I/O in one worker thread.
    """
    if delivery.get("source_platform") != CommunityBridgePlatform.BUZZ:
        return send(**kwargs)
    from .member_channels import member_channel_in_scope

    channel = delivery.get("channel") or {}
    phase5 = bool(getattr(settings, "SLACK_COMMUNITY_SEND_AS_MEMBER", False)
                  and member_channel_in_scope(channel.get("slack_workspace_id"), channel.get("slack_channel_id")))
    if not phase5 and not getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True):
        return send(**kwargs)

    source = {
        "slack_workspace_id": (delivery.get("channel") or {}).get("slack_workspace_id", ""),
        "buzz_pubkey": str((delivery.get("payload") or {}).get("source_author_id") or ""),
    }
    resolver = community_account_for_key if phase5 else verified_identity_for_buzz
    identity = resolver(**source)
    if not identity or not identity.get("user_profile_id"):
        raise PublicBridgeConsentRequired("Public Slack sharing requires a verified MLAI account")

    with transaction.atomic():
        user = get_user_model().objects.select_for_update().filter(
            community_chat_profile_id=identity["user_profile_id"], is_active=True,
        ).first()
        # Device revocation follows User -> Device, the same order as sends.
        if user is not None:
            list(CommunityChatDevice.objects.select_for_update().filter(
                user_id=user.pk, public_key=source["buzz_pubkey"],
            ).order_by("pk"))
        # Queued work and a pre-lock lookup cannot authorize a revoked device or
        # a link that now belongs to a different account.
        current = resolver(**source)
        if (user is None or not current
                or current.get("user_profile_id") != str(user.community_chat_profile_id)
                or (not phase5 and not current.get("slack_user_id"))
                or ((phase5 or getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True))
                    and not has_ai_consent(user.pk))):
            raise PublicBridgeConsentRequired(
                "Review and allow AI sharing in Settings before sharing to public Slack"
            )
        return send(**kwargs)
