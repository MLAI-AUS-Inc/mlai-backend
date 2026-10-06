"""Reviewed founder-account revocation, preserving other founders' grants."""

from django.db import transaction
from django.db.models import Q

from .website_contract import WebsiteAuthorityError, evidence_digest


def revocation_plan(user):
    """List every owned website binding affected by removing saved GitHub access."""
    from founder_tools.models import VibeRaisingCompany
    from .website_models import WebsiteConnection
    companies = {row.organization_id: row for row in VibeRaisingCompany.objects.filter(profile__user=user).exclude(organization=None).select_related("organization")}
    rows = WebsiteConnection.objects.filter(organization_id__in=companies, authorized_by=user,
        state__in=["connected", "paused"]).order_by("organization_id", "pk")
    affected = [{"companyId": str(companies[row.organization_id].pk), "domain": row.organization.domain,
        "connectionId": str(row.pk), "connectionGeneration": row.generation,
        "configurationRevision": row.configuration_version, "repository": row.github_repo} for row in rows.select_related("organization")]
    from integrations.models import GitHubInstallation, UserIntegration
    from core.actor_ids import actor_ids_for_user
    identifiers = list(GitHubInstallation.objects.filter(user=user).order_by("installation_id").values_list("installation_id", flat=True))
    legacy = list(UserIntegration.objects.filter(slack_user_id__in=actor_ids_for_user(user)).exclude(
        Q(github_installation_id__isnull=True) & Q(github_access_token__isnull=True)).order_by("slack_user_id").values_list("slack_user_id", "github_installation_id"))
    return {"schemaVersion": 1, "planDigest": evidence_digest({"user": str(user.pk), "affected": affected, "installations": identifiers, "legacy": legacy}),
        "affectedCompanies": affected, "providerRevocation": "manual_required",
        "providerInstructions": [{"label": "Review GitHub App installations and revoke provider access", "url": "https://github.com/settings/installations"},
            {"label": "Review authorised OAuth applications", "url": "https://github.com/settings/applications"}]}


def apply_revocation(user, config, *, data, idempotency_key):
    """Fence all reviewed owned bindings before clearing this founder's grants."""
    from organizations.models import Organization
    from integrations.models import GitHubInstallation, UserIntegration
    from core.actor_ids import actor_ids_for_user
    from .models import OrganizationContentConfig, ResearchAutomation
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from .website_connections import contract_for, transition_connection
    from django.contrib.auth import get_user_model
    if data.get("approved") is not True:
        raise WebsiteAuthorityError("github_revocation_review_required", "Review the affected startups before revoking GitHub access.")
    key = f"{config.website_connection_id}:github-revoke:{idempotency_key}"
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        from core.actor_ids import preferred_actor_id_for_user
        actor = preferred_actor_id_for_user(user)
        legacy, _ = UserIntegration.objects.get_or_create(slack_user_id=actor)
        previous = (legacy.pending_intent or {}).get("github_revocations", {})
        if idempotency_key in previous:
            saved = previous[idempotency_key]
            if saved["plan_digest"] != data.get("plan_digest"):
                raise WebsiteAuthorityError("operation_key_conflict", "This revocation identity belongs to a different review.")
            return saved["receipt"]
        existing = WebsiteConnectionOperation.objects.filter(idempotency_key=key, connection__authorized_by=user).first()
        if existing:
            if existing.payload.get("plan_digest") != data.get("plan_digest"):
                raise WebsiteAuthorityError("operation_key_conflict", "This revocation key belongs to a different review.")
            return existing
        plan = revocation_plan(user)
        if data.get("plan_digest") != plan["planDigest"]:
            raise WebsiteAuthorityError("github_revocation_plan_changed", "GitHub access changed. Review the affected startups again.")
        ids = [row["connectionId"] for row in plan["affectedCompanies"]]
        orgs = sorted(WebsiteConnection.objects.filter(pk__in=ids).values_list("organization_id", flat=True))
        list(Organization.objects.select_for_update().filter(pk__in=orgs).order_by("pk"))
        list(WebsiteConnection.objects.select_for_update().filter(pk__in=ids).order_by("organization_id", "pk"))
        if revocation_plan(user)["planDigest"] != plan["planDigest"]:
            raise WebsiteAuthorityError("github_revocation_plan_changed", "Website consent changed during review.")
        for row in OrganizationContentConfig.objects.filter(website_connection_id__in=ids).select_related("website_connection__organization"):
            transition_connection(row, action="revoke", expected=contract_for(row.website_connection), idempotency_key=f"account:{plan['planDigest']}")
        # These rows are per-user even when an installation ID is shared.
        GitHubInstallation.objects.filter(user=user).delete()
        aliases = actor_ids_for_user(user)
        UserIntegration.objects.filter(slack_user_id__in=aliases).update(github_access_token=None, github_refresh_token=None,
            github_token_expires_at=None, github_installation_id=None, github_scopes=[], github_user_name=None)
        owned = OrganizationContentConfig.objects.filter(Q(connected_slack_user_id__in=aliases) | Q(website_connection__authorized_by=user))
        owned.update(github_token_encrypted=None, github_refresh_token_encrypted=None, github_token_expires_at=None,
            github_installation_id=None, github_scopes=[], github_user_name=None, auto_publish=False, daily_discovery_enabled=False)
        ResearchAutomation.objects.filter(organization_id__in=owned.values_list("organization_id", flat=True), status="active").update(status="paused")
        receipt = {"status": "local_access_revoked", "githubRevocation": plan, "local_credentials_removed": True,
            "provider_revocation_complete": False, "provider_cleanup_pending": True, "repository_modified": False}
        legacy.refresh_from_db()
        legacy.pending_intent = {**(legacy.pending_intent or {}), "github_revocations": {**previous, idempotency_key: {"plan_digest": plan["planDigest"], "receipt": receipt}}}
        legacy.save(update_fields=["pending_intent", "updated_at"])
        config.refresh_from_db()
        if not config.website_connection_id:
            return receipt
        return WebsiteConnectionOperation.objects.create(connection=config.website_connection, generation=config.website_connection.generation,
            action="github-revoke", state="completed", idempotency_key=key, payload={"plan_digest": plan["planDigest"]},
            receipt=receipt)
