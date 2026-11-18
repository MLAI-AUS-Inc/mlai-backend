"""Inspect and explicitly adopt one existing startup roster."""
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import User
from founder_tools import membership, startup_email_invitations, startup_team
from founder_tools.models import VibeRaisingCompany
from founder_tools.profile_fields import is_research_workspace
from founder_tools.services import organization_owner_user_id


class Command(BaseCommand):
    help = "Dry-run or adopt shared access for one saved, unambiguous startup UUID."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, type=UUID)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        company = VibeRaisingCompany.objects.select_related("organization", "profile__user").filter(pk=options["company"]).first()
        if not company or not company.organization_id:
            raise CommandError("Choose a saved startup with an organization.")
        with startup_team._locked(company.organization) as (config, roster):
            company.refresh_from_db()
            if company.membership_policy == VibeRaisingCompany.SHARED_ACCESS:
                return "Already migrated."
            strategy = startup_team.mapping(config.pillar_strategy if config else None)
            if (is_research_workspace(company)
                    or VibeRaisingCompany.objects.filter(organization=company.organization).exclude(pk=company.pk).exists()
                    or not company.profile_id or not company.profile.user.is_active
                    or organization_owner_user_id(company.organization) != company.profile.user_id
                    or (startup_team.KEY in strategy and not roster)
                    or (roster and (roster.get("frozen")
                        or startup_team.mapping(roster.get("company")).get("id") != str(company.pk)))):
                raise CommandError("Workspace or roster requires review.")
            members = (roster or {}).get("members", [])
            if not isinstance(members, list) or any(not isinstance(row, dict) for row in members):
                raise CommandError("Roster is malformed.")
            rows = [row for row in members if row.get("state") == "active"]
            confirmed_ids = {row.get("user_id") for row in rows if type(row.get("user_id")) is int}
            users = User.objects.in_bulk(confirmed_ids)
            if (len(confirmed_ids) != len(rows) or set(users) != confirmed_ids
                    or any(row.get("via") not in {"owner_approval", "invite_link", "email_invite"} for row in rows)):
                raise CommandError("Confirmed roster could not be reconciled.")
            try:
                confirmed = [(row, UUID(row["memberId"])) for row in rows if users[row["user_id"]].is_active]
            except (ValueError, TypeError, KeyError) as exc:
                raise CommandError("Confirmed member IDs could not be reconciled.") from exc
            requests = (roster or {}).get("requests", [])
            if not isinstance(requests, list) or any(not isinstance(row, dict) for row in requests):
                raise CommandError("Pending request history is malformed.")
            pending = sum(row.get("state") == "pending" for row in requests)
            self.stdout.write(f"Confirmed teammates: {len(confirmed)}. Pending requests: {pending}.")
            if not options["apply"]:
                return "Dry run. Confirmed teammates will receive full access at cutover."
            membership.admit(company, company.profile.user, source="legacy_creator")
            for row, member_id in confirmed:
                membership.admit(company, users[row["user_id"]], source="legacy_roster", member_id=member_id,
                                 now=startup_team.stamp(row.get("joined_at")))
            from community_chat.startups.source_preferences import PREFERENCE_KEY
            from startup_updates.models import StartupProfile

            startup_profile = StartupProfile.objects.select_for_update().filter(organization=company.organization).first()
            if startup_profile:
                progress = dict(startup_profile.progress_configuration or {})
                choices = dict(progress.get(PREFERENCE_KEY) or {})
                legacy_key = f"{company.pk}:{company.profile.user_id}"
                if legacy_key in choices:
                    choices.setdefault(str(company.pk), choices[legacy_key])
                    progress[PREFERENCE_KEY] = choices
                    startup_profile.progress_configuration = progress
                    startup_profile.save(update_fields=["progress_configuration", "updated_at"])
            if roster:
                roster["invite"] = None
                startup_team._save(config, roster)
            startup_email_invitations.revoke_open(company.organization, timezone.now())
            company.membership_policy = VibeRaisingCompany.SHARED_ACCESS
            company.save(update_fields=["membership_policy"])
            membership.record_event(company, "policy_changed")
            return "Shared access enabled."
