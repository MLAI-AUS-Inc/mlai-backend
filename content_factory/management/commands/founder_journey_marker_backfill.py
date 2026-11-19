"""Give every organisation with a startup a founder-journey cohort marker (plan E6, D15).

Dry run by default: it prints what it would write and changes nothing. Pass
``--write`` to write. It is never run against production without the owner's
go-ahead for that run (decision D13).

For an organisation with a startup and no valid marker it writes the cohort
the organisation already has, as :func:`founder_journey_cohort.cohort` decides
it without the per-organisation kill switch (created at or after LAUNCHED_AT
is new_journey, otherwise legacy), with ``assignedBy: backfill``. So it never
moves an organisation between cohorts: it only makes the decision explicit,
so every organisation reaches the one server-owned setup state.

It never changes a valid marker and is idempotent. An organisation without a
config row that has keyword research, islands or clusters is left alone and
reported as ``blocked_island_inputs``: a new config row would enrol it in the
island refresh, which needs its own decision. An organisation created at or
after LAUNCHED_AT that is a named legacy domain or has state from before
LAUNCHED_AT is left alone and reported as ``needs_cohort_decision``: the
cohort report (``founder_journey_cohort``) counts it as losing an action, so
freezing its new_journey cohort here would hide that, and freezing it legacy
would move it.

``--write`` is refused while ``MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT`` is
unset or unparsable: every unmarked organisation is then legacy only until
LAUNCHED_AT is set, and a marker written now would keep it legacy afterwards.

Run ``founder_journey_cohort --apply`` first when the owner wants unmarked
organisations without history moved to the new journey: this backfill only
freezes the cohort each remaining organisation already has.

The last line is ``startups_without_marker: <n>`` after the run (for a dry
run: after the writes it would make). The command exits 1 when n > 0.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content_factory import founder_journey_cohort as cohorts
from content_factory.management.commands.founder_journey_cohort import _has_island_refresh_inputs, prior_state
from organizations.models import Organization

METRIC = "startups_without_marker"


def startup_organizations(organization_ids=None):
    """Organisations with at least one startup (``VibeRaisingCompany``), by id."""
    from founder_tools.models import VibeRaisingCompany
    ids = VibeRaisingCompany.objects.filter(organization__isnull=False)
    if organization_ids:
        ids = ids.filter(organization_id__in=organization_ids)
    ids = set(ids.values_list("organization_id", flat=True))
    return list(Organization.objects.filter(pk__in=ids).order_by("pk"))


def organizations_without_marker(organization_ids=None):
    """Invariant check: ids of organisations with a startup and no valid marker (read-only)."""
    from content_factory.models import OrganizationContentConfig
    orgs = startup_organizations(organization_ids)
    configs = {config.organization_id: config for config in
               OrganizationContentConfig.objects.filter(organization_id__in=[org.pk for org in orgs])}
    return [org.pk for org in orgs if not cohorts._valid(cohorts._raw_marker(configs.get(org.pk)))]


def backfill_cohort(organization) -> tuple[str, list[str]]:
    """The cohort the organisation already has without a marker, and why."""
    if cohorts._created_after_launch(organization):
        return cohorts.NEW_JOURNEY, ["backfill:created_after_launch"]
    return cohorts.LEGACY, ["backfill:before_launch"]


class LaunchUnset(CommandError):
    """``--write`` without a usable LAUNCHED_AT: a marker would freeze a cohort LAUNCHED_AT has not decided yet."""


def backfill(*, write=False, organization_ids=None):
    """Plan or write the markers. Returns ``{"rows": [...], "counts": {...}, METRIC: n}``."""
    launch = cohorts.launched_at()
    if write and launch is None:
        raise LaunchUnset("MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT is unset or unparsable; nothing was written.")
    counts = {"has_marker": 0, "new_journey": 0, "legacy": 0, "created_config": 0, "blocked_island_inputs": 0,
              "needs_cohort_decision": 0}
    rows = []
    remaining = 0
    for org in startup_organizations(organization_ids):
        with transaction.atomic():
            config = cohorts._locked_config(org, create=False) if write else cohorts._config_for(org)
            existing = cohorts._raw_marker(config)
            if cohorts._valid(existing):
                counts["has_marker"] += 1
                continue
            if config is None and _has_island_refresh_inputs(org):
                counts["blocked_island_inputs"] += 1
                remaining += 1
                rows.append({"id": org.pk, "domain": org.domain, "action": "blocked_island_inputs"})
                continue
            cohort_value, reasons = backfill_cohort(org)
            if cohort_value == cohorts.NEW_JOURNEY and prior_state(org, launch):
                # A named domain or pre-launch history: the cohort report's decision, not ours.
                counts["needs_cohort_decision"] += 1
                remaining += 1
                rows.append({"id": org.pk, "domain": org.domain, "action": "needs_cohort_decision"})
                continue
            rows.append({"id": org.pk, "domain": org.domain, "action": "write", "cohort": cohort_value,
                         "createConfig": config is None, "reasons": reasons})
            counts[cohort_value] += 1
            counts["created_config"] += int(config is None)
            if not write:
                continue
            if config is None:
                config = cohorts._locked_config(org, create=True)
                existing = cohorts._raw_marker(config)
            marker = cohorts._base_marker(cohort_value, "backfill", reasons)
            cohorts._save_marker(config, {**(existing or {}), **marker})
    if write:
        remaining = len(organizations_without_marker(organization_ids))
    return {"rows": rows, "counts": counts, METRIC: remaining, "dryRun": not write, "launchUnset": launch is None}


class Command(BaseCommand):
    help = "Dry run (default) or write (--write) a cohort marker for every organisation with a startup."

    def add_arguments(self, parser):
        parser.add_argument("--write", action="store_true",
                            help="Write the markers. Without it nothing changes.")
        parser.add_argument("--organization", type=int, action="append", dest="organizations",
                            help="Limit to this organisation id (repeatable).")
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **options):
        result = backfill(write=bool(options.get("write")), organization_ids=options.get("organizations"))
        if options.get("json"):
            self.stdout.write(json.dumps(result, sort_keys=True))
        else:
            mode = "dry_run" if result["dryRun"] else "write"
            for row in result["rows"]:
                self.stdout.write(f"{mode} org={row['id']} domain={row['domain']} action={row['action']}"
                                  + (f" cohort={row['cohort']} create_config={row['createConfig']}"
                                     if row["action"] == "write" else ""))
            if result.get("launchUnset"):
                self.stdout.write("launch_unset: MY_STARTUP_FOUNDER_JOURNEY_V2_LAUNCHED_AT is not set; --write is refused")
            self.stdout.write(" ".join(f"{key}={value}" for key, value in result["counts"].items()))
            self.stdout.write(f"{METRIC}: {result[METRIC]}")
        if result[METRIC] > 0:
            raise CommandError(f"{METRIC}: {result[METRIC]}", returncode=1)
