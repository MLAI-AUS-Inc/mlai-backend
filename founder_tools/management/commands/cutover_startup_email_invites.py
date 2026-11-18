"""Replace one startup's shareable links with recipient-bound invitations."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from founder_tools import membership, startup_team as team
from founder_tools.models import VibeRaisingCompany


class Command(BaseCommand):
    help = "Inspect or enable email invitation mode for one saved startup."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, **options):
        company = VibeRaisingCompany.objects.select_related("organization").filter(pk=options["company"]).first()
        if not company or not company.organization_id or company.closed_at:
            raise CommandError("Choose an open startup with an organization.")
        organization = company.organization
        with team._locked(organization, create=options["apply"]) as (config, roster):
            company.refresh_from_db()
            if company.closed_at or company.organization_id != organization.pk:
                raise CommandError("Choose an open startup with an organization.")
            canonical = membership.shared_workspace(organization)
            if canonical:
                valid = canonical.pk == company.pk
            else:
                valid = bool(team.enabled() and team._owner(organization)
                             and (not roster or (roster.get("company") or {}).get("id") == str(company.pk))
                             and VibeRaisingCompany.objects.filter(organization=organization).count() == 1)
            if not valid or (roster or {}).get("frozen"):
                raise CommandError("The selected startup needs an ownership review.")
            if not options["apply"]:
                return "Dry run. Email invitations replace the startup's anonymous links at cutover."
            if roster:
                roster["invite"] = None
                team._save(config, roster)
            if not company.email_invites_enabled:
                company.email_invites_enabled = True
                company.save(update_fields=["email_invites_enabled"])
                membership.record_event(company, "email_invites_enabled")
            return "Email invitation mode enabled."
