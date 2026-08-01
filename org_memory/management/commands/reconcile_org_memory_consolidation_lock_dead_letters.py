import json

from django.core.management.base import BaseCommand, CommandError

from organizations.models import Organization
from org_memory.models import OrganizationMembership
from org_memory.runtime import reconcile_consolidation_lock_dead_letters


class Command(BaseCommand):
    help = (
        "Preview or reconcile consolidation work affected by the nullable "
        "outer-join row-lock bug."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-domain", required=True)
        parser.add_argument("--provider", required=True)
        parser.add_argument("--operator-email")
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(
            domain__iexact=options["organization_domain"]
        ).first()
        if organization is None:
            raise CommandError("Organization does not exist.")
        limit = int(options["limit"])
        if not 1 <= limit <= 10000:
            raise CommandError("--limit must be between 1 and 10000.")
        operator = None
        if options["apply"]:
            operator_email = str(options.get("operator_email") or "").strip()
            if not operator_email:
                raise CommandError("--operator-email is required with --apply.")
            membership = OrganizationMembership.objects.select_related("user").filter(
                organization=organization,
                user__email__iexact=operator_email,
            ).first()
            if membership is None:
                raise CommandError("Operator is not a member of the organization.")
            operator = membership.user
        try:
            report = reconcile_consolidation_lock_dead_letters(
                organization=organization,
                provider=options["provider"],
                apply=options["apply"],
                resolved_by=operator,
                limit=limit,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, sort_keys=True))
