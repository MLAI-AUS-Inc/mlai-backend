from __future__ import annotations

import logging
import uuid

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status as drf_status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from core.actor_ids import (
    actor_ids_for_user as shared_actor_ids_for_user,
    is_internal_actor_id,
    preferred_actor_id_for_user,
    synthetic_actor_id_for_user_id,
)
from integrations.utils import normalize_domain
from organizations.models import Organization

from .models import VibeRaisingCompany, VibeRaisingProfile

logger = logging.getLogger(__name__)


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


class DomainOwnershipError(APIException):
    """A founder tried to use a domain another founder already owns.

    Vibe Raising tenancy is keyed on ``Organization.domain`` -- a unique, shared
    row -- so claiming a domain attaches you to whatever tenant already owns it.
    Until DNS-TXT domain verification lands, ownership is *first-claim-wins*: the
    earliest founder to bind a company to a domain's Organization owns that
    tenant, and no other (non-admin) founder may read or write it. Surfaces as
    HTTP 409 so the frontend can tell the founder the domain is already linked.
    """

    status_code = drf_status.HTTP_409_CONFLICT
    default_detail = "This domain is already linked to another account."
    default_code = "domain_already_claimed"


COMPANY_DOMAIN_CLAIMED_CODE = "company_domain_claimed"
COMPANY_DOMAIN_CLAIMED_DETAIL = "This startup is already on MLAI."


class CompanyDomainClaimedError(DomainOwnershipError):
    """A startup save claimed a domain another founder's startup already holds.

    Raised only at the startup claim sites (company save, a user-checked domain
    change, and binding a company to an existing organisation). Other tenancy
    guards keep the base ``DomainOwnershipError`` body. The founder can carry on
    without the website, so the body says so and never names the owner. Callers
    catch it outside any transaction that rolls back, queue the
    ``domain_claim_disputed`` alert, and return :meth:`response`. Left
    uncaught, DRF's exception handler renders the same body (``detail`` is kept
    a plain dict, so ``canContinueWithoutDomain`` stays a JSON boolean) but no
    alert is queued.
    """

    default_detail = COMPANY_DOMAIN_CLAIMED_DETAIL
    default_code = COMPANY_DOMAIN_CLAIMED_CODE

    def __init__(self, organization=None):
        #: The organisation whose existing claim refused this save, if known.
        self.organization = organization
        super().__init__()
        # APIException would turn every value into a string (True to "True").
        self.detail = self.body()

    def get_codes(self):
        return COMPANY_DOMAIN_CLAIMED_CODE

    def get_full_details(self):
        return self.body()

    @staticmethod
    def body() -> dict:
        """The 409 body shared by every claim site (no owner name or email)."""
        return {
            "code": COMPANY_DOMAIN_CLAIMED_CODE,
            "detail": COMPANY_DOMAIN_CLAIMED_DETAIL,
            "field": "domain",
            "canContinueWithoutDomain": True,
        }

    def response(self) -> Response:
        return Response(self.body(), status=drf_status.HTTP_409_CONFLICT)


def claimed_organization(domain) -> Organization | None:
    """The existing organisation a domain would join, if any."""
    normalized = normalize_company_domain(domain)
    return Organization.objects.filter(domain=normalized).first() if normalized else None


_ALERT_HELPER_MODULE = "content_factory.founder_journey_alerts"


