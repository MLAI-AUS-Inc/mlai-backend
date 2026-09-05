"""Read-only pre-rollout classification and milestone-liability report."""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q, Sum

from community_chat.volunteer.access import community_id
from community_chat.models import CommunityChatDevice
from community_chat.volunteer.models import VolunteerMemberState
from community_chat.volunteer.backfill import bonus_liability
from community_chat.volunteer.policy import levels, microroo, progress, roo
from community_chat.volunteer.services import contribution_total
from roo.models import Ledger, PointsAccount


class Command(BaseCommand):
    help = "Read-only Volunteer history classification and unpaid bonus liability."

    def handle(self, *args, **options):
        report = dict(
            community_id=community_id(),
            members=0,
            unreconciled=0,
            unknown_members=0,
            levels={str(row["level"]): 0 for row in levels()},
            unpaid_bonus_liability_roo="0",
            historical_potential_bonus_liability_roo="0",
            prospective_pending_bonus_liability_roo="0",
            ledger_sources=[],
        )
        liability = historical_liability = 0
        member_ids = CommunityChatDevice.objects.filter(status="verified").values(
            "user_id"
        )
        for user in (
            get_user_model()
            .objects.filter(is_active=True, pk__in=member_ids)
            .iterator()
        ):
            report["members"] += 1
            state = VolunteerMemberState.objects.filter(
                community=community_id(), user=user
            ).first()
            if state is not None:
                total = contribution_total(user, state)
            else:
                account = PointsAccount.objects.filter(user=user).first()
                has_earnings = (
                    Ledger.objects.filter(user=user)
                    .exclude(source="purchased_topup")
                    .filter(Q(kind="EARN") | Q(kind__isnull=True, delta__gt=0))
                    .exists()
                )
                total = (
                    None
                    if has_earnings
                    or account
                    and (account.lifetime_earned or account.lifetime_earned_microroo)
                    else 0
                )
            if total is None:
                report["unreconciled"] += 1
                report["unknown_members"] += 1
                continue
            current = progress(total)["current_level"]["level"]
            report["levels"][str(current)] += 1
            amounts = bonus_liability(user, state, total)
            liability += microroo(amounts["prospective_pending_roo"])
            historical_liability += microroo(amounts["historical_potential_roo"])
        report["unpaid_bonus_liability_roo"] = roo(liability)
        report["prospective_pending_bonus_liability_roo"] = roo(liability)
        report["historical_potential_bonus_liability_roo"] = roo(historical_liability)
        report["ledger_sources"] = [
            dict(
                source=row["source"], kind=row["kind"], microroo=str(row["total"] or 0)
            )
            for row in Ledger.objects.filter(user_id__in=member_ids)
            .values("source", "kind")
            .annotate(total=Sum("delta_microroo"))
            .order_by("source", "kind")
        ]
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
