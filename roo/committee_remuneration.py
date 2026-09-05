from datetime import date
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from integrations.services.slack import SlackService

from .models import PointsAdmin
from .services import PointsService


COMMITTEE_REMUNERATION_SOURCE = "MANUAL"
COMMITTEE_REMUNERATION_CREATED_BY = "SYSTEM"


def weekly_points() -> int:
    return int(getattr(settings, "COMMITTEE_REMUNERATION_WEEKLY_POINTS", 40))


def remuneration_timezone() -> ZoneInfo:
    return ZoneInfo(getattr(settings, "COMMITTEE_REMUNERATION_TIMEZONE", "Australia/Melbourne"))


def week_key(on_date: date) -> str:
    iso_year, iso_week, _ = on_date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _display_name(admin: PointsAdmin) -> str:
    user = admin.user
    if user:
        return user.full_name or user.email or admin.slack_user_id
    return admin.slack_user_id


class CommitteeRemunerationService:
    """Weekly Roo points remuneration for active committee members."""

    @staticmethod
    def members():
        return (
            PointsAdmin.objects.filter(role="committee", is_active=True)
            .select_related("user")
            .order_by("slack_user_id")
        )

    @classmethod
    def pay(cls, *, on_date: date = None, dry_run: bool = False) -> dict:
        """
        Pay the weekly points remuneration to every active committee member.

        The idempotency key is scoped to the ISO week, so repeated runs within
        the same week pay nobody twice and a missed run self-heals.
        """
        if on_date is None:
            on_date = timezone.now().astimezone(remuneration_timezone()).date()

        points = weekly_points()
        key = week_key(on_date)
        paid = []
        already_paid = []
        unlinked = []

        for admin in cls.members():
            name = _display_name(admin)
            user = admin.user or PointsService.get_user_by_slack_id(admin.slack_user_id)
            if not user:
                unlinked.append({"slack_user_id": admin.slack_user_id, "name": name})
                continue

            entry = {"slack_user_id": admin.slack_user_id, "name": name}
            if dry_run:
                paid.append(entry)
                continue

            _ledger, created = PointsService.award(
                user=user,
                delta=points,
                source=COMMITTEE_REMUNERATION_SOURCE,
                description=f"Weekly committee points remuneration ({key})",
                created_by_slack_id=COMMITTEE_REMUNERATION_CREATED_BY,
                idempotency_key=f"committee_remuneration:{admin.slack_user_id}:{key}",
            )
            if created:
                paid.append(entry)
            else:
                already_paid.append(entry)

        return {
            "week": key,
            "points": points,
            "paid": paid,
            "already_paid": already_paid,
            "unlinked": unlinked,
            "dry_run": dry_run,
        }


def format_slack_summary(summary: dict) -> str:
    paid = summary["paid"]
    prefix = "[dry run] " if summary["dry_run"] else ""

    lines = [
        f"{prefix}:coin: Committee — I just gave you your weekly "
        f"*{summary['points']} Roo points*. You love me, isn't it? :sunglasses:",
        f"_{summary['week']} · {len(paid)} "
        f"{'member' if len(paid) == 1 else 'members'}_",
    ]
    lines.extend(f"• {member['name']}" for member in paid)

    if summary["already_paid"]:
        names = ", ".join(member["name"] for member in summary["already_paid"])
        lines.append(f"_Already paid this week: {names}_")

    if summary["unlinked"]:
        names = ", ".join(
            f"{member['name']} (`{member['slack_user_id']}`)" for member in summary["unlinked"]
        )
        lines.append(
            f":warning: No linked account, so no points paid: {names}. "
            "Run `link_points_admins_to_users` to fix."
        )

    return "\n".join(lines)


def post_slack_summary(summary: dict, *, channel: str = None) -> tuple[bool, str]:
    channel = channel or getattr(settings, "COMMITTEE_REMUNERATION_SLACK_CHANNEL", "")
    if not channel:
        return False, ""
    posted, message_ts = SlackService.send_message(
        channel_id=channel,
        text=format_slack_summary(summary),
    )
    return posted, message_ts or ""
