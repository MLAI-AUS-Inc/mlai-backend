"""Seed keywords for organisations that have none (plan E9).

Topic research needs seed keywords or competitors. The first-profile path fills
seeds for new-journey orgs only while ``MY_STARTUP_ONE_FIELD_START_ENABLED`` is
on, so older and legacy orgs can have neither. This module plans and applies a
one-off backfill with ``startup_first_profile.fallback_seed_keywords``, from the
org's saved name and its own description, audience and company context.

It derives nothing from the website host alone: a name that is just the domain
(the default for a new organisation) does not count. An org with nothing to
derive from is reported and left alone, so its next research run keeps the
founder ask ``add_seed_topics``. Content Factory applies the same rule when a
run starts with both lists empty.

Only empty ``seed_keywords`` are written. Nothing here writes a
ContentFactoryRun, sends a message or calls Content Factory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db import transaction

from content_factory.startup_first_profile import _clean, _seed_list, fallback_seed_keywords

_HOSTNAME = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_CONTEXT_CHARS = 1500


@dataclass
class SeedPlan:
    """What the backfill would do for one organisation."""

    organization_id: int
    domain: str
    outcome: str  # "fill", "has_seeds" or "nothing_to_derive"
    seeds: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def real_name(name, domain) -> str:
    """The saved name, unless it is only the website host."""
    name = _clean(name, 255)
    host = _clean(domain, 255).lower()
    if not name or name.lower() == host or _HOSTNAME.fullmatch(name.lower()):
        return ""
    return name


def _inputs(organization):
    from content_factory.models import OrganizationContentConfig
    from startup_updates.models import StartupProfile

    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    profile = StartupProfile.objects.filter(organization=organization).first()
    name = real_name(getattr(config, "brand_name", ""), organization.domain) or real_name(organization.name, organization.domain)
    description = _clean(getattr(profile, "short_description", ""), 500)
    context = _clean(getattr(config, "company_context", ""), _CONTEXT_CHARS)
    audience = _clean(getattr(profile, "target_audience", ""), 500) or _clean(getattr(profile, "problem_solved", ""), 500)
    sources = [label for label, value in (("name", name), ("short_description", description),
                                          ("company_context", context), ("target_audience", audience)) if value]
    return {"name": name, "description": description or context, "audience": audience}, sources


def plan_for(organization) -> SeedPlan:
    """The backfill's decision for one organisation; reads only, never writes."""
    if _seed_list(organization.seed_keywords):
        return SeedPlan(organization.pk, organization.domain, "has_seeds")
    inputs, sources = _inputs(organization)
    # No domain: a seed list made from the host alone is not worth researching.
    seeds = fallback_seed_keywords(**inputs, domain="") if sources else []
    if not seeds:
        return SeedPlan(organization.pk, organization.domain, "nothing_to_derive", sources=sources)
    return SeedPlan(organization.pk, organization.domain, "fill", seeds=seeds, sources=sources)


def apply_plan(plan: SeedPlan) -> bool:
    """Write a ``fill`` plan if the org still has no seeds. True when it wrote."""
    from organizations.models import Organization
    from startup_updates.models import StartupProfile

    if plan.outcome != "fill" or not plan.seeds:
        return False
    with transaction.atomic():
        organization = Organization.objects.select_for_update().filter(pk=plan.organization_id).first()
        if organization is None or _seed_list(organization.seed_keywords):
            return False
        organization.seed_keywords = list(plan.seeds)
        organization.save(update_fields=["seed_keywords"])
        # Mirror the first-profile path, but never create a profile row here.
        profile = StartupProfile.objects.select_for_update().filter(organization=organization).first()
        if profile is not None and not profile.positive_keywords:
            profile.positive_keywords = list(plan.seeds)
            profile.save(update_fields=["positive_keywords", "updated_at"])
    return True
