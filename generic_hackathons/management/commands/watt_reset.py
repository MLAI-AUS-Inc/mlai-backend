"""Reset Watt The Hack game state in Firebase Realtime Database.

The streamed game persists per-team state across TWO RTDB trees, both under
``classes/{classId}``::

    classes/{cid}/households/{hid}/{data,scores,dailyTrend}        # sim / Netcode (Tree A)
    classes/{cid}/hackathon/households/{hid}/{observations,...}    # smart-home bus + web HUD (Tree B)

plus shared session state at ``classes/{cid}/data`` and frozen end-of-campaign copies at
``classArchives/{cid}``.

WHY THIS EXISTS: resetting ``WATT_CAMPAIGN_START`` alone is NOT enough to replay a team.
A team that already bootstrapped keeps ``bootstrapVersionApplied`` in
``households/{hid}/scores`` + ``/data``, so on reconnect Unity SKIPS bootstrap and
``SimulateCatchUp`` refuses to rewind to day 1 — the team resumes corrupted mid-campaign
state on a fresh clock. Deleting the nodes below forces a clean re-bootstrap.

Usage::

    python manage.py watt_reset --household-id TEAM1       # one team back to ground state
    python manage.py watt_reset --all                      # wipe EVERY team (fresh event)
    python manage.py watt_reset --all --dry-run            # show what would be deleted, delete nothing
    python manage.py watt_reset --household-id TEAM1 --yes  # skip the confirmation prompt

This talks to whatever RTDB the backend's Firebase creds point at (prod by default), so it
prints the target database URL and asks for confirmation before deleting.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.firebase_utils import rtdb_delete, rtdb_get
from generic_hackathons.smart_home_firebase import clean_segment


def _database_url():
    """Best-effort lookup of the RTDB the default Firebase app is wired to (for the operator)."""
    try:
        import firebase_admin
        return firebase_admin.get_app().options.get("databaseURL") or "(no databaseURL on app)"
    except Exception:  # noqa: BLE001
        return getattr(settings, "WATT_FIREBASE_DATABASE_URL", "(unknown)")


class Command(BaseCommand):
    help = "Reset Watt The Hack RTDB game state (one team, or the whole class) to ground state."

    def add_arguments(self, parser):
        parser.add_argument("--class-id", default=None,
                            help="Firebase class id (default: settings.WATT_HACKATHON_CLASS_ID or WATT).")
        parser.add_argument("--household-id", default=None,
                            help="Reset a single team's state, i.e. the team code, e.g. TEAM1.")
        parser.add_argument("--all", action="store_true",
                            help="Reset the ENTIRE class: every team + shared session state + archives.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show the paths that would be deleted (and what's there); delete nothing.")
        parser.add_argument("--yes", action="store_true",
                            help="Skip the interactive confirmation prompt (for scripts/CI).")

    def handle(self, *args, **opts):
        cid = clean_segment(opts["class_id"] or getattr(settings, "WATT_HACKATHON_CLASS_ID", "WATT"))
        household = opts["household_id"]
        wipe_all = opts["all"]

        if wipe_all and household:
            raise CommandError("Pass either --household-id or --all, not both.")
        if not wipe_all and not household:
            raise CommandError("Nothing to do. Pass --household-id <TEAM> for one team, or --all for the whole class.")

        if wipe_all:
            paths = [f"classes/{cid}", f"classArchives/{cid}"]
            scope = f"ALL teams in class '{cid}' (+ shared session state + archives)"
        else:
            hid = clean_segment(household)
            paths = [
                f"classes/{cid}/households/{hid}",            # Tree A: data / scores / dailyTrend
                f"classes/{cid}/hackathon/households/{hid}",  # Tree B: observations / commands / score / shop / ...
            ]
            scope = f"team '{hid}' in class '{cid}'"

        self.stdout.write(self.style.MIGRATE_HEADING(f"Target RTDB:  {_database_url()}"))
        self.stdout.write(self.style.MIGRATE_HEADING(f"Reset scope:  {scope}"))

        # Show what currently lives at each path (top-level keys only) so the operator can sanity-check.
        any_data = False
        for path in paths:
            try:
                value = rtdb_get(path)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(f"Failed to read {path} (RTDB wiring/creds problem?): {exc}") from exc
            if value is None:
                self.stdout.write(f"  {path}  ->  (empty, nothing to delete)")
            elif isinstance(value, dict):
                any_data = True
                self.stdout.write(f"  {path}  ->  keys: {', '.join(sorted(value.keys()))}")
            else:
                any_data = True
                self.stdout.write(f"  {path}  ->  {type(value).__name__}: {value!r}")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run set; nothing deleted."))
            return

        if not any_data:
            self.stdout.write(self.style.SUCCESS("Already at ground state; nothing to delete."))
            return

        if not opts["yes"]:
            confirm = input(f"\nPermanently DELETE the above from {_database_url()}? Type 'yes' to proceed: ")
            if confirm.strip().lower() != "yes":
                self.stdout.write(self.style.WARNING("Aborted; nothing deleted."))
                return

        for path in paths:
            try:
                rtdb_delete(path)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(f"Failed to delete {path}: {exc}") from exc
            self.stdout.write(self.style.SUCCESS(f"Deleted {path}"))

        self.stdout.write(self.style.SUCCESS(
            f"Done. Reset {scope}. Affected teams will re-bootstrap fresh on next connect."))
