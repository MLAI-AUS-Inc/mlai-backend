"""Retire a company's research without losing run or payment history."""

from hashlib import sha256
import json
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import models
from .p2_foundation import RESEARCH_WORKSPACE_RESET_KEY as RESET_KEY
from .run_ownership import OWNER_RESULT_KEY, owner_receipt
from .vibe_marketing_workflows import DISCOVERY_WORKFLOWS

RESEARCH_WORKFLOWS = DISCOVERY_WORKFLOWS | {"island_refresh"}
GENERATED_STRATEGY_KEYS = frozenset({
    "company_name", "generated_at", "pillars", "rationale", "source_offerings", "source_personas", "version",
})
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked", "denied"}
RESET_DETAIL = "Article research is paused while this workspace is being reset."


class ResearchWorkspaceConflict(ValueError):
    pass


def reset_state(config):
    strategy = getattr(config, "pillar_strategy", None) or {}
    return strategy.get(RESET_KEY) if isinstance(strategy, dict) else None


def research_paused(config):
    state = reset_state(config)
    return bool(state and state["phase"] != "resumed")


def research_config(organization):
    return models.OrganizationContentConfig.objects.filter(organization=organization).only(
        "organization_id", "pillar_strategy",
    ).first()


def research_cutoff(config):
    state = reset_state(config)
    return parse_datetime(state["cutoff"]) if state else None


def _scheduled_runs(organization_id, cutoff):
    island_runs = models.ContentIslandRefreshDispatch.objects.filter(
        organization_id=organization_id, created_at__gte=cutoff,
    ).exclude(content_factory_run_id="")
    daily_runs = models.AutomationRun.objects.filter(
        automation__organization_id=organization_id, created_at__gte=cutoff,
    ).exclude(content_factory_run_id="")
    return Q(run_id__in=island_runs.values("content_factory_run_id")) | Q(
        run_id__in=daily_runs.values("content_factory_run_id"),
    )


def current_research_runs(rows, config):
    """Keep retired results out of current ideas while retaining their audit rows."""
    cutoff = research_cutoff(config)
    if cutoff is None:
        return rows
    current = Q(organization_id=config.organization_id, created_at__gte=cutoff) & (
        Q(**{f"result__{OWNER_RESULT_KEY}__schema_version": 1})
        | _scheduled_runs(config.organization_id, cutoff)
    )
    return rows.filter(~Q(workflow__in=RESEARCH_WORKFLOWS) | current)


def accepts_research_write(config, run_id):
    """Check a service write inside the same organization lock as its mutation."""
    cutoff = research_cutoff(config)
    if cutoff is None:
        return True
    if research_paused(config) or not run_id:
        return False
    rows = ContentFactoryRun.objects.filter(
        organization_id=config.organization_id, run_id=str(run_id), workflow__in=RESEARCH_WORKFLOWS,
        created_at__gte=cutoff,
    ).exclude(status__in={"cancelled", "failed", "blocked", "denied"})
    # A scheduled refresh can report before its run snapshot is mirrored. The
    # scheduler's saved provider ID is still sufficient, and is tenant scoped.
    scheduled = _scheduled_runs(config.organization_id, cutoff)
    if rows.filter(scheduled).exists():
        return True
    run = rows.first()
    if owner_receipt(run) is not None:
        return True
    if ContentFactoryRun.objects.filter(run_id=str(run_id)).exists():
        return False
    return models.ContentIslandRefreshDispatch.objects.filter(
        organization_id=config.organization_id, created_at__gte=cutoff,
        content_factory_run_id=str(run_id), status=models.ContentIslandRefreshDispatchStatus.QUEUED,
    ).exists()


def require_current_research(organization, run):
    config = research_config(organization)
    if not accepts_research_write(config, run.run_id):
        raise ResearchWorkspaceConflict("This research has been retired. Start a new research run.")


def require_research_admission(organization, existing, dispatch_key):
    """Serialize fresh research admission with a workspace reset."""
    config = research_config(organization)
    if research_paused(config):
        raise ResearchWorkspaceConflict(RESET_DETAIL)
    cutoff = research_cutoff(config)
    retired = cutoff and _research_rows(organization).filter(created_at__lt=cutoff).filter(
        Q(run_request__client_request_id=dispatch_key) | Q(client_request_id=dispatch_key) | Q(run_id=dispatch_key),
    ).exists()
    if retired or (cutoff and existing is not None and existing.created_at < cutoff):
        raise ResearchWorkspaceConflict("This research has been retired. Start a new research run.")


def _research_rows(organization):
    return ContentFactoryRun.objects.filter(
        Q(organization=organization) | Q(organization__isnull=True, domain=organization.domain),
        workflow__in=RESEARCH_WORKFLOWS,
    )


def _workspace_tables(organization):
    scope = {"organization": organization}
    return {
        model.__name__: model.objects.filter(**scope)
        for model in (models.ContentIsland, models.ContentIslandRegion, models.SemanticCluster,
                      models.ResearchedKeyword, models.TopicFeedback)
    }


def _verify_related_scope(organization):
    for model, left, right in (
        (models.ContentIslandKeyword, "island__organization", "keyword__organization"),
        (models.ClusterMembership, "cluster__organization", "keyword__organization"),
        (models.ContentIslandEdge, "island_a__organization", "island_b__organization"),
    ):
        own_left, own_right = Q(**{left: organization}), Q(**{right: organization})
        if model.objects.filter(own_left | own_right).exclude(own_left & own_right).exists():
            raise ValueError("A research relation crosses the reviewed organization. Reconcile it before clearing.")
    if models.ContentIsland.objects.filter(region__organization=organization).exclude(organization=organization).exists():
        raise ValueError("An island outside this workspace refers to a reviewed region.")


