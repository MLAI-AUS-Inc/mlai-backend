from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from .models import (
    CapabilityGrantEffect,
    OrganizationCapabilityGrant,
    OrganizationMembership,
    OrganizationRoleAssignment,
)


INITIAL_CAPABILITIES = (
    "view_general_memory",
    "view_email_memory",
    "view_finance_memory",
    "view_people_sensitive_memory",
    "view_executive_memory",
    "review_claims",
    "manage_sources",
    "publish_knowledge",
    "approve_actions",
)

MEMORY_CLASS_CAPABILITIES = {
    "general": "view_general_memory",
    "internal": "view_general_memory",
    "committee": "view_general_memory",
    "email": "view_email_memory",
    "finance": "view_finance_memory",
    "people_sensitive": "view_people_sensitive_memory",
    "executive": "view_executive_memory",
    "no_agent": None,
}


class OrganizationAuthorizationError(ValueError):
    pass


@dataclass(frozen=True)
class OrganizationAuthorizationContext:
    membership: OrganizationMembership
    role_slugs: tuple[str, ...]
    allowed_capabilities: frozenset[str]
    denied_capabilities: frozenset[str]

    def has_capability(self, capability: str) -> bool:
        return capability in self.allowed_capabilities

    def may_view_memory_class(self, memory_class: str) -> bool:
        required = MEMORY_CLASS_CAPABILITIES.get(str(memory_class))
        return bool(required and self.has_capability(required))

    @property
    def memory_class_access(self) -> dict[str, bool]:
        return {
            memory_class: self.may_view_memory_class(memory_class)
            for memory_class in MEMORY_CLASS_CAPABILITIES
        }


def _active_window(at):
    return Q(valid_from__lte=at) & (
        Q(valid_until__isnull=True) | Q(valid_until__gt=at)
    )


def resolve_actor_authorization(
    actor,
    *,
    at=None,
) -> OrganizationAuthorizationContext:
    """Resolve backend-owned membership and grants for a verified external actor."""

    at = at or timezone.now()
    identity = getattr(actor, "identity", None)
    user = getattr(actor, "user", None)
    organization = getattr(actor, "organization", None)
    if identity is None or organization is None:
        raise OrganizationAuthorizationError("Verified organisation identity is required")
    if identity.organization_id != organization.pk:
        raise OrganizationAuthorizationError("External identity organisation does not match")
    if not identity.is_verified or user is None or not user.is_active:
        raise OrganizationAuthorizationError("External identity is inactive or unresolved")

    membership = (
        OrganizationMembership.objects.select_related("organization", "user")
        .filter(organization=organization, user=user)
        .first()
    )
    if membership is None or not membership.is_effective_at(at):
        raise OrganizationAuthorizationError("Organisation membership is inactive or missing")

    assignments = OrganizationRoleAssignment.objects.filter(
        membership=membership,
        role__organization=organization,
        role__is_active=True,
    ).filter(_active_window(at))
    role_rows = list(assignments.values_list("role_id", "role__slug"))
    role_ids = [role_id for role_id, _ in role_rows]

    subject_filter = Q(membership=membership)
    if role_ids:
        subject_filter |= Q(role_id__in=role_ids, role__organization=organization)
    grants = (
        OrganizationCapabilityGrant.objects.filter(
            subject_filter,
            capability__is_active=True,
        )
        .filter(_active_window(at))
        .values_list("capability__key", "effect")
    )

    allowed = set()
    denied = set()
    for capability, effect in grants:
        if effect == CapabilityGrantEffect.DENY:
            denied.add(capability)
        elif effect == CapabilityGrantEffect.ALLOW:
            allowed.add(capability)

    return OrganizationAuthorizationContext(
        membership=membership,
        role_slugs=tuple(sorted({slug for _, slug in role_rows})),
        allowed_capabilities=frozenset(allowed - denied),
        denied_capabilities=frozenset(denied),
    )


def actor_has_capability(actor, capability: str, *, at=None) -> bool:
    try:
        return resolve_actor_authorization(actor, at=at).has_capability(capability)
    except OrganizationAuthorizationError:
        return False


def actor_may_view_memory_class(actor, memory_class: str, *, at=None) -> bool:
    if memory_class not in MEMORY_CLASS_CAPABILITIES:
        return False
    try:
        return resolve_actor_authorization(actor, at=at).may_view_memory_class(memory_class)
    except OrganizationAuthorizationError:
        return False


def actor_is_active_committee_points_admin(actor) -> bool:
    """Require the exact active legacy class requested for unified Admin Roo.

    PointsAdmin is never sufficient on its own: callers must first pass the
    verified organisation identity, membership, capability, and pilot gates.
    A linked PointsAdmin row that belongs to a different canonical user fails
    closed; historical unlinked rows remain usable because the Slack ID itself
    has already been verified against the organisation identity.
    """

    identity = getattr(actor, "identity", None)
    user = getattr(actor, "user", None)
    slack_user_id = str(getattr(actor, "slack_user_id", "") or "").strip()
    if identity is None or user is None or not slack_user_id:
        return False

    from roo.models import PointsAdmin

    points_admin = PointsAdmin.objects.filter(
        slack_user_id=slack_user_id,
        is_active=True,
        role="committee",
    ).first()
    if points_admin is None:
        return False
    return points_admin.user_id in {None, user.pk}
