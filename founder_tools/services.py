from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.db import transaction

from integrations.utils import normalize_domain
from organizations.models import Organization

from .models import VibeRaisingCompany, VibeRaisingProfile


def normalize_company_domain(domain: str | None) -> str:
    return normalize_domain(domain or "") or ""


class DuplicateCompanyDomainError(ValueError):
    """A profile already has a different company registered on this domain.

    Two companies sharing a domain collapse onto the same Organization (which is
    keyed on a globally-unique domain), so their marketing/raising data would
    silently merge. We block that at the create/update boundary instead.
    """

    def __init__(self, domain: str, existing_company: "VibeRaisingCompany"):
        self.domain = domain
        self.existing_company = existing_company
        super().__init__(
            f"You already have a company registered on the domain '{domain}'. "
            "Switch to that company instead of creating a duplicate."
        )


def find_company_with_domain(profile, domain, *, exclude_company_id=None):
    """Return another company under ``profile`` whose (normalized) domain matches.

    Compares normalized forms so a sibling stored as ``https://www.acme.com`` is
    still detected against ``acme.com``. Returns ``None`` when the domain is blank
    or no conflicting sibling exists.
    """
    normalized = normalize_company_domain(domain)
    if not normalized:
        return None
    for company in profile.companies.all():
        if exclude_company_id is not None and company.id == exclude_company_id:
            continue
        if normalize_company_domain(company.domain) == normalized:
            return company
    return None


def assert_company_domain_available(profile, domain, *, exclude_company_id=None):
    """Raise :class:`DuplicateCompanyDomainError` if ``domain`` is taken by a sibling."""
    existing = find_company_with_domain(
        profile, domain, exclude_company_id=exclude_company_id
    )
    if existing is not None:
        raise DuplicateCompanyDomainError(normalize_company_domain(domain), existing)


def normalize_company_linkedin_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        raise ValueError("Enter a valid LinkedIn company URL.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[0].lower() == "in":
        raise ValueError("Enter a LinkedIn company URL, not a personal profile URL.")
    if len(path_parts) < 2 or path_parts[0].lower() != "company":
        raise ValueError("Enter a valid LinkedIn company URL.")

    slug = path_parts[1].strip()
    if not slug:
        raise ValueError("Enter a valid LinkedIn company URL.")
    return f"https://www.linkedin.com/company/{slug}"


def synthetic_actor_id_for_user(user) -> str:
    return f"mlai_user:{user.id}"


def actor_ids_for_user(user) -> list[str]:
    actor_ids = []
    slack_id = str(getattr(user, "slack_id", "") or "").strip()
    if slack_id:
        actor_ids.append(slack_id)
    actor_ids.append(synthetic_actor_id_for_user(user))
    return list(dict.fromkeys(actor_ids))


def founder_actor_id_for_user(user) -> str:
    return actor_ids_for_user(user)[0]


def _is_synthetic_actor_id(value: str | None) -> bool:
    return str(value or "").strip().startswith("mlai_user:")


def reconcile_user_slack_id_from_email(user) -> bool:
    if getattr(user, "slack_id", None) or not getattr(user, "email", None):
        return False
    slack_backed_user = (
        type(user).objects.filter(email__iexact=user.email)
        .exclude(pk=user.pk)
        .exclude(slack_id__isnull=True)
        .exclude(slack_id="")
        .first()
    )
    if slack_backed_user is None:
        return False
    user.slack_id = slack_backed_user.slack_id
    user.save(update_fields=["slack_id"])
    return True


def string_list_from_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _first_value(data, *keys, default=None):
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return default


def _has_any_key(data, *keys) -> bool:
    return any(key in data for key in keys)


def _submitted_value(data, *keys, default=""):
    for key in keys:
        if key in data:
            return data.get(key)
    return default


def _bool_from_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_organization_kind(value) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"for-profit", "for profit", "profit", "commercial"}:
        return "For-profit"
    if raw in {"not-for-profit", "not for profit", "non-profit", "nonprofit", "nfp"}:
        return "Not-for-profit"
    return ""


@transaction.atomic
def ensure_company_organization(company: VibeRaisingCompany) -> Organization | None:
    normalized_domain = normalize_company_domain(company.domain)
    if not normalized_domain:
        return None

    company_update_fields = []
    if company.domain != normalized_domain:
        company.domain = normalized_domain
        company_update_fields.append("domain")

    organization, created = Organization.objects.get_or_create(
        domain=normalized_domain,
        defaults={"name": company.name},
    )
    if not created and not organization.name:
        organization.name = company.name
        organization.save(update_fields=["name"])

    if company.organization_id != organization.id:
        company.organization = organization
        company_update_fields.append("organization")

    if company_update_fields:
        company.save(update_fields=[*company_update_fields, "updated_at"])

    return organization


