import json
import re

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from integrations.models import (
    CommunityBridgeIdentityLink,
    CommunityBridgeIdentityVerificationMethod,
)


PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")


class Command(BaseCommand):
    help = "Record an audited Slack-to-MLAI Chat link for an existing MLAI account."

    def add_arguments(self, parser):
        parser.add_argument("--slack-workspace-id", required=True)
        parser.add_argument("--slack-user-id", required=True)
        parser.add_argument("--mlai-profile-id", required=True)
        parser.add_argument("--buzz-pubkey")
        parser.add_argument("--display-name")
        parser.add_argument(
            "--verification-method",
            choices=tuple(CommunityBridgeIdentityVerificationMethod.values),
            default=CommunityBridgeIdentityVerificationMethod.ACCOUNT_CHALLENGE,
        )
        parser.add_argument("--verification-reference", required=True)
        parser.add_argument("--confirm-dual-control", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_dual_control"]:
            raise CommandError(
                "--confirm-dual-control is required after verifying the Slack and MLAI account ownership."
            )
        workspace_id = self._required(options["slack_workspace_id"], "--slack-workspace-id", 100)
        slack_user_id = self._required(options["slack_user_id"], "--slack-user-id", 100)
        profile_id = self._required(options["mlai_profile_id"], "--mlai-profile-id", 36)
        user = self._account(profile_id=profile_id, slack_user_id=slack_user_id)
        buzz_pubkey = str(options.get("buzz_pubkey") or "").strip().lower()
        device = self._device(user=user, public_key=buzz_pubkey)
        buzz_pubkey = device.public_key
        display_name = str(options.get("display_name") or "").strip() or user.full_name or "MLAI member"
        if len(display_name) > 255:
            raise CommandError("--display-name exceeds 255 characters.")
        proof_reference = self._required(
            options["verification_reference"],
            "--verification-reference",
            255,
        )
        if not PUBKEY_RE.fullmatch(buzz_pubkey):
            raise CommandError("--buzz-pubkey must be a 64-character hex public key.")

        user_conflict = (
            CommunityBridgeIdentityLink.objects.filter(
                slack_workspace_id=workspace_id,
                user=user,
            )
            .exclude(slack_user_id=slack_user_id)
            .first()
        )
        if user_conflict is not None:
            raise CommandError("That MLAI account is already linked to another Slack user in this workspace.")

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
                        "user": user,
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
                    "mlai_profile_id": str(user.community_chat_profile_id),
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
    def _account(*, profile_id: str, slack_user_id: str):
        user = get_user_model().objects.filter(
            community_chat_profile_id=profile_id,
            is_active=True,
        ).first()
        if user is None:
            raise CommandError("No active MLAI account matches --mlai-profile-id.")
        if str(user.slack_id or "").strip() != slack_user_id:
            raise CommandError("The Slack user is not connected to that MLAI account.")
        return user

    @staticmethod
    def _device(*, user, public_key: str) -> CommunityChatDevice:
        devices = CommunityChatDevice.objects.filter(
            user=user,
            status=DeviceBindingStatus.VERIFIED,
            revoked_at__isnull=True,
        )
        if public_key:
            if not PUBKEY_RE.fullmatch(public_key):
                raise CommandError("--buzz-pubkey must be a 64-character hex public key.")
            device = devices.filter(public_key=public_key).first()
            if device is None:
                raise CommandError("That public key is not an active device for the MLAI account.")
            return device
        device = devices.order_by(
            F("last_seen_at").desc(nulls_last=True),
            F("verified_at").desc(nulls_last=True),
            "-created_at",
        ).first()
        if device is None:
            raise CommandError("The MLAI account has no active verified chat device.")
        return device

    @staticmethod
    def _required(value: object, option_name: str, maximum: int) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise CommandError(f"{option_name} is required.")
        if len(normalized) > maximum:
            raise CommandError(f"{option_name} exceeds {maximum} characters.")
        return normalized
