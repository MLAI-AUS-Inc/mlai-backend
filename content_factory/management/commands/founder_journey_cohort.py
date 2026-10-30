"""Report or freeze every organisation's founder-journey cohort (P1.12).

``--report`` (the default) is read-only. It prints one row per organisation:
its marker, effective cohort, proposed cohort, the reasons that keep it on
today's path, an independent inventory of state a new path could strand, the
free first-article status and its in-flight runs. The last line is
``legacy_orgs_losing_an_action: <n>``: organisations created before launch,
not opted in or created on the new journey, whose effective or proposed
cohort is new_journey while they have history or state. The command exits 1
when n > 0.

``--apply`` writes a ``report_apply`` marker where none exists: new_journey
only for organisations with no reasons and no inventory, legacy (with the
reasons and ``inventory:<signal>`` entries) for every other one. It never
changes an existing marker and never marks an organisation created at or
after LAUNCHED_AT (those are new-journey through the launch window). It is
idempotent. Only run it in production after a clean report.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content_factory import founder_journey_cohort as cohorts
from content_factory.run_state import ACTIVE_RUN_STATUSES
from organizations.models import Organization

METRIC = "legacy_orgs_losing_an_action"
_IN_FLIGHT_STATUSES = frozenset(ACTIVE_RUN_STATUSES) | {
    "awaiting_approval", "approval_required", "awaiting_confirmation", "awaiting_delivery_mode",
    "preview_building", "preview_verifying", "repair_preview_building"}
_PROTECTED_ASSIGNMENTS = frozenset({"create", "opt_in"})


def _configs(org_ids):
    from content_factory.models import OrganizationContentConfig
    return {config.organization_id: config for config in OrganizationContentConfig.objects.filter(organization_id__in=org_ids)}


def _entitlement(organization, config):
    from content_factory.first_article_entitlement import entitlement_state
    from founder_tools.services import organization_owner_user_id
    from django.contrib.auth import get_user_model
    try:
        owner_id = organization_owner_user_id(organization)
        owner = get_user_model().objects.filter(pk=owner_id).first() if owner_id else None
        state = entitlement_state(organization, owner, config=config)
    except Exception as exc:  # noqa: BLE001 - the report must cover every org
        return f"unknown:{type(exc).__name__}"
    return state["status"] + (f":{state['limit']}" if state.get("limit") else "")


def _in_flight(organization):
    runs = cohorts._runs(organization).filter(status__in=_IN_FLIGHT_STATUSES).order_by("-created_at")
    return [f"{run.workflow}:{run.status}:{run.run_id}" for run in runs[:5]]


def report_rows(organizations=None):
    """One report row per organisation, plus the metric, as plain data."""
    launch = cohorts.launched_at()
    orgs = list(organizations if organizations is not None else Organization.objects.order_by("pk"))
    configs = _configs([org.pk for org in orgs])
    rows, losing = [], 0
    for org in orgs:
        config = configs.get(org.pk)
        marker = cohorts.read_marker(org, config) if config is not None else None
        valid = cohorts._valid(marker)
        effective = cohorts.cohort(org, config if config is not None else cohorts._EmptyConfig)
        reasons = cohorts.legacy_reasons(org, config)
        inventory = cohorts.founder_state_inventory(org, config if config is not None else cohorts._EmptyConfig)
        proposed = cohorts.NEW_JOURNEY if not reasons and not inventory else cohorts.LEGACY
        pre_launch = launch is None or org.created_at is None or org.created_at < launch
        protected = valid and marker.get("assignedBy") in _PROTECTED_ASSIGNMENTS
        counts = (pre_launch and not protected and (reasons or inventory)
                  and cohorts.NEW_JOURNEY in (effective, proposed))
        losing += int(bool(counts))
        rows.append({
            "id": org.pk,
            "domain": org.domain,
            "markerCohort": marker.get("cohort") if valid else None,
            "assignedBy": marker.get("assignedBy") if valid else None,
            "effectiveCohort": effective,
            "proposedCohort": proposed,
            "reasons": reasons,
            "inventory": inventory,
            "entitlement": _entitlement(org, config),
            "inFlightRuns": _in_flight(org),
            "losingAnAction": bool(counts),
        })
    return {"organizations": rows, METRIC: losing}


def apply_markers(organizations=None):
    """Freeze every unmarked pre-launch organisation's cohort. Returns counts."""
    launch = cohorts.launched_at()
    counts = {"new_journey": 0, "legacy": 0, "skipped_existing_marker": 0, "skipped_launch_window": 0}
    orgs = list(organizations if organizations is not None else Organization.objects.order_by("pk"))
    for org in orgs:
        if launch is not None and org.created_at is not None and org.created_at >= launch:
            counts["skipped_launch_window"] += 1
            continue
        with transaction.atomic():
            config = cohorts._locked_config(org, create=True)
            existing = cohorts._raw_marker(config)
            if cohorts._valid(existing):
                counts["skipped_existing_marker"] += 1
                continue
            reasons = cohorts.legacy_reasons(org, config)
            inventory = cohorts.founder_state_inventory(org, config)
            if not reasons and not inventory:
                marker = cohorts._base_marker(cohorts.NEW_JOURNEY, "report_apply", [])
            else:
                marker = cohorts._base_marker(cohorts.LEGACY, "report_apply",
                                              [*reasons, *(f"inventory:{signal}" for signal in inventory)])
            cohorts._save_marker(config, {**(existing or {}), **marker})
            counts[marker["cohort"]] += 1
    return counts


class Command(BaseCommand):
    help = "Report (read-only, default) or apply (--apply) every organisation's founder-journey cohort."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--report", action="store_true", help="Read-only report (default).")
        mode.add_argument("--apply", action="store_true",
                          help="Write report_apply markers where none exists. Never changes an existing marker.")
        parser.add_argument("--json", action="store_true", help="Print the report or the apply counts as JSON.")

    def handle(self, *args, **options):
        if options.get("apply"):
            counts = apply_markers()
            if options.get("json"):
                self.stdout.write(json.dumps(counts, sort_keys=True))
            else:
                self.stdout.write(" ".join(f"{key}={value}" for key, value in counts.items()))
            return
        report = report_rows()
        if options.get("json"):
            self.stdout.write(json.dumps(report, sort_keys=True))
        else:
            for row in report["organizations"]:
                self.stdout.write(
                    f"org={row['id']} domain={row['domain']} marker={row['markerCohort'] or '-'}"
                    f"/{row['assignedBy'] or '-'} effective={row['effectiveCohort']} proposed={row['proposedCohort']}"
                    f" reasons={','.join(row['reasons']) or '-'} inventory={','.join(row['inventory']) or '-'}"
                    f" entitlement={row['entitlement']} in_flight={','.join(row['inFlightRuns']) or '-'}"
                    + (" LOSING_AN_ACTION" if row["losingAnAction"] else ""))
            self.stdout.write(f"{METRIC}: {report[METRIC]}")
        if report[METRIC] > 0:
            raise CommandError(f"{METRIC}: {report[METRIC]}", returncode=1)
