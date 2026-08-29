import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from roo.models import PointsAdmin


User = get_user_model()

WEEKLY_ALLOWANCE = 100

# The 2026 committee, as supplied by the President. This roster is exclusive:
# anyone not on it loses admin access. Email is the match key, because Slack
# display names in the DB are inconsistent (first-name-only rows, nicknames)
# and matching on names silently mismatches people.
COMMITTEE = (
    ("Dr Sam Donegan", "sam@mlai.au"),
    ("Alisa Belova", "belova.alisa@gmail.com"),
    ("Sonia Kaurah", "sonia@talathrive.com"),
    ("Ryan Mouritz", "ryanmouritz@outlook.com"),
    ("Pegah Khaleghi", "pegah@bookiewand.ai"),
    ("Anjali Singh Gaharwar", "singhanjalig09@gmail.com"),
    ("Yana Lin", "yanalincoaching@gmail.com"),
    ("Daniel Malkinson", "danielmalkinson@gmail.com"),
    ("Jun Kai Chang", "jkchangworks@gmail.com"),
    ("Juan David Bernal P.", "mesieou@gmail.com"),
    ("Alan Philip", "alanphilip1000@gmail.com"),
    ("Callum Holt", "callumpholt@gmail.com"),
    ("Dr Anurag Ganugapati", "anu@statdoctor.net"),
    ("Shan Yang", "samyang102238188@gmail.com"),
    ("Kaey-Lib Tan", "kaeylib@gmail.com"),
)


class Command(BaseCommand):
    help = "Make the PointsAdmin table match the 2026 committee roster exactly."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", default=False)

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        created = []
        updated = []
        unchanged = []
        blocked = []
        roster_ids = set()

        for name, email in COMMITTEE:
            user = User.objects.filter(email__iexact=email).first()
            if not user:
                blocked.append({"name": name, "email": email, "reason": "no user account"})
                continue
            if not user.slack_id:
                blocked.append({"name": name, "email": email, "reason": "user has no slack_id"})
                continue

            roster_ids.add(user.slack_id)
            entry = {"name": name, "slack_user_id": user.slack_id}
            admin = PointsAdmin.objects.filter(slack_user_id=user.slack_id).first()

            if not admin:
                created.append(entry)
                if apply_changes:
                    PointsAdmin.objects.create(
                        slack_user_id=user.slack_id,
                        user=user,
                        role="committee",
                        is_active=True,
                        weekly_allowance=WEEKLY_ALLOWANCE,
                    )
                continue

            drift = {}
            if admin.role != "committee":
                drift["role"] = admin.role
            if not admin.is_active:
                drift["is_active"] = admin.is_active
            if admin.weekly_allowance != WEEKLY_ALLOWANCE:
                drift["weekly_allowance"] = admin.weekly_allowance
            if admin.user_id != user.id:
                drift["user_id"] = admin.user_id

            if not drift:
                unchanged.append(entry)
                continue

            entry["was"] = drift
            updated.append(entry)
            if apply_changes:
                admin.role = "committee"
                admin.is_active = True
                admin.weekly_allowance = WEEKLY_ALLOWANCE
                admin.user = user
                admin.save(
                    update_fields=["role", "is_active", "weekly_allowance", "user"]
                )

        # The roster is exclusive, so any other active admin loses access.
        # Deactivating rather than deleting keeps their ledger history intact.
        stale = PointsAdmin.objects.filter(is_active=True).exclude(
            slack_user_id__in=roster_ids
        )
        deactivated = [
            {"slack_user_id": admin.slack_user_id, "name": str(admin), "role": admin.role}
            for admin in stale
        ]
        if apply_changes:
            stale.update(is_active=False)

        self.stdout.write(
            json.dumps(
                {
                    "applied": apply_changes,
                    "weekly_allowance": WEEKLY_ALLOWANCE,
                    "created": created,
                    "updated": updated,
                    "unchanged": unchanged,
                    "deactivated": deactivated,
                    "blocked": blocked,
                },
                indent=2,
            )
        )
