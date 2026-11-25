"""Database-backed startup access and workspace lifecycle."""
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import StartupMembership, StartupMembershipEvent, VibeRaisingCompany, VibeRaisingProfile


def shared_workspace(organization):
    """Return the canonical company, including closed workspaces."""
    if not getattr(organization, "pk", None):
        return None
    return VibeRaisingCompany.objects.filter(
        organization=organization, membership_policy=VibeRaisingCompany.SHARED_ACCESS,
    ).first()


def active_memberships(company):
    """Query currently eligible people on an open startup."""
    return StartupMembership.objects.filter(
        company=company, company__closed_at__isnull=True, removed_at__isnull=True, user__is_active=True,
    )


def has_membership(user, company):
    return bool(
        getattr(user, "is_authenticated", False) and getattr(user, "is_active", False)
        and active_memberships(company).filter(user=user).exists()
    )


def companies_for_user(user):
    """Return canonical shared companies and legacy rows outside shared tenants."""
    companies = VibeRaisingCompany.objects.select_related("organization")
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return companies.none()
    memberships = StartupMembership.objects.filter(company_id=OuterRef("pk"), user=user, removed_at__isnull=True)
    canonical = VibeRaisingCompany.objects.filter(
        organization_id=OuterRef("organization_id"), membership_policy=VibeRaisingCompany.SHARED_ACCESS,
    )
    return companies.filter(closed_at__isnull=True).filter(
        Q(membership_policy=VibeRaisingCompany.SHARED_ACCESS) & Exists(memberships)
        | Q(membership_policy=VibeRaisingCompany.LEGACY_ACCESS, profile__user=user) & ~Exists(canonical)
    )


def lock_company_edit(user, company):
    """Serialize startup edits with adoption, removal and closure."""
    from django.shortcuts import get_object_or_404
    from organizations.models import Organization

    if company.organization_id:
        Organization.objects.select_for_update().get(pk=company.organization_id)
        company = get_object_or_404(companies_for_user(user), pk=company.pk)
    return company


def record_event(company, kind, *, actor=None, subject=None):
    return StartupMembershipEvent.objects.create(company=company, kind=kind, actor=actor, subject=subject)


def admit(company, user, *, source, actor=None, now=None, member_id=None):
    """Create or reactivate one membership while the organization lock is held."""
    now = now or timezone.now()
    defaults = {"joined_at": now, "source": source, "admitted_by": actor}
    if member_id is not None:
        defaults["id"] = member_id
    membership, created = StartupMembership.objects.get_or_create(company=company, user=user, defaults=defaults)
    if created:
        record_event(company, "joined", actor=actor, subject=user)
    elif membership.removed_at is not None and source != "legacy_roster":
        membership.joined_at = now
        membership.removed_at = None
        membership.source = source
        membership.admitted_by = actor
        membership.save(update_fields=["joined_at", "removed_at", "source", "admitted_by"])
        record_event(company, "rejoined", actor=actor, subject=user)
        created = True
    return membership, created


def activate_new_company(company, creator):
    """Make an unambiguous new saved workspace and its creator shareable atomically."""
    if not getattr(settings, "MY_STARTUP_SHARED_ACCESS_ENABLED", False):
        return
    from .startup_team import _locked

    with _locked(company.organization):
        if VibeRaisingCompany.objects.filter(organization=company.organization).exclude(pk=company.pk).exists():
            return
        admit(company, creator, source="creation", actor=creator)
        company.membership_policy = VibeRaisingCompany.SHARED_ACCESS
        company.email_invites_enabled = settings.MY_STARTUP_EMAIL_INVITES_ENABLED
        company.save(update_fields=["membership_policy", "email_invites_enabled"])


def _clear_personal_access(company, user_ids):
    from startup_updates.models import UserStartupBinding
    from organizations.models import Organization

    VibeRaisingProfile.objects.filter(user_id__in=user_ids, active_company=company).update(active_company=None)
    UserStartupBinding.objects.filter(
        organization=company.organization, user_id__in=user_ids,
    ).update(monthly_updates_enabled=False, is_default_for_gmail=False)
    Organization.objects.filter(pk=company.organization_id, billing_user_id__in=user_ids).update(billing_user=None)


def remove(company, membership, *, actor, now):
    """Revoke access and clear the departing account's startup selections."""
    from .startup_email_invitations import revoke_open

    revoke_open(company.organization, now, person=membership.user)
    membership.removed_at = now
    membership.save(update_fields=["removed_at"])
    _clear_personal_access(company, [membership.user_id])
    record_event(company, "removed", actor=actor, subject=membership.user)


@transaction.atomic
def close(company, actor):
    """Close the shared workspace under its organization lock."""
    from .startup_email_invitations import revoke_open
    from .startup_team import TeamError, _locked, _save

    with _locked(company.organization) as (config, roster):
        company.refresh_from_db()
        if not has_membership(actor, company):
            raise TeamError("not_found", "Startup not found.", 404)
        now = timezone.now()
        revoke_open(company.organization, now)
        memberships = StartupMembership.objects.filter(company=company, removed_at__isnull=True)
        _clear_personal_access(company, list(memberships.values_list("user_id", flat=True)))
        memberships.update(removed_at=now)
        company.closed_at = now
        company.save(update_fields=["closed_at"])
        if config is not None and roster is not None:
            roster["invite"] = None
            _save(config, roster)
        record_event(company, "closed", actor=actor)


@transaction.atomic
def merge_account_memberships(source, target, *, actor):
    """Reconcile startup access for accounts verified to belong to one person."""
    from organizations.models import Organization
    from .startup_email_invitations import revoke_open
    from .startup_team import _locked, _save, mapping

    organizations = Organization.objects.filter(
        founder_companies__memberships__user=source,
        founder_companies__membership_policy=VibeRaisingCompany.SHARED_ACCESS,
    ).distinct().order_by("pk")
    now = timezone.now()
    for organization in organizations:
        with _locked(organization) as (config, roster):
            company = shared_workspace(organization)
            source_row = StartupMembership.objects.filter(company=company, user=source).first()
            if source_row is None:
                continue
            if not StartupMembership.objects.filter(company=company, user=target).exists():
                if source.is_active and source_row.removed_at is None and not company.closed_at:
                    admit(company, target, source="account_merge", actor=actor, now=now)
                else:
                    StartupMembership.objects.create(
                        company=company, user=target, source="account_merge", joined_at=source_row.joined_at,
                        removed_at=source_row.removed_at or now,
                    )
            revoke_open(organization, now, person=source)
            source_row.delete()
            record_event(company, "account_merged", actor=actor, subject=target)
            if roster and config:
                for request in roster.get("requests", []):
                    if request.get("user_id") == source.pk and request.get("state") == "pending":
                        request.update(state="withdrawn", decided_at=now.isoformat(), decided_by_user_id=actor.pk)
                if mapping(roster.get("invite")).get("created_by_user_id") == source.pk:
                    roster["invite"] = None
                _save(config, roster)
    StartupMembership.objects.filter(admitted_by=source).update(admitted_by=None)
