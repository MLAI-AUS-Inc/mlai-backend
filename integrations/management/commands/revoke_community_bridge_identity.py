import json

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from integrations.models import CommunityBridgeIdentityLink


class Command(BaseCommand):
    help = "Revoke a verified Slack-to-MLAI Chat identity link."

    def add_arguments(self, parser):
        parser.add_argument("--slack-workspace-id", required=True)
        parser.add_argument("--slack-user-id", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        workspace_id = str(options["slack_workspace_id"] or "").strip()
        slack_user_id = str(options["slack_user_id"] or "").strip()
        reason = str(options["reason"] or "").strip()
        if not workspace_id or not slack_user_id or not reason:
            raise CommandError("workspace, Slack user, and revocation reason are required.")
        if len(reason) > 255:
            raise CommandError("--reason exceeds 255 characters.")
        link = CommunityBridgeIdentityLink.objects.filter(
            slack_workspace_id=workspace_id,
            slack_user_id=slack_user_id,
            revoked_at__isnull=True,
        ).first()
        if link is None:
            raise CommandError("No active identity link matches that workspace and Slack user.")
        link.revoked_at = timezone.now()
        link.revocation_reason = reason
        link.save(update_fields=["revoked_at", "revocation_reason", "updated_at"])
        self.stdout.write(
            json.dumps(
                {
                    "status": "revoked",
                    "slack_workspace_id": link.slack_workspace_id,
                    "slack_user_id": link.slack_user_id,
                    "buzz_pubkey": link.buzz_pubkey,
                    "revoked_at": link.revoked_at.isoformat(),
                },
                sort_keys=True,
            )
        )
