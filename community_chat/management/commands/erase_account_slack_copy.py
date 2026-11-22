"""Review or erase one mapped Slack copy for a confirmed deletion request."""

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from slack_sdk.errors import SlackApiError, SlackRequestError

from community_chat.deletion_slack import SlackCopyErasureError, erase_slack_copy


class Command(BaseCommand):
    help = "Preview or erase one reviewed MLAI-controlled Slack copy."

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=UUID)
        parser.add_argument("link_id", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            result = erase_slack_copy(
                request_id=options["request_id"], link_id=options["link_id"], apply=options["apply"],
            )
        except SlackCopyErasureError as error:
            raise CommandError(str(error)) from error
        except (SlackApiError, SlackRequestError) as error:
            raise CommandError("Slack is unavailable. Retry this copy without closing the deletion case.") from error
        self.stdout.write(result)