def queue_domain_claim_alert(*, organization, claimant_user_id) -> str | None:
    """Tell ops that a founder was refused a domain another startup holds.

    Calls the founder-journey alert helper (IF-15, reason domain_claim_disputed)
    and must run outside the transaction that rolled the refused save back.
    Until that module is deployed with this code, a structured warning is the
    alert; a module that exists but fails to import is logged as an error. Returns the alert id, or None when nothing could be queued. Alert
    storage problems are logged and never change the founder's response.
    """
    if organization is None:
        logger.warning("founder_journey_alert_missing_org alert_reason=domain_claim_disputed claimant=%s",
                       claimant_user_id)
        return None
    try:
        from content_factory.founder_journey_alerts import queue_founder_journey_alert
    except ImportError as exc:
        if not (isinstance(exc, ModuleNotFoundError) and exc.name == _ALERT_HELPER_MODULE):
            # The helper is deployed but cannot load: that is a fault, not a
            # missing feature, so it is logged as an error with no receipt.
            logger.exception("founder_journey_alert_helper_broken alert_reason=domain_claim_disputed org_id=%s",
                             organization.pk)
            return None
        alert_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mlai:founder-journey:{organization.pk}:claimant-user-{claimant_user_id}:domain_claim_disputed",
        ))
        logger.warning(
            "founder_journey_alert alert_id=%s alert_reason=domain_claim_disputed org_id=%s claimant_user_id=%s",
            alert_id, organization.pk, claimant_user_id,
            extra={"alert_id": alert_id, "alert_reason": "domain_claim_disputed", "org_id": organization.pk,
                   "domain": organization.domain, "claimant_user_id": claimant_user_id, "state": "unconfigured"},
        )
        return alert_id
    try:
        return queue_founder_journey_alert(
            organization=organization, alert_reason="domain_claim_disputed", company_id=None,
            details={"claimant_user_id": claimant_user_id},
        )
    except Exception:
        logger.exception("founder_journey_alert_queue_failed alert_reason=domain_claim_disputed org_id=%s",
                         organization.pk)
        return None


PREVIOUS_OWNER_KEY = "previous_owner"


def research_workspace_claim_hold() -> timedelta:
    """How long an unsaved research workspace holds its domain."""
    return timedelta(hours=settings.FOUNDER_RESEARCH_WORKSPACE_CLAIM_HOLD_HOURS)


def _organization_claims(organization):
    """Companies bound to the org, oldest first, as (live, stale research workspaces).

    A research workspace is the hidden company "Research my startup" creates
    before anything is saved. Older than the hold, it no longer claims the
    domain. The researchDraft marker is per organisation, so it cannot tell a
    saved startup from a workspace on its own (an administrator's research sets
    it on an org that already holds a real startup). A workspace is therefore
    stale only when it is the org's one and only company and nobody has made it
    their active startup; any other org keeps first-claim-wins and releases
    nothing.
    """
    from .profile_fields import is_research_workspace

    companies = list(
        VibeRaisingCompany.objects
        .filter(organization=organization)
        .select_related("profile")
        .order_by("created_at", "id")
    )
    if len(companies) != 1:
        return companies, []
    [workspace] = companies
    cutoff = timezone.now() - research_workspace_claim_hold()
    if (
        workspace.created_at > cutoff
        or not is_research_workspace(workspace)
        or VibeRaisingProfile.objects.filter(active_company=workspace).exists()
    ):
        return companies, []
    return [], companies


def organization_owner_user_id(organization) -> int | None:
    """User id of the founder who owns this organization's Vibe Raising tenant.

    First-claim-wins: the earliest ``VibeRaisingCompany`` bound to the org,
    skipping research workspaces older than the claim hold unless they are all
    there is. Returns ``None`` when there is no current founder claim. This does
    not establish that the existing tenant is empty or available for reassignment.
    """
    if organization is None:
        return None
    live, stale = _organization_claims(organization)
    first = (live or stale or [None])[0]
    return first.profile.user_id if first else None


def user_may_use_organization(user, organization) -> bool:
    """Existing tenant history requires an established founder owner."""
    owner_id = organization_owner_user_id(organization)
    return owner_id is not None and owner_id == getattr(user, "id", None)


def previous_owner_matches(user, organization) -> bool:
    """Whether ``user`` is the recorded owner of a startup they deleted here."""
    from content_factory.models import OrganizationContentConfig

    user_id = getattr(user, "id", None)
    if organization is None or user_id is None:
        return False
    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    strategy = config.pillar_strategy if config and isinstance(config.pillar_strategy, dict) else {}
    record = strategy.get(PREVIOUS_OWNER_KEY)
    return isinstance(record, dict) and record.get("user_id") == user_id


