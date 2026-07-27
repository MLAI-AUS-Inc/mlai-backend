import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from organizations.models import Organization
from org_memory.authorization import (
    OrganizationAuthorizationError,
    actor_may_view_memory_class,
    resolve_actor_authorization,
)
from org_memory.membership_backfill import (
    build_identity_resolution_report,
    build_membership_candidate_report,
)
from org_memory.models import (
    CapabilityGrantEffect,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackIdentity,
    OrganizationSlackWorkspace,
)
from roo.models import PointsAdmin
from startup_updates.models import UserStartupBinding


class OrganizationAuthorizationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.test")
        self.other_organization = Organization.objects.create(name="Other", domain="other.test")
        self.user = get_user_model().objects.create_user(email="admin@mlai.test")
        self.identity = OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider="slack",
            external_tenant_id="TMLAI123",
            external_user_id="UADMIN123",
            email_at_link_time=self.user.email,
            verified_at=timezone.now(),
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        self.actor = SimpleNamespace(
            organization=self.organization,
            identity=self.identity,
            user=self.user,
        )
        self.general = OrganizationCapability.objects.get(key="view_general_memory")
        self.finance = OrganizationCapability.objects.get(key="view_finance_memory")

    def _role_with_grant(self, slug, capability, effect=CapabilityGrantEffect.ALLOW):
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug=slug,
            name=slug,
        )
        OrganizationRoleAssignment.objects.create(membership=self.membership, role=role)
        OrganizationCapabilityGrant.objects.create(
            role=role,
            capability=capability,
            effect=effect,
        )
        return role

    def test_overlapping_roles_accumulate_capabilities_and_explicit_deny_wins(self):
        self._role_with_grant("general-reader", self.general)
        self._role_with_grant("finance-reader", self.finance)
        deny_role = self._role_with_grant(
            "finance-denied",
            self.finance,
            CapabilityGrantEffect.DENY,
        )

        authorization = resolve_actor_authorization(self.actor)

        self.assertTrue(authorization.has_capability("view_general_memory"))
        self.assertFalse(authorization.has_capability("view_finance_memory"))
        self.assertEqual(
            authorization.role_slugs,
            ("finance-denied", "finance-reader", "general-reader"),
        )
        self.assertIn("view_finance_memory", authorization.denied_capabilities)

        OrganizationRoleAssignment.objects.filter(role=deny_role).update(
            valid_until=timezone.now()
        )
        self.assertTrue(
            resolve_actor_authorization(self.actor).has_capability("view_finance_memory")
        )

    def test_inactive_and_expired_memberships_fail_closed(self):
        self._role_with_grant("general-reader", self.general)
        self.membership.ended_at = timezone.now()
        self.membership.is_active = False
        self.membership.save(update_fields=("ended_at", "is_active"))

        with self.assertRaisesMessage(OrganizationAuthorizationError, "inactive or missing"):
            resolve_actor_authorization(self.actor)

    def test_identity_must_be_verified_active_and_in_the_same_organization(self):
        self._role_with_grant("general-reader", self.general)
        self.identity.verified_at = None
        self.identity.save(update_fields=("verified_at",))
        with self.assertRaisesMessage(OrganizationAuthorizationError, "inactive or unresolved"):
            resolve_actor_authorization(self.actor)

        self.identity.verified_at = timezone.now()
        self.identity.organization = self.other_organization
        with self.assertRaisesMessage(OrganizationAuthorizationError, "does not match"):
            resolve_actor_authorization(self.actor)

    def test_memory_classes_are_deterministic_and_no_agent_is_never_allowed(self):
        self._role_with_grant("general-reader", self.general)
        self._role_with_grant("finance-reader", self.finance)

        self.assertTrue(actor_may_view_memory_class(self.actor, "internal"))
        self.assertTrue(actor_may_view_memory_class(self.actor, "committee"))
        self.assertTrue(actor_may_view_memory_class(self.actor, "finance"))
        self.assertFalse(actor_may_view_memory_class(self.actor, "executive"))
        self.assertFalse(actor_may_view_memory_class(self.actor, "no_agent"))
        self.assertFalse(actor_may_view_memory_class(self.actor, "invented"))

    def test_direct_membership_deny_overrides_role_allow(self):
        self._role_with_grant("general-reader", self.general)
        OrganizationCapabilityGrant.objects.create(
            membership=self.membership,
            capability=self.general,
            effect=CapabilityGrantEffect.DENY,
        )

        self.assertFalse(
            resolve_actor_authorization(self.actor).has_capability("view_general_memory")
        )

    def test_cross_organization_role_rows_are_ignored_fail_closed(self):
        foreign_role = OrganizationRole.objects.create(
            organization=self.other_organization,
            slug="foreign-reader",
            name="Foreign reader",
        )
        # Bypass model validation to prove authorization still filters bad rows.
        OrganizationRoleAssignment.objects.create(
            membership=self.membership,
            role=foreign_role,
        )
        OrganizationCapabilityGrant.objects.create(
            role=foreign_role,
            capability=self.general,
        )

        self.assertFalse(
            resolve_actor_authorization(self.actor).has_capability("view_general_memory")
        )


class MembershipBackfillTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.test")
        self.user = get_user_model().objects.create_user(email="candidate@mlai.test")
        self.reviewer = get_user_model().objects.create_user(
            email="reviewer@mlai.test",
            is_staff=True,
        )
        self.workspace = OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TMLAI123",
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider="slack",
            external_tenant_id="TMLAI123",
            external_user_id="UCANDIDATE1",
            email_at_link_time=self.user.email,
            verified_at=timezone.now(),
        )
        UserStartupBinding.objects.create(
            organization=self.organization,
            user=self.user,
            role="founder",
        )
        PointsAdmin.objects.create(
            user=self.user,
            slack_user_id="UCANDIDATE1",
            role="admin",
        )
        self.role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="memory-reader",
            name="Memory reader",
        )

    def test_candidate_report_uses_points_admin_only_as_supporting_tenant_evidence(self):
        unlinked = PointsAdmin.objects.create(
            slack_user_id="UUNLINKED1",
            role="committee",
        )

        report = build_membership_candidate_report(self.organization)

        self.assertEqual(len(report["candidates"]), 1)
        sources = {row["source"] for row in report["candidates"][0]["evidence"]}
        self.assertEqual(
            sources,
            {"organization_identity", "points_admin", "user_startup_binding"},
        )
        self.assertTrue(
            any(
                row.get("record_id") == unlinked.pk
                and row["reason"] == "points_admin_has_no_linked_user"
                for row in report["unresolved"]
            )
        )
        self.assertFalse(OrganizationMembership.objects.exists())

    def test_reviewed_command_creates_only_explicitly_approved_membership_and_role(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "membership-review.json"
            call_command(
                "backfill_org_memory_memberships",
                organization_domain=self.organization.domain,
                output=str(report_path),
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["candidates"][0]["approved"] = True
            report["candidates"][0]["role_slugs"] = [self.role.slug]
            report_path.write_text(json.dumps(report), encoding="utf-8")

            call_command(
                "backfill_org_memory_memberships",
                organization_domain=self.organization.domain,
                reviewed_input=str(report_path),
                reviewed_by=self.reviewer.email,
            )

        membership = OrganizationMembership.objects.get(
            organization=self.organization,
            user=self.user,
        )
        self.assertEqual(membership.reviewed_by, self.reviewer)
        self.assertEqual(membership.source, "reviewed_backfill")
        self.assertTrue(
            OrganizationRoleAssignment.objects.filter(
                membership=membership,
                role=self.role,
            ).exists()
        )

    def test_reviewed_command_rejects_stale_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "membership-review.json"
            call_command(
                "backfill_org_memory_memberships",
                organization_domain=self.organization.domain,
                output=str(report_path),
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["candidates"][0]["approved"] = True
            report_path.write_text(json.dumps(report), encoding="utf-8")
            UserStartupBinding.objects.filter(user=self.user).update(role="changed")

            with self.assertRaisesMessage(CommandError, "Evidence changed"):
                call_command(
                    "backfill_org_memory_memberships",
                    organization_domain=self.organization.domain,
                    reviewed_input=str(report_path),
                    reviewed_by=self.reviewer.email,
                )

        self.assertFalse(OrganizationMembership.objects.exists())

    def test_identity_report_surfaces_unresolved_and_legacy_conflicts(self):
        other_user = get_user_model().objects.create_user(email="other@mlai.test")
        unresolved = OrganizationIdentity.objects.create(
            organization=self.organization,
            provider="google",
            external_tenant_id="google-account",
            external_user_id="unresolved@example.test",
        )
        legacy = OrganizationSlackIdentity.objects.create(
            workspace=self.workspace,
            slack_user_id="UCANDIDATE1",
            user=other_user,
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            provider="google",
            external_tenant_id="google-tenant",
            external_user_id="google-user-one",
            email_at_link_time="duplicate@mlai.test",
            user=self.user,
            verified_at=timezone.now(),
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            provider="google",
            external_tenant_id="google-tenant",
            external_user_id="google-user-two",
            email_at_link_time="duplicate@mlai.test",
            user=other_user,
            verified_at=timezone.now(),
        )

        report = build_identity_resolution_report(self.organization)

        self.assertTrue(
            any(row["identity_id"] == unresolved.pk for row in report["unresolved"])
        )
        self.assertTrue(
            any(
                row.get("legacy_identity_id") == legacy.pk
                and row["type"] == "canonical_legacy_user_mismatch"
                for row in report["conflicts"]
            )
        )
        self.assertEqual(report["duplicate_email_links"][0]["linked_users"], 2)
