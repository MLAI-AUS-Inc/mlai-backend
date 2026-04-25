from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from integrations.utils import normalize_domain
from organizations.models import Organization

from .models import VibeRaisingCompany, VibeRaisingProfile


def normalize_company_domain(domain: str | None) -> str:
    return normalize_domain(domain or "") or ""


def founder_actor_id_for_user(user) -> str:
    return f"mlai_user:{user.id}"


@transaction.atomic
def ensure_company_organization(company: VibeRaisingCompany) -> Organization | None:
    normalized_domain = normalize_company_domain(company.domain)
    if not normalized_domain:
        return None

    if company.domain != normalized_domain:
        company.domain = normalized_domain

    organization, created = Organization.objects.get_or_create(
        domain=normalized_domain,
        defaults={"name": company.name},
    )
    if not created and not organization.name:
        organization.name = company.name
        organization.save(update_fields=["name"])

    if company.organization_id != organization.id:
        company.organization = organization
        company.save(update_fields=["domain", "organization", "updated_at"])
    elif company.domain == normalized_domain:
        company.save(update_fields=["domain", "updated_at"])

    return organization


def get_or_create_founder_profile(user) -> VibeRaisingProfile:
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


def get_founder_company_context(user, company_id=None) -> FounderCompanyContext:
    profile = get_or_create_founder_profile(user)
    if profile.role != VibeRaisingProfile.ROLE_FOUNDER:
        raise PermissionError("Only founders can access this product.")

    companies = profile.companies.select_related("organization")
    if company_id:
        company = companies.get(pk=company_id)
        if profile.active_company_id != company.id:
            set_active_company(profile, company)
    else:
        company = resolve_active_company(profile)
        if company is None:
            raise VibeRaisingCompany.DoesNotExist("No founder company exists.")

    organization = ensure_company_organization(company)
    if organization is None:
        raise Organization.DoesNotExist("Company domain is required.")

    return FounderCompanyContext(profile=profile, company=company, organization=organization)
