"""Optional, monotonic article-improvement progress; never drafting authority."""
from __future__ import annotations

import importlib

from django.conf import settings
from django.db import transaction

from .models import OrganizationContentConfig, WebsiteBaselineSnapshot
from .p2_foundation import p2_applies

STEPS = ("voice", "founders", "audience", "website_check")
KEY = "article_improvements_v1"


def _mapping(value):
    return value if isinstance(value, dict) else {}


def has_first_draft(organization):
    """Use actual delivery history; missing telemetry never invents a draft."""
    try:
        helper = getattr(importlib.import_module("content_factory.founder_funnel"), "has_first_draft")
    except (ImportError, AttributeError):
        return False
    try:
        return bool(helper(organization))
    except Exception:
        return False


def _current_steps(config):
    from .article_voice import article_voice_payload
    from .editorial_catalog import catalog_payload
    result = set()
    if article_voice_payload(config.pillar_strategy):result.add("voice")
    if any(isinstance(row, dict) and str(row.get("name") or "").strip() for row in config.authors or []):result.add("founders")
    try:
        catalog = catalog_payload(config.pillar_strategy)
        if any(row.get("status") == "approved" for row in catalog["audience_options"]):result.add("audience")
    except (ValueError, KeyError, TypeError):
        pass
    if WebsiteBaselineSnapshot.objects.filter(organization_id=config.organization_id, status="completed").exists():result.add("website_check")
    return result


def summary(organization, *, user=None):
    """Cache only private earned progress, preserving all public settings and clocks.

    Eligibility is based on real first-draft evidence and stays earned after a
    draft is deleted. Completion is a record of having tried an optional tool;
    removing an author or changing preferences cannot turn it into a gate.
    """
    if not getattr(settings, "MY_STARTUP_ARTICLE_IMPROVEMENTS_ENABLED", False) or not p2_applies(organization):
        return None
    delivered = has_first_draft(organization)
    with transaction.atomic():
        config = OrganizationContentConfig.objects.select_for_update().filter(organization=organization).first()
        if config is None:return {"available": False, "completed": [], "canEdit": False}
        strategy = _mapping(config.pillar_strategy)
        heal = _mapping(strategy.get("self_heal"))
        prior = _mapping(heal.get(KEY))
        available = prior.get("available") is True or delivered
        completed = set(prior.get("completed") or []) & set(STEPS) if isinstance(prior.get("completed"), list) else set()
        if available:completed |= _current_steps(config)
        receipt = {"version": 1, "available": available, "completed": [step for step in STEPS if step in completed]}
        if available and receipt != prior:
            OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy={**strategy, "self_heal": {**heal, KEY: receipt}})
    from founder_tools.services import user_may_use_organization
    can_edit = bool(user and user.is_active and user_may_use_organization(user, organization))
    if can_edit and getattr(settings, "MY_STARTUP_TEAM_ENABLED", False):
        try:
            from founder_tools.startup_team import member_may_publish
            can_edit = bool(member_may_publish(user, organization))
        except (ImportError, AttributeError):
            pass
    return {"available": available, "completed": receipt["completed"], "canEdit": can_edit}
