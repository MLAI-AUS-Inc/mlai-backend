"""Current organization-only authority for one explicitly admitted nightly service."""
from django.db import transaction
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .run_ownership import BUDGET_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY, current_execution_ticket, owner_receipt, user_stop_intent

ORIGINAL_FIELDS = frozenset({"domain", "client_request_id", "request_source", "include_expansion",
    "island_refresh_recovery_version", BUDGET_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY})


def _paused(organization, *, now):
    """Read existing research policy without changing configuration or clocks."""
    from .models import AutomationRun, NotificationDelivery, ResearchAutomation
    from integrations.services.daily_research_policy import MEMORY_KEY, zone
    automations = list(ResearchAutomation.objects.filter(organization=organization).order_by("created_at"))
    if not automations:
        return False
    boundaries = []
    for automation in automations:
        metadata = automation.metadata
        if not isinstance(metadata, dict):
            return True
        memory = metadata.get(MEMORY_KEY, {})
        if not isinstance(memory, dict) or automation.status != "active" or memory.get("pause_reason"):
            return True
        boundaries.append(automation.created_at)
        if memory.get("last_engaged_at"):
            try:
                stamp = parse_datetime(str(memory["last_engaged_at"]))
            except (ValueError, TypeError, OverflowError):
                return True
            if stamp is None or timezone.is_naive(stamp):
                return True
            boundaries.append(stamp)
    if not any((automation.metadata.get(MEMORY_KEY) or {}).get("last_engaged_at") for automation in automations):
        selection = AutomationRun.objects.filter(automation__organization=organization).exclude(selected_topic={}).order_by("-updated_at").first()
        if selection is not None:
            boundaries.append(selection.updated_at)
    local_zone = zone(automations[0].timezone)
    # Count delivered local days, not fan-out attempts. Return at most 3 rows.
    days = list(NotificationDelivery.objects.filter(automation_run__automation__organization=organization,
        event_type="topic_selection", status="sent", delivered_at__gt=max(boundaries),
        automation_run__slot_index__lt=100).annotate(local_day=TruncDate("delivered_at", tzinfo=local_zone)).filter(
        local_day__lt=now.astimezone(local_zone).date()).order_by("local_day").values_list("local_day", flat=True).distinct()[:3])
    return len(days) >= 3


def require_nightly_principal(run):
    """Require the exact original service scope and fresh current eligibility.

    This grants no founder identity, repository credentials, article debit or
    publication permission. Admission flags do not revoke an existing receipt.
    A missing original version/budget remains held instead of being enrolled.
    """
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from founder_tools import startup_team
    from .models import ContentIsland, OrganizationContentConfig, SemanticCluster
    from .run_execution_control import ExecutionControlDenied, require_unsettled_execution, valid_control_budget
    from .run_dispatch_settlement import _original_request_intact
    from .services.island_refresh_domain import validate_refresh_domain
    from .services.island_refresh_scheduler import _seedable, has_researched_keywords, active_workshop_window
    def deny():
        raise ExecutionControlDenied("execution_principal_unavailable")
    with transaction.atomic():
        organization = Organization.objects.select_for_update().filter(pk=run.organization_id).first()
        if organization is None:
            deny()
        current = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk, organization=organization).first()
        config = OrganizationContentConfig.objects.select_for_update().filter(organization=organization).first()
        if current is None or config is None:
            deny()
        receipt, saved = owner_receipt(current), current.run_request
        if (receipt is None or receipt["authority"] != {"backend_facts": True, "event_protocol": 1}
                or receipt["identity"]["principal"] != {"kind": "scheduler", "service": "island_refresh"}
                or current.workflow != "island_refresh" or current.github_repo
                or not isinstance(saved, dict) or set(saved) != ORIGINAL_FIELDS
                or saved.get("request_source") != "mlai_backend_scheduler"
                or type(saved.get("island_refresh_recovery_version")) is not int or saved["island_refresh_recovery_version"] != 1
                or type(saved.get("include_expansion")) is not bool
                or saved.get("domain") != current.domain or organization.domain != current.domain
                or not valid_control_budget(saved.get(BUDGET_KEY)) or saved[BUDGET_KEY]["runtime"] != "article"
                or not _original_request_intact(current) or user_stop_intent(current)):
            deny()
        try:
            if validate_refresh_domain(current.domain) != current.domain:
                deny()
        except ValueError:
            deny()
        ticket = current_execution_ticket(current)
        if (ticket != current_execution_ticket(run) or any(value is not None for value in receipt["identity"]["scope"].values())
                or ticket["scope"] != receipt["identity"]["scope"]):
            deny()
        require_unsettled_execution(current)
        strategy = config.pillar_strategy
        roster = startup_team._roster(config)
        if (not isinstance(strategy, dict) or "previous_owner" in strategy
                or startup_team.KEY in strategy and (roster is None or roster.get("frozen") is not False
                    or startup_team.pinned_owner(organization)[1] is None)):
            deny()
        now = timezone.now()
        if active_workshop_window(now) is not None or _paused(organization, now=now):
            deny()
        islands = {organization.pk} if ContentIsland.objects.filter(organization=organization).exists() else set()
        clusters = {organization.pk} if SemanticCluster.objects.filter(organization=organization).exists() else set()
        if not _seedable(config, island_org_ids=islands, cluster_org_ids=clusters) or not has_researched_keywords(config):
            deny()
