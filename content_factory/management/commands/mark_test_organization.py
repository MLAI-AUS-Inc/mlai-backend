"""Mark or unmark an organisation as MLAI test traffic (plan F1). Dry run unless --apply.

A marked organisation is left out of every founder report, alert, staff Slack
notice, self-heal sweep and revenue number (content_factory.founder_test_orgs).
It keeps every guard: spend ceilings, consent, refunds and the fleet's caps.

    manage.py mark_test_organization --organization-id 63 --reason "catalogue test org"
    manage.py mark_test_organization --organization-id 63 --reason "..." --apply
    manage.py mark_test_organization --organization-id 63 --unmark --apply
    manage.py mark_test_organization --list
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from content_factory import founder_test_orgs


class Command(BaseCommand):
    help = "Mark or unmark a test organisation (plan F1). Prints the change and writes nothing without --apply."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", type=int, help="The organisation to mark or unmark.")
        parser.add_argument("--unmark", action="store_true", help="Remove the marker instead of setting it.")
        parser.add_argument("--reason", default="", help="Why this is a test organisation (required to mark).")
        parser.add_argument("--apply", action="store_true", help="Write the change. Without it this is a dry run.")
        parser.add_argument("--list", action="store_true", help="List every test organisation and exit (read-only).")

    def handle(self, *args, **options):
        from organizations.models import Organization
        if options["list"]:
            marked = founder_test_orgs.marked_ids()
            configured = founder_test_orgs.setting_ids()
            rows = [{"organizationId": pk, "name": name, "marker": pk in marked, "setting": pk in configured}
                    for pk, name in Organization.objects.filter(pk__in=sorted(marked | configured))
                    .order_by("pk").values_list("pk", "name")]
            self.stdout.write(json.dumps({"testOrganizations": rows}, sort_keys=True))
            return
        organization_id = options["organization_id"]
        if not organization_id:
            raise CommandError("Pass --organization-id, or --list.")
        organization = Organization.objects.filter(pk=organization_id).first()
        if organization is None:
            raise CommandError(f"Organisation {organization_id} does not exist.")
        marked = not options["unmark"]
        reason = str(options["reason"] or "").strip()
        if marked and not reason:
            raise CommandError("Pass --reason to mark an organisation as a test organisation.")
        current = founder_test_orgs.is_test_org(organization)
        in_setting = organization.pk in founder_test_orgs.setting_ids()
        plan = {"organizationId": organization.pk, "name": organization.name, "currentlyTest": current,
                "inSettingOverride": in_setting, "action": "mark" if marked else "unmark",
                "apply": bool(options["apply"])}
        if not options["apply"]:
            plan["result"] = "dry_run"
            self.stdout.write(json.dumps(plan, sort_keys=True))
            self.stderr.write("Dry run: nothing was changed. Pass --apply to write the marker.")
            return
        outcome = founder_test_orgs.set_marker(organization, marked=marked, reason=reason)
        plan["result"] = "changed" if outcome["changed"] else "unchanged"
        if not marked and in_setting:
            plan["note"] = "FOUNDER_TEST_ORGANIZATION_IDS still lists this organisation; remove it there too."
        self.stdout.write(json.dumps(plan, sort_keys=True))
