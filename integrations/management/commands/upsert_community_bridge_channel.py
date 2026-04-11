import json
import re

from django.core.management.base import BaseCommand, CommandError

from integrations.models import CommunityBridgeChannel


DISCORD_CHANNEL_URL_RE = re.compile(
    r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>\d+)/(?P<channel_id>\d+)(?:/\d+)?/?$"
)


class Command(BaseCommand):
    help = "Create or update a Slack to Discord community bridge channel mapping."

    def add_arguments(self, parser):
        parser.add_argument("--slack-channel-id", required=True)
        parser.add_argument("--slack-channel-name", default="")
        parser.add_argument("--discord-url", default="")
        parser.add_argument("--discord-guild-id", default="")
        parser.add_argument("--discord-channel-id", default="")
        parser.add_argument("--discord-channel-name", default="")
        parser.add_argument("--disabled", action="store_true")
        parser.add_argument("--no-sync-edits", action="store_true")
        parser.add_argument("--no-sync-deletes", action="store_true")
        parser.add_argument("--no-sync-replies", action="store_true")

    def handle(self, *args, **options):
        slack_channel_id = self._require_non_empty(options["slack_channel_id"], "--slack-channel-id")
        slack_channel_name = str(options["slack_channel_name"] or "").strip()
        discord_guild_id, discord_channel_id = self._resolve_discord_target(options)
        discord_channel_name = str(options["discord_channel_name"] or "").strip()

        conflict = (
            CommunityBridgeChannel.objects.filter(discord_channel_id=discord_channel_id)
            .exclude(slack_channel_id=slack_channel_id)
            .first()
        )
        if conflict is not None:
            raise CommandError(
                f"Discord channel {discord_channel_id} is already mapped to Slack channel "
                f"{conflict.slack_channel_id}."
            )

        channel, created = CommunityBridgeChannel.objects.update_or_create(
            slack_channel_id=slack_channel_id,
            defaults={
                "slack_channel_name": slack_channel_name,
                "discord_guild_id": discord_guild_id,
                "discord_channel_id": discord_channel_id,
                "discord_channel_name": discord_channel_name,
                "enabled": not options["disabled"],
                "sync_edits": not options["no_sync_edits"],
                "sync_deletes": not options["no_sync_deletes"],
                "sync_replies": not options["no_sync_replies"],
            },
        )

        self.stdout.write(
            json.dumps(
                {
                    "status": "created" if created else "updated",
                    "slack_channel_id": channel.slack_channel_id,
                    "slack_channel_name": channel.slack_channel_name,
                    "discord_guild_id": channel.discord_guild_id,
                    "discord_channel_id": channel.discord_channel_id,
                    "discord_channel_name": channel.discord_channel_name,
                    "enabled": channel.enabled,
                    "sync_edits": channel.sync_edits,
                    "sync_deletes": channel.sync_deletes,
                    "sync_replies": channel.sync_replies,
                },
                sort_keys=True,
            )
        )

    def _resolve_discord_target(self, options):
        discord_url = str(options["discord_url"] or "").strip()
        provided_guild_id = str(options["discord_guild_id"] or "").strip()
        provided_channel_id = str(options["discord_channel_id"] or "").strip()

        parsed_guild_id = ""
        parsed_channel_id = ""
        if discord_url:
            parsed_guild_id, parsed_channel_id = self._parse_discord_url(discord_url)

        discord_guild_id = provided_guild_id or parsed_guild_id
        discord_channel_id = provided_channel_id or parsed_channel_id

        if not discord_guild_id:
            raise CommandError(
                "Provide --discord-url or --discord-guild-id together with --discord-channel-id."
            )
        if not discord_channel_id:
            raise CommandError(
                "Provide --discord-url or --discord-guild-id together with --discord-channel-id."
            )

        if provided_guild_id and parsed_guild_id and provided_guild_id != parsed_guild_id:
            raise CommandError(
                f"--discord-guild-id ({provided_guild_id}) does not match --discord-url ({parsed_guild_id})."
            )
        if provided_channel_id and parsed_channel_id and provided_channel_id != parsed_channel_id:
            raise CommandError(
                f"--discord-channel-id ({provided_channel_id}) does not match --discord-url ({parsed_channel_id})."
            )

        return discord_guild_id, discord_channel_id

    def _parse_discord_url(self, url):
        match = DISCORD_CHANNEL_URL_RE.match(url)
        if match is None:
            raise CommandError(
                "Invalid Discord channel URL. Expected "
                "https://discord.com/channels/<guild_id>/<channel_id>."
            )
        return match.group("guild_id"), match.group("channel_id")

    def _require_non_empty(self, value, flag_name):
        normalized = str(value or "").strip()
        if not normalized:
            raise CommandError(f"{flag_name} is required.")
        return normalized