@transaction.atomic
def apply_shared_startup_details(*, user, company: VibeRaisingCompany, data: dict) -> Organization | None:
    organization = ensure_company_organization(company)
    if organization is None:
        return None

    brand_name_provided = _has_any_key(data, "brandName", "brand_name")
    company_context_provided = _has_any_key(data, "companyContext", "company_context")
    competitors_provided = "competitors" in data
    seed_keywords_provided = _has_any_key(data, "seedKeywords", "seed_keywords")
    founder_names_provided = _has_any_key(data, "founderNames", "founder_names")
    stage_provided = "stage" in data
    organization_kind_provided = _has_any_key(data, "organizationKind", "organization_kind")
    short_description_provided = _has_any_key(data, "shortDescription", "short_description")
    problem_solved_provided = _has_any_key(data, "problemSolved", "problem_solved")
    target_audience_provided = _has_any_key(data, "targetAudience", "target_audience")
    notes_provided = "notes" in data

    brand_name = str(_submitted_value(data, "brandName", "brand_name", default="") or "").strip()
    company_context = str(_submitted_value(data, "companyContext", "company_context", default="") or "").strip()
    linkedin_url = ""
    linkedin_url_provided = "companyLinkedInUrl" in data or "company_linkedin_url" in data
    if linkedin_url_provided:
        linkedin_url = normalize_company_linkedin_url(
            _submitted_value(data, "companyLinkedInUrl", "company_linkedin_url", default="")
        )
    competitors = string_list_from_value(_submitted_value(data, "competitors", default=[]))
    seed_keywords = string_list_from_value(_submitted_value(data, "seedKeywords", "seed_keywords", default=[]))
    founder_names = string_list_from_value(_submitted_value(data, "founderNames", "founder_names", default=[]))
    stage = str(_submitted_value(data, "stage", default="") or "").strip()
    organization_kind = _normalize_organization_kind(
        _submitted_value(data, "organizationKind", "organization_kind", default="")
    )
    short_description = str(_submitted_value(data, "shortDescription", "short_description", default="") or "").strip()
    problem_solved = str(_submitted_value(data, "problemSolved", "problem_solved", default="") or "").strip()
    target_audience = str(_submitted_value(data, "targetAudience", "target_audience", default="") or "").strip()
    notes = str(_submitted_value(data, "notes", default="") or "").strip()

    organization_update_fields = []
    if brand_name and organization.name != brand_name:
        organization.name = brand_name
        organization_update_fields.append("name")
    elif company.name and not organization.name:
        organization.name = company.name
        organization_update_fields.append("name")
    if competitors_provided and organization.competitors != competitors:
        organization.competitors = competitors
        organization_update_fields.append("competitors")
    if seed_keywords_provided and organization.seed_keywords != seed_keywords:
        organization.seed_keywords = seed_keywords
        organization_update_fields.append("seed_keywords")
    if linkedin_url_provided and organization.company_linkedin_url != linkedin_url:
        organization.company_linkedin_url = linkedin_url
        organization_update_fields.append("company_linkedin_url")
    if organization_update_fields:
        organization.save(update_fields=organization_update_fields)

    from content_factory.models import OrganizationContentConfig
    from startup_updates.services import bind_user_to_startup, resolve_or_create_profile

    config, _created = OrganizationContentConfig.objects.get_or_create(organization=organization)
    config_update_fields = []
    actor_id = founder_actor_id_for_user(user)
    actor_aliases = actor_ids_for_user(user)
    connected_actor_id = str(config.connected_slack_user_id or "").strip()
    if not connected_actor_id:
        config.connected_slack_user_id = actor_id
        config_update_fields.append("connected_slack_user_id")
    elif connected_actor_id not in actor_aliases and _is_synthetic_actor_id(connected_actor_id):
        config.connected_slack_user_id = actor_id
        config_update_fields.append("connected_slack_user_id")
    elif (
        connected_actor_id in actor_aliases
        and connected_actor_id != actor_id
        and not _is_synthetic_actor_id(actor_id)
        and _is_synthetic_actor_id(connected_actor_id)
    ):
        config.connected_slack_user_id = actor_id
        config_update_fields.append("connected_slack_user_id")
    if brand_name_provided and config.brand_name != brand_name:
        config.brand_name = brand_name
        config_update_fields.append("brand_name")
    if company_context_provided and config.company_context != company_context:
        config.company_context = company_context
        config_update_fields.append("company_context")

    github_repo = str(_first_value(data, "githubRepo", "github_repo", default="") or "").strip()
    if github_repo and config.github_repo != github_repo:
        config.github_repo = github_repo
        config_update_fields.append("github_repo")
    article_delivery_mode = str(
        _first_value(data, "articleDeliveryMode", "article_delivery_mode", default="") or ""
    ).strip()
    if article_delivery_mode and config.article_delivery_mode != article_delivery_mode:
        config.article_delivery_mode = article_delivery_mode
        config_update_fields.append("article_delivery_mode")
    default_timezone = str(_first_value(data, "defaultTimezone", "default_timezone", default="") or "").strip()
    if default_timezone and config.default_timezone != default_timezone:
        config.default_timezone = default_timezone
        config_update_fields.append("default_timezone")
    if "dailyDiscoveryEnabled" in data or "daily_discovery_enabled" in data:
        daily_enabled = _bool_from_value(
            _first_value(data, "dailyDiscoveryEnabled", "daily_discovery_enabled", default=False)
        )
        if config.daily_discovery_enabled != daily_enabled:
            config.daily_discovery_enabled = daily_enabled
            config_update_fields.append("daily_discovery_enabled")

    if config_update_fields:
        config_update_fields.append("updated_at")
        config.save(update_fields=config_update_fields)

    _, startup_profile = resolve_or_create_profile(domain=organization.domain)
    startup_update_fields = []
    if founder_names_provided and startup_profile.founder_names != founder_names:
        startup_profile.founder_names = founder_names
        startup_update_fields.append("founder_names")
    if competitors_provided and startup_profile.competitor_domains != competitors:
        startup_profile.competitor_domains = competitors
        startup_update_fields.append("competitor_domains")
    if seed_keywords_provided and startup_profile.positive_keywords != seed_keywords:
        startup_profile.positive_keywords = seed_keywords
        startup_update_fields.append("positive_keywords")
    if stage_provided and startup_profile.stage != stage:
        startup_profile.stage = stage
        startup_update_fields.append("stage")
    if organization_kind_provided and startup_profile.organization_kind != organization_kind:
        startup_profile.organization_kind = organization_kind
        startup_update_fields.append("organization_kind")
    if short_description_provided and startup_profile.short_description != short_description:
        startup_profile.short_description = short_description
        startup_update_fields.append("short_description")
    if problem_solved_provided and startup_profile.problem_solved != problem_solved:
        startup_profile.problem_solved = problem_solved
        startup_update_fields.append("problem_solved")
    if target_audience_provided and startup_profile.target_audience != target_audience:
        startup_profile.target_audience = target_audience
        startup_update_fields.append("target_audience")
    if notes_provided and startup_profile.notes != notes:
        startup_profile.notes = notes
        startup_update_fields.append("notes")
    if company.name and company.name not in startup_profile.company_aliases:
        startup_profile.company_aliases = [*startup_profile.company_aliases, company.name]
        startup_update_fields.append("company_aliases")
    if organization.domain and organization.domain not in startup_profile.domain_aliases:
        startup_profile.domain_aliases = [*startup_profile.domain_aliases, organization.domain]
        startup_update_fields.append("domain_aliases")
    if startup_update_fields:
        startup_update_fields.append("updated_at")
        startup_profile.save(update_fields=startup_update_fields)

    bind_user_to_startup(user=user, organization=organization, role="founder", is_default_for_gmail=True)
    return organization


