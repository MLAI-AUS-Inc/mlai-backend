import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
)


PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")


class Command(BaseCommand):
    help = "Record an audited Slack-to-MLAI Chat identity link after dual-control verification."

    def add_arguments(self, parser):
        parser.add_argument("--slack-workspace-id", required=True)
        parser.add_argument("--slack-user-id", required=True)
        parser.add_argument("--buzz-pubkey", required=True)
        parser.add_argument("--display-name", required=True)
        parser.add_argument(
            "--verification-method",
            choices=tuple(CommunityBridgeIdentityVerificationMethod.values),
            default=CommunityBridgeIdentityVerificationMethod.OPERATOR_ATTESTED,
        )
        parser.add_argument("--verification-reference", required=True)
        parser.add_argument("--confirm-dual-control", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_dual_control"]:
            raise CommandError(
                "--confirm-dual-control is required after independently verifying the Slack account and Nostr key."
            )
        workspace_id = self._required(options["slack_workspace_id"], "--slack-workspace-id", 100)
        slack_user_id = self._required(options["slack_user_id"], "--slack-user-id", 100)
        buzz_pubkey = self._required(options["buzz_pubkey"], "--buzz-pubkey", 64).lower()
        display_name = self._required(options["display_name"], "--display-name", 255)
        proof_reference = self._required(
            options["verification_reference"],
            "--verification-reference",
            255,
        )
        if not PUBKEY_RE.fullmatch(buzz_pubkey):
            raise CommandError("--buzz-pubkey must be a 64-character hex public key.")

        conflict = (
            CommunityBridgeIdentityLink.objects.filter(
                slack_workspace_id=workspace_id,
                buzz_pubkey=buzz_pubkey,
            )
            .exclude(slack_user_id=slack_user_id)
            .first()
        )
        if conflict is not None:
            raise CommandError("That public key is already linked to another Slack user in this workspace.")

        try:
            with transaction.atomic():
                link, created = CommunityBridgeIdentityLink.objects.update_or_create(
                    slack_workspace_id=workspace_id,
                    slack_user_id=slack_user_id,
                    defaults={
                        "buzz_pubkey": buzz_pubkey,
                        "display_name": display_name,
                        "verification_method": options["verification_method"],
                        "verification_reference": proof_reference,
                        "verified_at": timezone.now(),
                        "revoked_at": None,
                        "revocation_reason": "",
                    },
                )
        except IntegrityError as exc:
            raise CommandError("The identity link conflicts with an existing verified link.") from exc

        self.stdout.write(
            json.dumps(
                {
                    "status": "created" if created else "updated",
                    "slack_workspace_id": link.slack_workspace_id,
                    "slack_user_id": link.slack_user_id,
                    "buzz_pubkey": link.buzz_pubkey,
                    "display_name": link.display_name,
                    "verification_method": link.verification_method,
                    "verified_at": link.verified_at.isoformat(),
                },
                sort_keys=True,
            )
        )

    @staticmethod
    def _required(value: object, option_name: str, maximum: int) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise CommandError(f"{option_name} is required.")
        if len(normalized) > maximum:
            raise CommandError(f"{option_name} exceeds {maximum} characters.")
        return normalized
