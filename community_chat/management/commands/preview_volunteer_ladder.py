"""Read-only preview: each member's level today and on the 200-level ladder.

Run it before the ladder switch and attach the output to the PR. It writes
nothing: on PostgreSQL the whole run is a READ ONLY transaction, and it never
creates the per-member Volunteer record that a first visit would create.
Rows carry internal account ids only, never names or emails.
"""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q

from community_chat.models import CommunityChatDevice
from community_chat.volunteer.access import community_id
from community_chat.volunteer.ladder import preview
from community_chat.volunteer.models import VolunteerMemberState
from community_chat.volunteer.policy import progress
from community_chat.volunteer.services import contribution_total
from roo.models import Ledger, PointsAccount


def read_only_total(user):
    """The member's total today, or None while their history is being checked."""
    state = VolunteerMemberState.objects.filter(
        community=community_id(), user=user
    ).first()
    if state is not None:
        return contribution_total(user, state)
    account = PointsAccount.objects.filter(user=user).first()
    has_earnings = (
        Ledger.objects.filter(user=user)
        .exclude(source="purchased_topup")
        .filter(Q(kind="EARN") | Q(kind__isnull=True, delta__gt=0))
        .exists()
    )
    if has_earnings or (
        account and (account.lifetime_earned or account.lifetime_earned_microroo)
    ):
        return None
    return 0


class Command(BaseCommand):
    help = "Read-only: compare members' levels today with the 200-level ladder."

    def add_arguments(self, parser):
        parser.add_argument(
            "--rows",
            action="store_true",
            help="Include one row per member (internal account id only).",
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            member_ids = CommunityChatDevice.objects.filter(
                status="verified"
            ).values("user_id")
            users = (
                get_user_model()
                .objects.filter(is_active=True, pk__in=member_ids)
                .order_by("pk")
                .iterator()
            )
            report = preview(
                ((user.pk, read_only_total(user)) for user in users),
                lambda total: progress(total)["current_level"]["level"],
            )
            transaction.set_rollback(True)
        report["community_id"] = community_id()
        if not options["rows"]:
            report.pop("rows")
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
