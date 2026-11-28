"""Export one calendar year of MLAI Wrapped stats as a media-intel stat sheet.

Read-only. The CSV (``Name`` plus stat columns) goes to stdout or ``--output``;
the name review, collisions and notes go to stderr, because they name MLAI
Chat profile IDs that must never travel in the sheet itself.
"""

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from community_chat.admin_member_progress_data import local_day
from community_chat.wrapped_stats import sheet
from community_chat.wrapped_stats_data import SCOPES, wrapped_people


def _uuid(value):
    try:
        return str(UUID(value))
    except ValueError:
        raise CommandError(f"Not an MLAI Chat profile ID: {value!r}") from None


def _override(value):
    profile_id, separator, name = value.partition("=")
    if not separator or not name.strip():
        raise CommandError("--name takes PROFILE_ID=Preferred Name")
    return _uuid(profile_id.strip()), name.strip()


class Command(BaseCommand):
    help = "Export a year of MLAI Wrapped stats (committee by default) as media-intel's stat-sheet CSV."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True, help="Calendar year, Australia/Melbourne.")
        parser.add_argument("--scope", choices=SCOPES, default="committee")
        parser.add_argument(
            "--member", action="append", default=[], metavar="PROFILE_ID",
            help="An MLAI Chat public profile ID; repeat for several. Implies --scope members.",
        )
        parser.add_argument(
            "--name", action="append", default=[], metavar="PROFILE_ID=NAME",
            help="Confirmed name for one member, overriding the preferred-name lookup.",
        )
        parser.add_argument("--output", help="Write the CSV here instead of stdout.")

    def handle(self, *args, **options):
        now = timezone.now()
        year = options["year"]
        if not 2025 <= year <= local_day(now).year:
            raise CommandError("Choose a year from 2025 onward that has started.")
        members = [_uuid(value) for value in options["member"]]
        scope = "members" if members else options["scope"]
        if scope == "members" and not members:
            raise CommandError("--scope members needs at least one --member.")
        overrides = dict(_override(value) for value in options["name"])

        people, selection = wrapped_people(year, now, scope=scope, member_ids=members, overrides=overrides)
        result = sheet(people)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8", newline="") as handle:
                handle.write(result["csv"])
        else:
            self.stdout.write(result["csv"], ending="")

        review = result["review"]
        err = self.stderr
        err.write(f"MLAI Wrapped {year}, scope {scope}: {result['rows']} people, {len(result['columns']) - 1} stat columns.")
        for person in review["people"]:
            err.write(f"  {person['name']}  [{person['name_source']}]  {person['id']}")
        for collision in review["name_collisions"]:
            err.write(self.style.WARNING(
                f"Same name, kept as separate rows; confirm one by one: {collision['name']} "
                + ", ".join(member["id"] for member in collision["members"])
            ))
        for item in review["needs_name"]:
            err.write(self.style.WARNING(f"Needs a confirmed name ({item['reason']}): {item['id']}"))
        for key, label in (
            ("unknown_profile_ids", "Unknown profile ID"),
            ("without_qualifying_record", "Selected but not in the member roster"),
        ):
            for profile_id in selection[key]:
                err.write(self.style.WARNING(f"{label}: {profile_id}"))
        for note in result["notes"]:
            err.write(f"Note: {note}")
        if options["verbosity"] > 1:
            err.write(json.dumps(dict(review=review, selection=selection), indent=2))