def _snapshot(organization, config):
    """A digest covers the exact scope, including edits that keep the same row counts."""
    _verify_related_scope(organization)
    data = {name: list(rows.order_by("pk").values()) for name, rows in _workspace_tables(organization).items()}
    related = {
        models.ContentIslandEdge: {"organization": organization},
        models.ContentIslandKeyword: {"island__organization": organization},
        models.ContentIslandSnapshot: {"island__organization": organization},
        models.ClusterMembership: {"cluster__organization": organization},
        models.KeywordVelocity: {"keyword__organization": organization},
        models.AISaturation: {"keyword__organization": organization},
        models.PAQuestion: {"keyword__organization": organization},
    }
    data.update({model.__name__: list(model.objects.filter(**scope).order_by("pk").values())
                 for model, scope in related.items()})
    active = list(_research_rows(organization).exclude(status__in=TERMINAL_STATUSES).order_by("run_id")
                  .values("run_id", "workflow", "status"))
    automations = list(models.ResearchAutomation.objects.filter(organization=organization)
                       .order_by("pk").values("id", "status"))
    pending_slots = list(models.AutomationRun.objects.filter(
        automation__organization=organization,
        status__in={"scheduled", "queued", "topic_selection_sent", "delivery_mode_required"},
    ).order_by("pk").values("id", "status", "content_factory_run_id"))
    pending_refreshes = list(models.ContentIslandRefreshDispatch.objects.filter(
        organization=organization, status=models.ContentIslandRefreshDispatchStatus.QUEUED,
    ).order_by("pk").values("id", "content_factory_run_id"))
    pending_discoveries = list(models.ScheduledDiscoveryDispatch.objects.filter(
        domain=organization.domain, state__in={"scheduled", "queued", "topic_selection_sent"},
    ).order_by("pk").values("id", "state", "content_factory_job_id"))
    strategy = {key: value for key, value in (config.pillar_strategy or {}).items() if key in GENERATED_STRATEGY_KEYS}
    plan = {
        "organization_id": organization.pk, "domain": organization.domain, "reset": reset_state(config),
        "counts": {name: len(rows) for name, rows in data.items()}, "generated_strategy_keys": sorted(strategy),
        "active_runs": active, "automations": automations, "pending_slots": pending_slots,
        "pending_refreshes": pending_refreshes, "pending_discoveries": pending_discoveries,
        "daily_discovery_enabled": config.daily_discovery_enabled,
    }
    reviewed = {**plan, "rows": data, "strategy": strategy}
    plan["digest"] = sha256(json.dumps(reviewed, default=str, sort_keys=True).encode()).hexdigest()
    return plan


@transaction.atomic
def reset_workspace(organization, *, action="plan", reviewed_digest=None):
    """Plan, pause, clear and resume one reviewed organization using existing storage."""
    organization = Organization.objects.select_for_update().get(pk=organization.pk, domain=organization.domain)
    config = models.OrganizationContentConfig.objects.select_for_update().get(organization=organization)
    plan = _snapshot(organization, config)
    if action == "plan":
        return plan
    if not reviewed_digest or reviewed_digest != plan["digest"]:
        raise ValueError("The research reset plan changed. Review a fresh inventory.")
    state = reset_state(config)
    strategy = dict(config.pillar_strategy or {})
    if action == "freeze" and (not state or state["phase"] == "resumed"):
        state = {"id": str(uuid4()), "cutoff": timezone.now().isoformat(), "phase": "frozen"}
        models.ResearchAutomation.objects.filter(
            organization=organization, status=models.ResearchAutomationStatus.ACTIVE,
        ).update(status=models.ResearchAutomationStatus.PAUSED, updated_at=timezone.now())
        models.AutomationRun.objects.filter(
            automation__organization=organization, status=models.AutomationRunStatus.SCHEDULED,
        ).update(status=models.AutomationRunStatus.CANCELLED, updated_at=timezone.now())
        models.ScheduledDiscoveryDispatch.objects.filter(
            domain=organization.domain, state=models.ScheduledDiscoveryDispatchState.SCHEDULED,
        ).update(state=models.ScheduledDiscoveryDispatchState.CANCELLED, updated_at=timezone.now())
    elif action == "clear" and state and state["phase"] == "frozen":
        if any(plan[key] for key in ("active_runs", "pending_slots", "pending_refreshes", "pending_discoveries")):
            raise ValueError("Stop and reconcile the listed research work before clearing its workspace.")
        for rows in _workspace_tables(organization).values():
            rows.delete()
        strategy = {key: value for key, value in strategy.items() if key not in GENERATED_STRATEGY_KEYS}
        state = {**state, "phase": "cleared", "reviewed_digest": reviewed_digest}
    elif action == "resume" and state and state["phase"] == "cleared":
        state = {**state, "phase": "resumed"}
    else:
        raise ValueError("The requested transition is not valid for the current research reset.")
    config.pillar_strategy = {**strategy, RESET_KEY: state}
    config.daily_discovery_enabled = False
    config.save(update_fields=["pillar_strategy", "daily_discovery_enabled", "updated_at"])
    return _snapshot(organization, config)
