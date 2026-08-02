import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    ReconciliationMapping,
    ReconciliationPartyIdentity,
    ReconciliationProfile,
    ReconciliationRule,
)
from integrations.services.reconciliation_knowledge import (
    KNOWLEDGE_POLICY_VERSION,
    build_reconciliation_knowledge_export,
)
from organizations.models import Organization
from roo.models import PointsAdmin
from startup_updates.models import (
    LinearProjectArtifact,
    LinearProjectMemberArtifact,
    LinearProjectSelection,
    LumaEventSelection,
)


User = get_user_model()


class ReconciliationKnowledgeExportTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.user = User.objects.create_user(email="agent@example.com", slack_id="UAGENT")
        PointsAdmin.objects.create(slack_user_id="UADMIN", role="admin", is_active=True)
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LINEAR,
            user=self.user,
            organization=self.organization,
            access_token="NEVER_EXPORT_CONNECTOR_TOKEN",
            external_account_id="linear-workspace",
        )
        self.luma_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.LUMA,
            user=self.user,
            organization=self.organization,
            access_token="NEVER_EXPORT_LUMA_TOKEN",
            external_account_id="luma-account",
        )

        ReconciliationProfile.objects.create(
            organization=self.organization,
            xero_bank_account_id="NEVER_EXPORT_BANK_ACCOUNT",
            xero_bank_account_name="NEVER_EXPORT_BANK_NAME",
            revenue_account_code="200",
            revenue_tax_type="OUTPUT2",
            fee_account_code="404",
            fee_tax_type="INPUT2",
            event_tracking_category_id="event-category",
            event_tracking_category_name="Event Name",
            project_tracking_category_id="project-category",
            project_tracking_category_name="Project Name",
        )
        ReconciliationMapping.objects.create(
            organization=self.organization,
            source_type=ReconciliationMapping.SOURCE_LUMA_EVENT,
            source_id="luma-event-1",
            source_label="Pitch Night",
            accounting_treatment=ReconciliationMapping.TREATMENT_REVENUE,
            event_tracking_option_id="event-option-1",
            event_tracking_option_name="Pitch Night",
            account_code="200",
            tax_type="OUTPUT2",
            reconciliation_note="NEVER_EXPORT_PRIVATE_NOTE",
        )
        ReconciliationPartyIdentity.objects.create(
            organization=self.organization,
            bank_narration_key="TRANSFER TO SANSOMI",
            direction="debit",
            canonical_name="Sansoni Management",
            xero_contact_id="contact-sansoni",
            xero_contact_name="Sansoni Management",
            linear_user_id="linear-sansoni",
            linear_name="Sansoni",
            linear_email="NEVER_EXPORT_IDENTITY_EMAIL@example.com",
            status=ReconciliationPartyIdentity.STATUS_VERIFIED,
            confidence=1.0,
            verified_by_slack_id="UADMIN",
            verified_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
            active=True,
            notes="NEVER_EXPORT_IDENTITY_NOTE",
        )
        ReconciliationRule.objects.create(
            organization=self.organization,
            name="Sansoni contractor payments",
            scope=ReconciliationRule.SCOPE_MERCHANT,
            bank_narration_key="TRANSFER TO SANSOMI",
            direction="debit",
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc).date(),
            proposed_action=ReconciliationRule.ACTION_CREATE_BANK_TRANSACTION,
            contact_name="Sansoni Management",
            account_code="405",
            account_name="Contractor Expenses",
            tax_type="INPUT",
            description_template="Contractor work for {project}",
            project_source_id="linear-project-1",
            project_tracking_option_name="Studio Operations",
            priority=100,
            status=ReconciliationRule.STATUS_VERIFIED,
            active=True,
            evidence=[{"raw_message": "NEVER_EXPORT_RULE_EVIDENCE"}],
            notes="NEVER_EXPORT_RULE_NOTE",
            verified_by_slack_id="UADMIN",
            verified_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
        LumaEventSelection.objects.create(
            connection=self.luma_connection,
            user=self.user,
            organization=self.organization,
            event_id="luma-event-1",
            event_name="Pitch Night",
            event_url="https://example.invalid/NEVER_EXPORT_EVENT_URL",
            selected=True,
            raw_payload={"secret": "NEVER_EXPORT_LUMA_RAW"},
        )
        LinearProjectSelection.objects.create(
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            linear_project_id="linear-project-1",
            project_name="Studio Operations",
            selected=True,
            raw_payload={"secret": "NEVER_EXPORT_LINEAR_RAW"},
        )
        project = LinearProjectArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            linear_project_id="linear-project-1",
            name="Studio Operations",
            description="NEVER_EXPORT_PROJECT_DESCRIPTION",
            lead_email="NEVER_EXPORT_LEAD_EMAIL@example.com",
            raw_payload={"secret": "NEVER_EXPORT_PROJECT_RAW"},
        )
        LinearProjectMemberArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            project=project,
            linear_user_id="linear-sansoni",
            name="Sansoni",
            email="NEVER_EXPORT_MEMBER_EMAIL@example.com",
            raw_payload={"secret": "NEVER_EXPORT_MEMBER_RAW"},
            active=True,
        )

    @patch("integrations.services.reconciliation_knowledge.build_learning_candidates", return_value=[])
    def test_export_is_stable_sanitized_and_self_verifying(self, _candidates):
        first = build_reconciliation_knowledge_export(
            organization=self.organization,
            fetched_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        second = build_reconciliation_knowledge_export(
            organization=self.organization,
            fetched_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(first["source_hash"], second["source_hash"])
        self.assertNotEqual(first["exported_at"], second["exported_at"])
        self.assertEqual(first["policy"]["version"], KNOWLEDGE_POLICY_VERSION)
        self.assertEqual(
            first["counts"],
            {name: len(items) for name, items in first["collections"].items()},
        )
        for name, items in first["collections"].items():
            self.assertIn(name, first["policy"]["source_hashes"])
            for item in items:
                self.assertEqual(item["source_backend"], "mlai-backend")
                self.assertTrue(item["record_id"])
                self.assertTrue(item["version"])
                self.assertEqual(item["fetched_at"], first["exported_at"])

        payload = json.dumps(first, sort_keys=True)
        for forbidden in [
            "NEVER_EXPORT_CONNECTOR_TOKEN",
            "NEVER_EXPORT_LUMA_TOKEN",
            "NEVER_EXPORT_BANK_ACCOUNT",
            "NEVER_EXPORT_BANK_NAME",
            "NEVER_EXPORT_PRIVATE_NOTE",
            "NEVER_EXPORT_IDENTITY_EMAIL",
            "NEVER_EXPORT_IDENTITY_NOTE",
            "NEVER_EXPORT_RULE_EVIDENCE",
            "NEVER_EXPORT_RULE_NOTE",
            "NEVER_EXPORT_EVENT_URL",
            "NEVER_EXPORT_LUMA_RAW",
            "NEVER_EXPORT_LINEAR_RAW",
            "NEVER_EXPORT_PROJECT_DESCRIPTION",
            "NEVER_EXPORT_LEAD_EMAIL",
            "NEVER_EXPORT_PROJECT_RAW",
            "NEVER_EXPORT_MEMBER_EMAIL",
            "NEVER_EXPORT_MEMBER_RAW",
        ]:
            self.assertNotIn(forbidden, payload)

        identity = first["collections"]["party_identities"][0]
        self.assertEqual(identity["data"]["canonical_name"], "Sansoni Management")
        self.assertEqual(first["counts"]["approved_accounting_tuples"], 1)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    @patch("integrations.services.reconciliation_knowledge.build_learning_candidates", return_value=[])
    def test_endpoint_requires_points_admin(self, _candidates, _permission):
        denied = self.client.get(
            reverse("reconciliation_knowledge_export"),
            {"slack_user_id": "UNAUTHORIZED", "domain": "mlai.au"},
        )
        self.assertEqual(denied.status_code, status.HTTP_403_FORBIDDEN)

        response = self.client.get(
            reverse("reconciliation_knowledge_export"),
            {"slack_user_id": "UADMIN", "domain": "mlai.au"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["source_backend"], "mlai-backend")
        self.assertEqual(response.data["organization"]["domain"], "mlai.au")
        self.assertEqual(
            response["Allow"],
            "GET, HEAD, OPTIONS",
        )
