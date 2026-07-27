from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from organizations.models import Organization
from roo.models import PointsAdmin
from startup_updates.models import UserStartupBinding

from .models import (
    MembershipSource,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackIdentity,
)


REPORT_SCHEMA_VERSION = 1


def _fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_id(organization_id: int, user_id: int) -> str:
    return _fingerprint(f"org-memory-membership:v1:{organization_id}:{user_id}")[:24]


def build_membership_candidate_report(organization: Organization) -> dict:
    evidence_by_user = defaultdict(list)
    unresolved = []

    bindings = UserStartupBinding.objects.filter(organization=organization).select_related("user")
    for binding in bindings:
        evidence_by_user[binding.user_id].append(
            {
                "source": "user_startup_binding",
                "record_id": binding.pk,
                "legacy_role": binding.role,
            }
        )

    identities = OrganizationIdentity.objects.filter(organization=organization).select_related("user")
    for identity in identities:
        evidence = {
            "source": "organization_identity",
            "record_id": identity.pk,
            "provider": identity.provider,
            "external_tenant_id": identity.external_tenant_id,
            "external_user_id": identity.external_user_id,
            "active": identity.is_active,
            "verified": bool(identity.verified_at),
        }
        if identity.user_id:
            evidence_by_user[identity.user_id].append(evidence)
        else:
            unresolved.append({**evidence, "reason": "identity_has_no_linked_user"})

    legacy_identities = OrganizationSlackIdentity.objects.filter(
        workspace__organization=organization
    ).select_related("workspace", "user")
    for identity in legacy_identities:
        evidence = {
            "source": "legacy_slack_identity",
            "record_id": identity.pk,
            "external_tenant_id": identity.workspace.slack_team_id,
            "external_user_id": identity.slack_user_id,
            "active": identity.is_active and identity.workspace.is_active,
        }
        if identity.user_id:
            evidence_by_user[identity.user_id].append(evidence)
        else:
            unresolved.append({**evidence, "reason": "legacy_identity_has_no_linked_user"})

    points_admins = list(PointsAdmin.objects.select_related("user").all())
    points_by_user = defaultdict(list)
    for points_admin in points_admins:
        evidence = {
            "source": "points_admin",
            "record_id": points_admin.pk,
            "external_user_id": points_admin.slack_user_id,
            "legacy_role": points_admin.role,
            "active": points_admin.is_active,
        }
        if points_admin.user_id:
            points_by_user[points_admin.user_id].append(evidence)
        else:
            unresolved.append({**evidence, "reason": "points_admin_has_no_linked_user"})

    for user_id, evidence_rows in points_by_user.items():
        if user_id in evidence_by_user:
            evidence_by_user[user_id].extend(evidence_rows)
        else:
            unresolved.extend(
                {**row, "reason": "points_admin_has_no_organization_evidence"}
                for row in evidence_rows
            )

    users = {
        user.pk: user
        for user in get_user_model().objects.filter(pk__in=evidence_by_user.keys())
    }
    existing = {
        membership.user_id: membership
        for membership in OrganizationMembership.objects.filter(
            organization=organization,
            user_id__in=evidence_by_user.keys(),
        )
    }
    candidates = []
    for user_id in sorted(evidence_by_user, key=lambda value: users[value].email.lower()):
        user = users[user_id]
        evidence = sorted(
            evidence_by_user[user_id],
            key=lambda row: (row["source"], row["record_id"]),
        )
        issues = []
        if not user.is_active:
            issues.append("user_inactive")
        verified_identity = any(
            row["source"] == "organization_identity"
            and row.get("provider") == "slack"
            and row.get("active")
            and row.get("verified")
            for row in evidence
        )
        if not verified_identity:
            issues.append("verified_slack_identity_missing")
        membership = existing.get(user_id)
        membership_state = None
        if membership:
            membership_state = "active" if membership.is_effective_at() else "inactive"
            if membership_state == "inactive":
                issues.append("existing_membership_inactive")
        candidates.append(
            {
                "candidate_id": _candidate_id(organization.pk, user_id),
                "user_id": user_id,
                "email": user.email,
                "existing_membership": membership_state,
                "evidence": evidence,
                "evidence_fingerprint": _fingerprint(evidence),
                "issues": sorted(issues),
                "approved": False,
                "role_slugs": [],
            }
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": timezone.now().isoformat(),
        "organization": {
            "id": organization.pk,
            "name": organization.name,
            "domain": organization.domain,
        },
        "instructions": (
            "Review every candidate. Set approved=true and list explicit role_slugs; "
            "the apply step rejects stale evidence and unresolved candidates."
        ),
        "candidates": candidates,
        "unresolved": sorted(
            unresolved,
            key=lambda row: (row["source"], row.get("record_id") or 0),
        ),
    }