def user_may_claim_organization(user, organization) -> bool:
    """Whether a founder's startup may bind to this existing organisation.

    The owner may. With no live claim, the founder who deleted the startup may
    take it back, and a domain held only by stale research workspaces is free.
    Unowned history otherwise still needs review. Tenancy reads keep using
    :func:`user_may_use_organization`, which this does not widen.
    """
    if user_may_use_organization(user, organization):
        return True
    live, stale = _organization_claims(organization)
    if live:
        return False
    return previous_owner_matches(user, organization) or bool(stale)


def domain_is_available_to(user, domain) -> bool:
    """New domains may be claimed; an existing unowned tenant needs review."""
    organization = claimed_organization(domain)
    if organization is None:
        return True
    return user_may_claim_organization(user, organization)


def _settle_organization_claim(user, company, organization) -> None:
    """Finish a permitted claim: reclaim a deleted startup or release stale workspaces.

    Runs inside ensure_company_organization's transaction with the org locked,
    so a refused or rolled-back save releases nothing.
    """
    from content_factory.models import OrganizationContentConfig
    from .profile_fields import clear_research_draft

    live, stale = _organization_claims(organization)
    released = [workspace for workspace in stale
                if workspace.pk != company.pk and workspace.profile.user_id != user.id]
    if released and not live:
        # Nothing the other account researched passes to this founder: its
        # runs, approved catalogue and config go with the workspace. The hidden
        # workspace would otherwise become this founder's owner (and a visible
        # company for the researcher) once the marker is cleared.
        _purge_org_marketing_data(organization)
        VibeRaisingCompany.objects.filter(pk__in=[workspace.pk for workspace in released]).delete()
        clear_research_draft(organization)
        logger.info(
            "research_workspace_released org=%s claimant=%s released=%s",
            organization.pk, user.id, len(released),
        )
    config = OrganizationContentConfig.objects.select_for_update().filter(organization=organization).first()
    strategy = config.pillar_strategy if config and isinstance(config.pillar_strategy, dict) else {}
    record = strategy.get(PREVIOUS_OWNER_KEY)
    if isinstance(record, dict) and record.get("user_id") == user.id:
        strategy = dict(strategy)
        strategy.pop(PREVIOUS_OWNER_KEY)
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])
        logger.info("company_domain_reclaimed org=%s user=%s", organization.pk, user.id)


