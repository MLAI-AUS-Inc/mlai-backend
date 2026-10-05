"""Shared startup fields stored in reserved configuration JSON without schema changes."""
from copy import deepcopy
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from rest_framework.exceptions import ValidationError

PROFILE_KEY = "startup_profile_details"
BRANDING_KEY = "startup_branding"


def validate_profile_fields(data):
    """Validate supplied fields without adding defaults for omitted values."""
    result = dict(data)
    aliases = {"founderProfiles": "founder_profiles", "hasRevenue": "has_revenue", "defaultTimezone": "default_timezone"}
    for key, alias in aliases.items():
        if key not in result and alias in result:
            result[key] = result[alias]
    if "founderProfiles" in result:
        rows = result["founderProfiles"]
        if not isinstance(rows, list) or len(rows) > 50:
            raise ValidationError({"founderProfiles": "Use a list of up to 50 founders."})
        founders = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
                raise ValidationError({"founderProfiles": "Add a name for each founder."})
            name = row["name"].strip()
            link = row.get("linkedinUrl", row.get("linkedInUrl", "")) or ""
            if len(name) > 255 or not isinstance(link, str) or len(link) > 512:
                raise ValidationError({"founderProfiles": "Founder details are too long."})
            link = link.strip()
            if link:
                try:
                    parsed = urlsplit(link)
                    valid = parsed.scheme == "https" and parsed.hostname in {"linkedin.com", "www.linkedin.com"} and not parsed.username and not parsed.password
                except ValueError:
                    valid = False
                if not valid:
                    raise ValidationError({"founderProfiles": "Use a full https://www.linkedin.com/ URL for founder profiles."})
            founders.append({"name": name, "linkedinUrl": link})
        result["founderProfiles"] = founders
        result["founderNames"] = [row["name"] for row in founders]
    if "hasRevenue" in result:
        value = result["hasRevenue"]
        if value not in (None, "", "Yes", "No"):
            raise ValidationError({"hasRevenue": "Choose Yes or No, or leave this blank."})
        result["hasRevenue"] = value or ""
    if "defaultTimezone" in result:
        value = result["defaultTimezone"]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError({"defaultTimezone": "Choose a timezone."})
        try:
            ZoneInfo(value.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError({"defaultTimezone": "Choose a valid IANA timezone."})
        result["defaultTimezone"] = value.strip()
    return result


def profile_details(organization):
    """Return persisted optional profile fields, excluding unrelated configuration."""
    from content_factory.models import OrganizationContentConfig

    if organization is None:
        return {}
    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    strategy = config.pillar_strategy if config and isinstance(config.pillar_strategy, dict) else {}
    value = strategy.get(PROFILE_KEY)
    if not isinstance(value, dict):
        return {}
    return {key: deepcopy(value[key]) for key in ("founderProfiles", "hasRevenue") if key in value}


def save_profile_details(organization, data, *, config=None):
    """Lock and merge supplied optional fields; never replace another JSON namespace."""
    from content_factory.models import OrganizationContentConfig
    from organizations.models import Organization

    changes = {key: deepcopy(data[key]) for key in ("founderProfiles", "hasRevenue") if key in data}
    strategy = getattr(config, "pillar_strategy", None)
    provisional = bool(isinstance(strategy, dict) and
        isinstance(strategy.get(PROFILE_KEY), dict) and strategy[PROFILE_KEY].get("researchDraft"))
    if not changes and not provisional:
        return
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        config, _ = OrganizationContentConfig.objects.select_for_update().get_or_create(organization=organization)
        strategy = deepcopy(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
        current = strategy.get(PROFILE_KEY)
        strategy[PROFILE_KEY] = {**(current if isinstance(current, dict) else {}), **changes}
        strategy[PROFILE_KEY].pop("researchDraft", None)
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])


def visible_companies(queryset):
    """Keep provisional research workspaces out of startup selectors and lists."""
    drafts = queryset.filter(
        organization__content_config__pillar_strategy__startup_profile_details__researchDraft=True
    ).values("pk")
    return queryset.exclude(pk__in=drafts)


def is_research_workspace(company):
    """Identify only this company's explicit unsaved research marker."""
    from content_factory.models import OrganizationContentConfig
    if not company.organization_id:
        return False
    config = OrganizationContentConfig.objects.filter(organization_id=company.organization_id).first()
    strategy = config.pillar_strategy if config and isinstance(config.pillar_strategy, dict) else {}
    profile = strategy.get(PROFILE_KEY)
    return isinstance(profile, dict) and profile.get("researchDraft") is True


def lock_research_profile(profile):
    """Serialize provisional workspace admission within the caller's transaction."""
    from founder_tools.models import VibeRaisingProfile
    return VibeRaisingProfile.objects.select_for_update().get(pk=profile.pk)


def organization_branding(organization):
    """Read explicitly established organization branding, never a random founder logo."""
    from content_factory.models import OrganizationContentConfig

    if organization is None or getattr(organization, "pk", None) is None:
        return {}
    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    strategy = config.pillar_strategy if config and isinstance(config.pillar_strategy, dict) else {}
    branding = strategy.get(BRANDING_KEY)
    if not isinstance(branding, dict) or "avatarUrl" not in branding:
        return {}
    return {"avatarUrl": str(branding.get("avatarUrl") or "")}


def company_avatar_url(company):
    """Prefer explicit canonical branding while preserving a legacy company's logo."""
    branding = organization_branding(company.organization if company.organization_id else None)
    return branding.get("avatarUrl", company.avatar_url or "")


@transaction.atomic
def save_company_branding(company, user, avatar_url):
    """Publish a logo only through the established organization owner's company."""
    from content_factory.models import OrganizationContentConfig
    from organizations.models import Organization
    from .services import DomainOwnershipError, user_may_use_organization

    if company.organization_id:
        organization = Organization.objects.select_for_update().get(pk=company.organization_id)
        if not user_may_use_organization(user, organization):
            raise DomainOwnershipError("Only this startup's organization owner can change its shared logo.")
        config, _ = OrganizationContentConfig.objects.select_for_update().get_or_create(organization=organization)
        strategy = deepcopy(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
        strategy[BRANDING_KEY] = {"avatarUrl": avatar_url or "", "companyId": str(company.pk)}
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])
    company.avatar_url = avatar_url or None
    company.save(update_fields=["avatar_url", "updated_at"])
