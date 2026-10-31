"""Report or freeze every organisation's founder-journey cohort (P1.12).

``--report`` (the default) is read-only. It prints one row per organisation:
its marker, effective cohort, proposed cohort, the reasons that keep it on
today's path, an independent inventory of state a new path could strand, the
state that predates its cohort decision, the free first-article status and
its in-flight runs. The last line is ``legacy_orgs_losing_an_action: <n>``,
counting organisations whose effective cohort is new_journey (and that did not
opt in) while they have history the new journey could strand:

- a named legacy domain, always;
- one created at or after LAUNCHED_AT (unless it has a create marker), when
  it has state recorded before LAUNCHED_AT;
- one created before LAUNCHED_AT (every organisation when it is unset) with
  no create marker, when it has state recorded before its marker's
  ``assignedAt`` (the reasons or inventory when that time is unknown).

State a founder adds on the new journey after the decision never counts, so
the metric stays meaningful after ``--apply`` and after launch. The command
exits 1 when n > 0.

``--apply`` writes a ``report_apply`` marker where none exists: new_journey
only for organisations with no reasons and no inventory, legacy (with the
reasons and ``inventory:<signal>`` entries) for every other one. An
organisation created at or after LAUNCHED_AT is left to the launch window
unless it is a named domain or has state from before LAUNCHED_AT, which
freezes it as legacy (with ``before_launch:<signal>`` entries). An
organisation without a config row that would stay legacy, or that has
keyword research or content islands, gets no row and no marker: it is legacy
through LAUNCHED_AT already, and a new row would enrol it in the island
refresh. It never changes an existing marker and is idempotent. Only run it
in production after a clean report.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from content_factory import founder_journey_cohort as cohorts
from content_factory.run_state import ACTIVE_RUN_STATUSES, ARTICLE_WORKFLOWS
from organizations.models import Organization

METRIC = "legacy_orgs_losing_an_action"
_IN_FLIGHT_STATUSES = frozenset(ACTIVE_RUN_STATUSES) | {
    "awaiting_approval", "approval_required", "awaiting_confirmation", "awaiting_delivery_mode",
    "preview_building", "preview_verifying", "repair_preview_building"}


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


def prior_state(organization, cutoff) -> list[str]:
    """Timestamped state recorded at or before ``cutoff``, plus ``named_org`` for a named domain.

    It reads only rows with a creation time (connections, hosted sites,
    articles, article and setup runs, website operations, notification
    channels, paid jobs), so it separates history from state a founder added
    on the new journey after the cohort was decided.
    """
    from content_factory.hosted_article_models import HostedArticleSite
    from content_factory.models import ContentFactoryJob, NotificationChannel, WrittenArticle
    from content_factory.website_models import WebsiteConnection, WebsiteConnectionOperation
    signals = []
    domain = cohorts._domain(organization)
    if domain in cohorts.NAMED_LEGACY_DOMAINS or (domain.startswith("www.") and domain[4:] in cohorts.NAMED_LEGACY_DOMAINS):
        signals.append("named_org")
    before = {"created_at__lte": cutoff}
    checks = (
        ("website_connection", WebsiteConnection.objects.filter(organization_id=organization.pk, **before)),
        ("hosted_site", HostedArticleSite.objects.filter(organization_id=organization.pk, **before)),
        ("written_article", WrittenArticle.objects.filter(organization_id=organization.pk, **before)),
        ("article_run", cohorts._runs(organization).filter(workflow__in=ARTICLE_WORKFLOWS, **before)),
        ("setup_run", cohorts._runs(organization).filter(workflow__in=cohorts.SETUP_WORKFLOWS, **before)),
        ("website_operation", WebsiteConnectionOperation.objects.filter(
            connection__organization_id=organization.pk, **before)),
        ("notification_channel", NotificationChannel.objects.filter(organization_id=organization.pk, **before)),
        ("paid_job", ContentFactoryJob.objects.filter(
            domain__iexact=domain, billing_status__in=["charged", "reused"], **before) if domain else None),
    )
    signals.extend(name for name, queryset in checks if queryset is not None and queryset.exists())
    return signals


def _assigned_at(marker):
    value = parse_datetime(str((marker or {}).get("assignedAt") or ""))
    return None if value is None or timezone.is_naive(value) else value


def _losing(org, *, marker, valid, effective, reasons, inventory, launch):
    """Whether a new-journey org has history it could lose, and the state that shows it."""
    assigned_by = marker.get("assignedBy") if valid else None
    if effective != cohorts.NEW_JOURNEY or assigned_by == "opt_in":
        return False, []
    post_launch = launch is not None and org.created_at is not None and org.created_at >= launch
    if post_launch:
        cutoff = launch
    elif valid:
        cutoff = _assigned_at(marker)
    else:
        cutoff = None
    if cutoff is None:
        # No decision time to compare with: any history or state counts.
        if "named_org" in reasons:
            return True, ["named_org"]
        return assigned_by != "create" and bool(reasons or inventory), []
    prior = prior_state(org, cutoff)
    if "named_org" in prior:
        return True, prior
    return assigned_by != "create" and bool(prior), prior


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
        counts, prior = _losing(org, marker=marker, valid=valid, effective=effective, reasons=reasons,
                                inventory=inventory, launch=launch)
        losing += int(counts)
        rows.append({
            "id": org.pk,
            "domain": org.domain,
            "markerCohort": marker.get("cohort") if valid else None,
            "assignedBy": marker.get("assignedBy") if valid else None,
            "effectiveCohort": effective,
            "proposedCohort": proposed,
            "reasons": reasons,
            "inventory": inventory,
            "priorState": prior,
            "entitlement": _entitlement(org, config),
            "inFlightRuns": _in_flight(org),
            "losingAnAction": bool(counts),
        })
    return {"organizations": rows, METRIC: losing}


def _has_island_refresh_inputs(organization) -> bool:
    """Keyword research, islands or clusters: what a new config row would hand the island refresh."""
    from content_factory.models import ContentIsland, ResearchedKeyword, SemanticCluster
    return any(model.objects.filter(organization_id=organization.pk).exists()
               for model in (ResearchedKeyword, ContentIsland, SemanticCluster))


def apply_markers(organizations=None):
    """Freeze every unmarked organisation's cohort (see the module docstring). Returns counts."""
    launch = cohorts.launched_at()
    counts = {"new_journey": 0, "legacy": 0, "skipped_existing_marker": 0, "skipped_launch_window": 0,
              "legacy_no_config": 0}
    orgs = list(organizations if organizations is not None else Organization.objects.order_by("pk"))
    for org in orgs:
        post_launch = launch is not None and org.created_at is not None and org.created_at >= launch
        with transaction.atomic():
            config = cohorts._locked_config(org, create=False)
            existing = cohorts._raw_marker(config)
            if cohorts._valid(existing):
                counts["skipped_existing_marker"] += 1
                continue
            reasons = cohorts.legacy_reasons(org, config)
            inventory = cohorts.founder_state_inventory(org, config)
            frozen = [*reasons, *(f"inventory:{signal}" for signal in inventory)]
            if post_launch:
                prior = prior_state(org, launch)
                if not prior:
                    counts["skipped_launch_window"] += 1
                    continue
                cohort_value = cohorts.LEGACY
                frozen += [f"before_launch:{signal}" for signal in prior]
            else:
                cohort_value = cohorts.NEW_JOURNEY if not frozen else cohorts.LEGACY
            if config is None:
                if not post_launch and (cohort_value == cohorts.LEGACY or _has_island_refresh_inputs(org)):
                    counts["legacy_no_config"] += 1
                    continue
                config = cohorts._locked_config(org, create=True)
            marker = cohorts._base_marker(cohort_value, "report_apply",
                                          frozen if cohort_value == cohorts.LEGACY else [])
            cohorts._save_marker(config, {**(existing or {}), **marker})
            counts[cohort_value] += 1
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