def _record_previous_owner(organization, *, user_id, company_id) -> None:
    """Remember who deleted this sole-owned startup so they can add it again."""
    from content_factory.models import OrganizationContentConfig

    config, _ = OrganizationContentConfig.objects.get_or_create(organization=organization)
    strategy = dict(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
    strategy[PREVIOUS_OWNER_KEY] = {
        "user_id": user_id,
        "company_id": str(company_id),
        "offboarded_at": timezone.now().isoformat(),
        "reason": "company_deleted",
    }
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


def summarize_organization_data(organization) -> dict:
    """Counts of the org-keyed assets a founder would lose sight of if their
    company were re-pointed away from this Organization."""
    from content_factory.models import OrganizationContentConfig

    config = OrganizationContentConfig.objects.filter(organization=organization).first()
    return {
        "connections": organization.external_service_connections.count(),
        "gmailConnections": organization.google_connections.count(),
        "articleRuns": organization.content_factory_runs.count(),
        "monthlyUpdates": organization.monthly_update_drafts.count(),
        "hasGithubRepo": bool(config and (config.github_repo or "").strip()),
    }


def organization_has_material_data(summary: dict) -> bool:
    return any(
        summary.get(key)
        for key in ("connections", "gmailConnections", "articleRuns", "monthlyUpdates", "hasGithubRepo")
    )


class CompanyDomainChangeBlocked(ValueError):
    """Re-pointing this company to another Organization would strand its data.

    Organization is the tenant boundary: integrations, Gmail, article runs and
    monthly updates all hang off it. When a domain edit cannot be satisfied by
    renaming the org in place, the move detaches the company from that history
    -- so it needs an explicit confirmation instead of happening silently.
    """

    def __init__(self, company: "VibeRaisingCompany", current_domain: str, new_domain: str, data_summary: dict):
        self.company = company
        self.current_domain = current_domain
        self.new_domain = new_domain
        self.data_summary = data_summary
        super().__init__(
            f"Changing {company.name or 'this company'}'s website from '{current_domain}' to "
            f"'{new_domain}' disconnects its existing integrations, article runs and updates. "
            "Confirm the domain change to continue anyway, or register the new domain as a "
            "separate company."
        )


def apply_company_domain_change(company: VibeRaisingCompany, new_domain, *, user=None, confirmed=False) -> str:
    """Migrate-or-guard a company's move to a new domain.

    Call BEFORE writing the new domain to the company (and before
    ensure_company_organization re-resolves the org):

    - Renames the existing Organization in place when that is safe (the org is
      exclusively this company's and no Organization exists on the new domain),
      so connections/runs/updates follow the company. Returns "renamed".
    - Otherwise the eventual ensure_company_organization will re-point the
      company at a different Organization. If the old org holds material data
      and the caller has not confirmed, raises CompanyDomainChangeBlocked
      (surfaced as a structured 409). A confirmed stranding is logged so
      orphaned orgs stay discoverable. Returns "repoint".
    - No-ops for new companies, blank domains, and unchanged domains.

    ``user`` (when given) is also checked against first-claim-wins domain
    ownership, mirroring the vibe-raising company endpoint.
    """
    organization = company.organization if company.organization_id else None
    normalized_new = normalize_company_domain(new_domain)
    if organization is None or not normalized_new:
        return "noop"
    if normalize_company_domain(organization.domain) == normalized_new:
        return "noop"

    if user is not None and not domain_is_available_to(user, normalized_new):
        raise CompanyDomainClaimedError(claimed_organization(normalized_new))

    target_exists = Organization.objects.filter(domain=normalized_new).exclude(pk=organization.pk).exists()
    exclusive = not (
        VibeRaisingCompany.objects.filter(organization=organization).exclude(pk=company.pk).exists()
    )

    if not target_exists and exclusive:
        old_domain = organization.domain
        organization.domain = normalized_new
        organization.save(update_fields=["domain"])
        logger.info(
            "company_domain_change_renamed_org company=%s org=%s old_domain=%s new_domain=%s",
            company.pk,
            organization.pk,
            old_domain,
            normalized_new,
        )
        return "renamed"

    summary = summarize_organization_data(organization)
    if organization_has_material_data(summary):
        if not confirmed:
            raise CompanyDomainChangeBlocked(company, organization.domain, normalized_new, summary)
        logger.warning(
            "company_domain_change_stranded_org company=%s org=%s old_domain=%s new_domain=%s data=%s",
            company.pk,
            organization.pk,
            organization.domain,
            normalized_new,
            summary,
        )
    # Only this guarded, explicit domain-edit path may request a new tenant.
    # Ordinary reporting reads retain the company's existing organization.
    company.organization = None
    return "repoint"


def _purge_org_marketing_data(organization) -> None:
    """Delete the org-level marketing/raising artifacts an offboarded company
    leaves behind (the startup-update purge only covers ingestion artifacts)."""
    from content_factory.models import OrganizationContentConfig
    from startup_updates.models import MonthlyUpdateDraft
    from workflow_runs.models import ContentFactoryRun

    from content_factory.website_connections import offboard_website_connections
    offboard_website_connections(organization, purge=True)
    MonthlyUpdateDraft.objects.filter(organization=organization).delete()
    # Cascades ContentFactoryRunStep; ContentFactoryJob is domain-keyed, not FK.
    ContentFactoryRun.objects.filter(organization=organization).delete()
    OrganizationContentConfig.objects.filter(organization=organization).delete()


def offboard_company(company: VibeRaisingCompany, *, reason: str = "company_offboarding") -> dict:
    """Disconnect a company's integrations, purge its data, and delete the row.

    Composes the existing per-provider disconnect/purge machinery:

    - **Always** revokes and removes *this user's* Gmail and third-party
      connections for the company's organization. Those rows are keyed by
      ``(user, organization)``, so a co-founder who shares the org (first-claim
      tenancy) is never touched. This is the security-critical step: it stops a
      shut-down startup from keeping live OAuth tokens.
    - **Org-level** data (monthly updates, article runs, content config, and the
      startup-update ingestion artifacts) is purged only when this company is the
      org's sole owner — never a co-owner's shared data.
    - The **Organization row is retained** (it is the tenant boundary; any later
      reassignment requires explicit ownership review). For a sole owner it
      records ``pillar_strategy['previous_owner']``, so the same founder can add
      the startup again; nobody else can claim the retained domain.
    - Re-points the profile's active company to a sibling (or clears it), then
      deletes the company row.

    Best-effort and idempotent-ish: a provider hiccup is collected as a warning
    rather than aborting the offboard, since a half-connected startup is worse
    than an over-cleaned one. Returns a summary of what was removed.
    """
    from integrations.models import ExternalServiceConnection
    from startup_updates.data_deletion import (
        delete_startup_data_for_organization,
        disconnect_gmail_for_user,
    )

    profile = company.profile
    user = profile.user
    organization = company.organization if company.organization_id else None
    company_id = company.id

    summary = {
        "companyId": str(company_id),
        "gmailDisconnected": False,
        "connectionsRemoved": 0,
        "orgShared": False,
        "orgDataPurged": False,
        "targetedPurgeCompleted": False,
        "domainReleased": False,
        "warnings": [],
    }

    if organization is not None:
        org_shared = (
            VibeRaisingCompany.objects.filter(organization=organization)
            .exclude(pk=company.pk)
            .exists()
        )
        summary["orgShared"] = org_shared
        if org_shared:
            from content_factory.website_connections import offboard_website_connections
            offboard_website_connections(organization, user=user)

        # 1) Gmail: revoke the Google refresh token, delete the connection row
        #    and its cached mail. disconnect_gmail_for_user is (user, org)-scoped.
        try:
            gmail_result = disconnect_gmail_for_user(
                user, delete_derived_data=True, organization=organization, reason=reason
            )
            summary["gmailDisconnected"] = bool(gmail_result.get("googleAccount"))
        except Exception as exc:  # pragma: no cover - never block offboarding
            logger.exception("offboard_gmail_failed company=%s", company_id)
            summary["warnings"].append(f"Gmail disconnect failed: {exc}")

        # 2) Third-party connections (Stripe/Notion/Linear/Slack/GA/Xero/Luma/
        #    bank): delete this user's rows for the org, removing the encrypted
        #    tokens and cascading their financial accounts/records.
        connections = ExternalServiceConnection.objects.filter(user=user, organization=organization)
        summary["connectionsRemoved"] = connections.count()
        connections.delete()

        # 3) Org-level data — only when this company is the org's sole owner.
        if not org_shared:
            try:
                delete_startup_data_for_organization(
                    organization, requested_by_user_id=user.id, reason=reason
                )
            except Exception as exc:  # pragma: no cover - collected, not fatal
                logger.exception("offboard_startup_purge_failed company=%s", company_id)
                summary["warnings"].append(f"Startup data purge failed: {exc}")
            _purge_org_marketing_data(organization)
            _record_previous_owner(organization, user_id=user.id, company_id=company_id)
            # The organisation and other retained records still exist. Do not
            # claim a complete tenant purge or permit automatic domain reuse.
            summary["targetedPurgeCompleted"] = not summary["warnings"]

    # 4) Re-point (or clear) the active company before removing this one.
    if profile.active_company_id == company.id:
        replacement = (
            profile.companies.exclude(pk=company.pk).order_by("created_at", "name").first()
        )
        profile.active_company = replacement
        profile.save(update_fields=["active_company", "updated_at"])
        summary["newActiveCompanyId"] = str(replacement.id) if replacement else None

    # 5) Delete the company row — frees the (profile, domain) slot for re-use.
    company.delete()
    logger.info(
        "company_offboarded company=%s user=%s org_shared=%s connections=%s",
        company_id,
        user.id,
        summary["orgShared"],
        summary["connectionsRemoved"],
    )
    return summary


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
    return synthetic_actor_id_for_user_id(user.id)


def actor_ids_for_user(user) -> list[str]:
    return shared_actor_ids_for_user(user)


def founder_actor_id_for_user(user) -> str:
    return preferred_actor_id_for_user(user)


def _is_synthetic_actor_id(value: str | None) -> bool:
    return is_internal_actor_id(value)


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
    from core.slack_founder_links import (
        ConflictingSlackFounderLinkError,
        assign_direct_slack_identity,
    )

    try:
        assign_direct_slack_identity(user, slack_backed_user.slack_id)
    except (ConflictingSlackFounderLinkError, IntegrityError):
        return False
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
    if company.organization_id:
        return company.organization
    normalized_domain = normalize_company_domain(company.domain)
    if not normalized_domain:
        organization, _ = Organization.objects.get_or_create(domain=f"startup-{company.pk}.invalid", defaults={"name": company.name})
        company.organization = organization
        company.save(update_fields=["organization", "updated_at"])
        return organization

    company_update_fields = []
    if company.domain != normalized_domain:
        company.domain = normalized_domain
        company_update_fields.append("domain")

    organization, created = Organization.objects.select_for_update().get_or_create(
        domain=normalized_domain,
        defaults={"name": company.name},
    )
    if not created:
        user = company.profile.user
        is_admin = bool(user.is_staff or user.is_superuser)
        if not is_admin:
            if not user_may_claim_organization(user, organization):
                raise CompanyDomainClaimedError(organization)
            _settle_organization_claim(user, company, organization)
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
    from founder_tools.profile_fields import validate_profile_fields, save_profile_details

    data = validate_profile_fields(data)
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
    from content_factory.portable_drafts import website_preview_delivery_mode

    # A connected website always previews: content_only is stored as review_draft.
    article_delivery_mode = website_preview_delivery_mode(config, str(
        _first_value(data, "articleDeliveryMode", "article_delivery_mode", default="") or ""
    ).strip())
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

    save_profile_details(organization, data, config=config)
    bind_user_to_startup(user=user, organization=organization, role="founder", is_default_for_gmail=True)
    return organization


def _member_display_name(user) -> str:
    """The member's own account name; never derived from the email address."""
    name = str(getattr(user, "full_name", "") or "").strip() or str(getattr(user, "first_name", "") or "").strip()
    return " ".join(name.split())[:255]


def seed_new_company_founder_and_author(*, user, company: VibeRaisingCompany) -> None:
    """Record the signed-in member as founder 1 and default article author.

    Called only when a startup is first saved. Existing founders (profile rows
    or legacy founder names) and existing authors are never overwritten, and a
    member without a name seeds nothing, so the Founders step stays optional.
    """
    from content_factory.authors import normalize_author
    from content_factory.models import OrganizationContentConfig
    from founder_tools.profile_fields import profile_details, save_profile_details, validate_profile_fields
    from startup_updates.services import resolve_or_create_profile

    organization = company.organization if company.organization_id else None
    name = _member_display_name(user)
    if organization is None or not name:
        return
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        _, startup_profile = resolve_or_create_profile(domain=organization.domain)
        if not profile_details(organization).get("founderProfiles") and not startup_profile.founder_names:
            data = validate_profile_fields({"founderProfiles": [{"name": name}]})
            save_profile_details(organization, data)
            startup_profile.founder_names = data["founderNames"]
            startup_profile.save(update_fields=["founder_names", "updated_at"])
        config, _ = OrganizationContentConfig.objects.select_for_update().get_or_create(organization=organization)
        if not config.authors:
            author = normalize_author({"id": f"member-{user.id}", "name": name, "role": "Founder"})
            config.authors = [author]
            config.default_author_id = author["id"]
            config.save(update_fields=["authors", "default_author_id", "updated_at"])


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
    from founder_tools.profile_fields import visible_companies
    company = profile.active_company or visible_companies(profile.companies.all()).order_by("created_at", "name").first()
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