def apply_reviewed_membership_report(
    *,
    organization: Organization,
    report: dict,
    reviewer,
) -> dict:
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise CommandError("Unsupported reviewed report schema_version")
    report_organization = report.get("organization") or {}
    if (
        report_organization.get("id") != organization.pk
        or report_organization.get("domain") != organization.domain
    ):
        raise CommandError("Reviewed report organisation does not match")
    if not reviewer.is_active or not (reviewer.is_staff or reviewer.is_superuser):
        raise CommandError("The reviewer must be an active staff user")

    current_report = build_membership_candidate_report(organization)
    current_by_id = {
        row["candidate_id"]: row for row in current_report["candidates"]
    }
    approved = [row for row in report.get("candidates", []) if row.get("approved") is True]
    approved_ids = [row.get("candidate_id") for row in approved]
    if len(approved_ids) != len(set(approved_ids)):
        raise CommandError("Reviewed report contains a duplicate approved candidate")
    validated = []
    for reviewed in approved:
        current = current_by_id.get(reviewed.get("candidate_id"))
        if current is None or current["user_id"] != reviewed.get("user_id"):
            raise CommandError("Reviewed report contains an unknown membership candidate")
        if current["evidence_fingerprint"] != reviewed.get("evidence_fingerprint"):
            raise CommandError(f"Evidence changed for candidate {current['candidate_id']}")
        if current["issues"]:
            raise CommandError(
                f"Candidate {current['candidate_id']} has unresolved issues: "
                + ", ".join(current["issues"])
            )
        role_slugs = reviewed.get("role_slugs") or []
        if not isinstance(role_slugs, list) or any(
            not isinstance(slug, str) or not slug for slug in role_slugs
        ):
            raise CommandError(f"Candidate {current['candidate_id']} has invalid role_slugs")
        roles = list(
            OrganizationRole.objects.filter(
                organization=organization,
                slug__in=set(role_slugs),
                is_active=True,
            )
        )
        if {role.slug for role in roles} != set(role_slugs):
            raise CommandError(f"Candidate {current['candidate_id']} references an unknown role")
        validated.append((current, roles))

    now = timezone.now()
    memberships_created = 0
    assignments_created = 0
    with transaction.atomic():
        for current, roles in validated:
            membership, created = OrganizationMembership.objects.get_or_create(
                organization=organization,
                user_id=current["user_id"],
                defaults={
                    "joined_at": now,
                    "source": MembershipSource.REVIEWED_BACKFILL,
                    "reviewed_by": reviewer,
                    "reviewed_at": now,
                },
            )
            if not membership.is_effective_at(now):
                raise CommandError(
                    f"Candidate {current['candidate_id']} has an inactive membership"
                )
            if not created:
                membership.reviewed_by = reviewer
                membership.reviewed_at = now
                membership.save(update_fields=("reviewed_by", "reviewed_at", "updated_at"))
            memberships_created += int(created)
            for role in roles:
                active_exists = OrganizationRoleAssignment.objects.filter(
                    membership=membership,
                    role=role,
                    valid_from__lte=now,
                ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now)).exists()
                if not active_exists:
                    OrganizationRoleAssignment.objects.create(
                        membership=membership,
                        role=role,
                        valid_from=now,
                        assigned_by=reviewer,
                        reason="Approved organisational-memory membership backfill",
                    )
                    assignments_created += 1

    return {
        "approved_candidates": len(validated),
        "memberships_created": memberships_created,
        "role_assignments_created": assignments_created,
    }


def load_report(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not read reviewed report: {exc}") from exc
    if not isinstance(value, dict):
        raise CommandError("Reviewed report must be a JSON object")
    return value


def build_identity_resolution_report(organization=None) -> dict:
    identities = OrganizationIdentity.objects.select_related("organization", "user")
    legacy = OrganizationSlackIdentity.objects.select_related(
        "workspace__organization", "user"
    )
    if organization is not None:
        identities = identities.filter(organization=organization)
        legacy = legacy.filter(workspace__organization=organization)

    unresolved = []
    for identity in identities:
        reasons = []
        if identity.user_id is None:
            reasons.append("linked_user_missing")
        if identity.verified_at is None:
            reasons.append("verification_missing")
        if not identity.is_active:
            reasons.append("identity_inactive")
        if reasons:
            unresolved.append(
                {
                    "identity_id": identity.pk,
                    "organization_domain": identity.organization.domain,
                    "provider": identity.provider,
                    "external_tenant_id": identity.external_tenant_id,
                    "external_user_id": identity.external_user_id,
                    "reasons": reasons,
                }
            )

    conflicts = []
    canonical_by_key = {
        (
            identity.organization_id,
            identity.external_tenant_id,
            identity.external_user_id,
        ): identity
        for identity in identities.filter(provider="slack")
    }
    for legacy_identity in legacy:
        key = (
            legacy_identity.workspace.organization_id,
            legacy_identity.workspace.slack_team_id,
            legacy_identity.slack_user_id,
        )
        canonical = canonical_by_key.get(key)
        if canonical is None:
            conflicts.append(
                {
                    "type": "canonical_slack_identity_missing",
                    "legacy_identity_id": legacy_identity.pk,
                    "organization_domain": legacy_identity.workspace.organization.domain,
                    "external_tenant_id": legacy_identity.workspace.slack_team_id,
                    "external_user_id": legacy_identity.slack_user_id,
                }
            )
        elif canonical.user_id != legacy_identity.user_id:
            conflicts.append(
                {
                    "type": "canonical_legacy_user_mismatch",
                    "identity_id": canonical.pk,
                    "legacy_identity_id": legacy_identity.pk,
                    "canonical_user_id": canonical.user_id,
                    "legacy_user_id": legacy_identity.user_id,
                }
            )

    duplicate_emails = list(
        identities.exclude(email_at_link_time="")
        .values("organization_id", "provider", "external_tenant_id", "email_at_link_time")
        .annotate(count=Count("id"), linked_users=Count("user", distinct=True))
        .filter(count__gt=1, linked_users__gt=1)
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": timezone.now().isoformat(),
        "organization_domain": organization.domain if organization else None,
        "unresolved": unresolved,
        "conflicts": conflicts,
        "duplicate_email_links": duplicate_emails,
    }
