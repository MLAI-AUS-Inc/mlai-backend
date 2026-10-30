"""Mint one opaque MLAI Chat invite link (workshop cohorts or individual invites).

The link carries ``intent`` for routing and an encrypted ``inv1.`` token; the
cohort label, email and name never appear in the URL. Links resolve only while
``COMMUNITY_CHAT_INVITE_TOKENS_ENABLED`` is on.
"""

from django.core.management.base import BaseCommand, CommandError

from community_chat.invite_tokens import (
    INVITE_INTENTS,
    InvalidInviteToken,
    clamp_ttl_hours,
    invite_link,
    invite_tokens_enabled,
    max_ttl_hours,
    mint_invite_token,
)


class Command(BaseCommand):
    help = "Print one signed, short-lived MLAI Chat invite link."

    def add_arguments(self, parser):
        parser.add_argument("--intent", choices=INVITE_INTENTS, default=None,
                            help="Route the invitee straight to My startup after joining.")
        parser.add_argument("--cohort", required=True, help="Cohort label, up to 40 plain characters.")
        parser.add_argument("--email", default=None, help="Prefill for an individually sent invite.")
        parser.add_argument("--name", default=None, help="Prefill for an individually sent invite.")
        parser.add_argument("--ttl-hours", type=int, default=None,
                            help="Lifetime in hours; capped at COMMUNITY_CHAT_INVITE_TOKEN_MAX_TTL_HOURS.")

    def handle(self, *args, **options):
        requested = options["ttl_hours"]
        ttl_hours = clamp_ttl_hours(requested)
        try:
            token = mint_invite_token(
                intent=options["intent"], cohort=options["cohort"], email=options["email"],
                name=options["name"], ttl_hours=ttl_hours,
            )
        except InvalidInviteToken as exc:
            raise CommandError(f"Invite details are not valid ({exc}).") from exc
        if requested is not None and requested > ttl_hours:
            self.stderr.write(f"TTL capped at {max_ttl_hours()} hours.")
        if not invite_tokens_enabled():
            self.stderr.write("COMMUNITY_CHAT_INVITE_TOKENS_ENABLED is off: this link will not resolve yet.")
        self.stdout.write(invite_link(token, intent=options["intent"]))
