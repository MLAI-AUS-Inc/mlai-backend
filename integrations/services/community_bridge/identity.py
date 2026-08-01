from typing import Optional

from integrations.models import CommunityBridgeIdentityLink


def verified_identity_for_slack(
    *,
    slack_workspace_id: str,
    slack_user_id: str,
) -> Optional[dict]:
    return _serialize(
        CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=str(slack_workspace_id or "").strip(),
            slack_user_id=str(slack_user_id or "").strip(),
            revoked_at__isnull=True,
        ).first()
    )


def verified_identity_for_buzz(
    *,
    slack_workspace_id: str,
    buzz_pubkey: str,
) -> Optional[dict]:
    return _serialize(
        CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=str(slack_workspace_id or "").strip(),
            buzz_pubkey=str(buzz_pubkey or "").strip().lower(),
            revoked_at__isnull=True,
        ).first()
    )


def _serialize(link: Optional[CommunityBridgeIdentityLink]) -> Optional[dict]:
    if link is None:
        return None
    return {
        "id": link.id,
        "slack_workspace_id": link.slack_workspace_id,
        "slack_user_id": link.slack_user_id,
        "buzz_pubkey": link.buzz_pubkey,
        "display_name": link.display_name,
        "verification_method": link.verification_method,
        "verified_at": link.verified_at.isoformat(),
    }
