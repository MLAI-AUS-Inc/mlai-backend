"""Preview or apply a specifically reviewed historical bonus approval."""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from community_chat.volunteer.access import VolunteerError, linked_member
from community_chat.volunteer.backfill import (
    award_historical_bonuses,
    historical_bonus_preview,
)
from community_chat.volunteer.policy import levels


class Command(BaseCommand):
    help = "Preview historical Volunteer bonuses; --execute requires an explicit reviewed state and approved levels."

    def add_arguments(self, parser):
        parser.add_argument("--member-id", type=int, required=True)
        parser.add_argument("--reviewer-id", type=int, required=True)
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--expected-opening-roo")
        parser.add_argument("--expected-ledger-cutoff", type=int)
        parser.add_argument("--expected-state-token")
        parser.add_argument(
            "--approved-level",
            action="append",
            choices=[level["key"] for level in levels()[1:]],
        )
        parser.add_argument("--reason")

    def handle(self, *args, **options):
        try:
            member = linked_member(options["member_id"])
            reviewer = (
                get_user_model()
                .objects.filter(pk=options["reviewer_id"], is_active=True)
                .first()
            )
            if reviewer is None:
                raise VolunteerError("not_authorised", 403)
            if options["execute"]:
                required = (
                    "expected_opening_roo",
                    "expected_ledger_cutoff",
                    "expected_state_token",
                    "approved_level",
                    "reason",
                )
                if any(options[key] is None for key in required):
                    raise CommandError(
                        "Execution requires the reviewed opening, cutoff, state token, explicit approved levels and reason."
                    )
                result = award_historical_bonuses(
                    member,
                    reviewer,
                    expected_opening_roo=options["expected_opening_roo"],
                    expected_ledger_cutoff=options["expected_ledger_cutoff"],
                    expected_state_token=options["expected_state_token"],
                    approved_level_keys=options["approved_level"],
                    reason=options["reason"],
                )
            else:
                result = historical_bonus_preview(member, reviewer)
        except VolunteerError as exc:
            raise CommandError(exc.code) from exc
        self.stdout.write(json.dumps(result, indent=2, sort_keys=True))