def get_or_create_founder_profile(user) -> VibeRaisingProfile:
    reconcile_user_slack_id_from_email(user)
    profile, _created = VibeRaisingProfile.objects.get_or_create(
        user=user,
        defaults={"role": VibeRaisingProfile.ROLE_FOUNDER},
    )
    return profile


@transaction.atomic
def set_active_company(profile: VibeRaisingProfile, company: VibeRaisingCompany) -> VibeRaisingProfile:
    if company.profile_id != profile.id:
        raise ValueError("Company does not belong to this profile.")
    ensure_company_organization(company)
    profile.active_company = company
    profile.save(update_fields=["active_company", "updated_at"])
    return profile


@transaction.atomic
def resolve_active_company(profile: VibeRaisingProfile) -> VibeRaisingCompany | None:
    company = profile.active_company or profile.companies.order_by("created_at", "name").first()
    if company and profile.active_company_id != company.id:
        profile.active_company = company
        profile.save(update_fields=["active_company", "updated_at"])
    if company:
        ensure_company_organization(company)
    return company


@dataclass(frozen=True)
class FounderCompanyContext:
    profile: VibeRaisingProfile
    company: VibeRaisingCompany
    organization: Organization


def get_founder_company_context(user, company_id=None, *, persist_active=False) -> FounderCompanyContext:
    profile = get_or_create_founder_profile(user)
    if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
        raise PermissionError("Only founders can access this product.")

    companies = profile.companies.select_related("organization")
    if company_id:
        # `companies` is already scoped to this profile, so .get() enforces
        # ownership (DoesNotExist for a company the user does not own).
        company = companies.get(pk=company_id)
        # By default a per-request company_id only SCOPES this request; it does
        # NOT mutate the profile's shared active_company. Only the explicit
        # switch endpoint (and write/setup flows that opt in via persist_active)
        # should persist the selection — otherwise concurrent per-startup
        # requests thrash each other's active company. See Phase 4 of the
        # multi-startup isolation plan.
        if persist_active and profile.active_company_id != company.id:
            set_active_company(profile, company)
    else:
        company = resolve_active_company(profile)
        if company is None:
            raise VibeRaisingCompany.DoesNotExist("No founder company exists.")

    organization = ensure_company_organization(company)
    if organization is None:
        raise Organization.DoesNotExist("Company domain is required.")

    return FounderCompanyContext(profile=profile, company=company, organization=organization)
