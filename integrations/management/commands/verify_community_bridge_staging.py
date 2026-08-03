import json

from django.core.management.base import BaseCommand, CommandError

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeDeliveryStatus,
    CommunityBridgeMessageLink,
    CommunityBridgePlatform,
)


class Command(BaseCommand):
    help = "Verify durable evidence from a bidirectional MLAI Chat/Slack staging exercise."

    def add_arguments(self, parser):
        parser.add_argument("--slack-channel-id", required=True)
        parser.add_argument("--slack-message-id", required=True)
        parser.add_argument("--buzz-event-id", required=True)
        parser.add_argument("--slack-reaction-id", default="")
        parser.add_argument("--buzz-reaction-event-id", default="")

    def handle(self, *args, **options):
        channel_id = str(options["slack_channel_id"] or "").strip()
        try:
            channel = CommunityBridgeChannel.objects.get(
                slack_channel_id=channel_id,
                destination_platform=CommunityBridgePlatform.BUZZ,
            )
        except CommunityBridgeChannel.DoesNotExist as exc:
            raise CommandError("MLAI Chat mapping was not found for that Slack channel") from exc

        if not channel.enabled:
            raise CommandError("the MLAI Chat mapping is disabled")
        if not (channel.sync_edits and channel.sync_deletes and channel.sync_replies):
            raise CommandError("edit, delete, and reply synchronization must be enabled")

        evidence = [
            self._verify_link(
                channel=channel,
                source_platform=CommunityBridgePlatform.SLACK,
                source_message_id=options["slack_message_id"],
                destination_platform=CommunityBridgePlatform.BUZZ,
                label="slack_to_mlai_message",
            ),
            self._verify_link(
                channel=channel,
                source_platform=CommunityBridgePlatform.BUZZ,
                source_message_id=options["buzz_event_id"],
                destination_platform=CommunityBridgePlatform.SLACK,
                label="mlai_to_slack_message",
            ),
        ]

        optional_pairs = (
            (
                "slack_reaction_id",
                CommunityBridgePlatform.SLACK,
                CommunityBridgePlatform.BUZZ,
                "slack_to_mlai_reaction",
            ),
            (
                "buzz_reaction_event_id",
                CommunityBridgePlatform.BUZZ,
                CommunityBridgePlatform.SLACK,
                "mlai_to_slack_reaction",
            ),
        )
        for option_name, source_platform, destination_platform, label in optional_pairs:
            source_id = str(options[option_name] or "").strip()
            if source_id:
                evidence.append(
                    self._verify_link(
                        channel=channel,
                        source_platform=source_platform,
                        source_message_id=source_id,
                        destination_platform=destination_platform,
                        label=label,
                    )
                )

        dead_count = CommunityBridgeDelivery.objects.filter(
            channel=channel,
            status=CommunityBridgeDeliveryStatus.DEAD,
        ).count()
        if dead_count:
            raise CommandError(
                f"mapping has {dead_count} dead delivery row(s); investigate before approval"
            )

        self.stdout.write(
            json.dumps(
                {
                    "status": "passed",
                    "mapping_id": channel.id,
                    "slack_channel_id": channel.slack_channel_id,
                    "buzz_channel_id": channel.destination_channel_id,
                    "dead_delivery_count": dead_count,
                    "evidence": evidence,
                },
                sort_keys=True,
            )
        )

    def _verify_link(
        self,
        *,
        channel,
        source_platform,
        source_message_id,
        destination_platform,
        label,
    ):
        source_id = str(source_message_id or "").strip()
        if not source_id:
            raise CommandError(f"{label} source ID is required")
        try:
            link = CommunityBridgeMessageLink.objects.get(
                channel=channel,
                source_platform=source_platform,
                source_message_id=source_id,
                destination_platform=destination_platform,
            )
        except CommunityBridgeMessageLink.DoesNotExist as exc:
            raise CommandError(f"{label} does not have a durable message link") from exc

        incomplete = CommunityBridgeDelivery.objects.filter(
            channel=channel,
            source_platform=source_platform,
            source_message_id=source_id,
        ).exclude(status=CommunityBridgeDeliveryStatus.COMPLETED)
        if incomplete.exists():
            statuses = sorted(set(incomplete.values_list("status", flat=True)))
            raise CommandError(f"{label} has incomplete deliveries: {', '.join(statuses)}")

        if not CommunityBridgeDelivery.objects.filter(
            channel=channel,
            source_platform=source_platform,
            source_message_id=source_id,
            status=CommunityBridgeDeliveryStatus.COMPLETED,
        ).exists():
            raise CommandError(f"{label} does not have a completed delivery")

        return {
            "label": label,
            "source_platform": source_platform,
            "source_message_id": source_id,
            "destination_platform": destination_platform,
            "destination_message_id": link.destination_message_id,
        }
