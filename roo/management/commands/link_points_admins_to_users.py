"""Backfill ``PointsAdmin.user`` from the Slack ID it already stores.

Historically ``PointsAdmin`` rows were keyed purely on ``slack_user_id`` and
left the ``user`` FK null. The web app (e.g. the Vibe Raising admin dashboard)
gates on ``PointsAdmin.user`` via :func:`roo.permissions.is_points_admin_user`,
so unlinked admins are invisible to the website. Both tables already carry the
Slack ID (``User.slack_id`` is unique), so this command links any active
``PointsAdmin`` whose ``slack_user_id`` matches a ``User.slack_id``.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from roo.models import PointsAdmin

User = get_user_model()


class Command(BaseCommand):
    help = "Link PointsAdmin rows to User accounts by matching Slack ID."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )
        parser.add_argument(
            "--include-inactive",
            action="store_true",
            help="Also link PointsAdmin rows where is_active=False.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        qs = PointsAdmin.objects.filter(user__isnull=True)
        if not options["include_inactive"]:
            qs = qs.filter(is_active=True)

        linked = 0
        skipped_no_slack = 0
        skipped_no_user = 0

        for admin in qs.iterator():
            slack_id = (admin.slack_user_id or "").strip()
            if not slack_id:
                skipped_no_slack += 1
                continue

            user = User.objects.filter(slack_id=slack_id).first()
            if user is None:
                skipped_no_user += 1
                self.stdout.write(
                    f"  no User with slack_id={slack_id} for PointsAdmin "
                    f"#{admin.pk} ({admin.role})"
                )
                continue

            if dry_run:
                self.stdout.write(
                    f"  would link PointsAdmin #{admin.pk} ({admin.role}) -> {user.email}"
                )
            else:
                admin.user = user
                admin.save(update_fields=["user"])
                self.stdout.write(
                    f"  linked PointsAdmin #{admin.pk} ({admin.role}) -> {user.email}"
                )
            linked += 1

        verb = "Would link" if dry_run else "Linked"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {linked} PointsAdmin row(s); "
                f"skipped {skipped_no_slack} without a Slack ID, "
                f"{skipped_no_user} without a matching User."
            )
        )
