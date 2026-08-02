import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from organizations.models import Organization
from org_memory.models import (
    MemoryEntity,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationMembership,
)


class MalformedEntityRepairTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Entity Repair",
            domain="entity-repair.mlai.test",
        )
        self.operator = get_user_model().objects.create_user(
            email="entity-repair-operator@mlai.test"
        )
        membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.operator,
        )
        OrganizationCapabilityGrant.objects.create(
            membership=membership,
            capability=OrganizationCapability.objects.get(key="manage_sources"),
        )
        self.entity = MemoryEntity.objects.create(
            organization=self.organization,
            entity_type="project",
            canonical_name="R",
            normalized_name="r",
            resolved_key="malformed-project-r",
            metadata={"resolution": "deterministic_scoped_key"},
        )

    def command(self, **kwargs):
        output = io.StringIO()
        call_command(
            "repair_malformed_memory_entity",
            organization_domain=self.organization.domain,
            entity_id=str(self.entity.pk),
            operation_id="repair-project-r-20260802",
            stdout=output,
            **kwargs,
        )
        return json.loads(output.getvalue())

    def test_preview_is_read_only_and_apply_can_be_restored(self):
        preview = self.command()
        self.assertEqual(preview["mode"], "preview")
        self.assertFalse(preview["changed"])
        self.entity.refresh_from_db()
        self.assertNotIn("retrieval_quarantined", self.entity.metadata)

        applied = self.command(
            apply=True,
            reason="One-character extraction artifact.",
            operator_email=self.operator.email,
        )
        self.assertTrue(applied["changed"])
        self.entity.refresh_from_db()
        self.assertIs(self.entity.metadata["retrieval_quarantined"], True)
        self.assertEqual(
            self.entity.metadata["malformed_entity_repair"]["previous_metadata"],
            {"resolution": "deterministic_scoped_key"},
        )

        restored = self.command(
            restore=True,
            operator_email=self.operator.email,
        )
        self.assertTrue(restored["changed"])
        self.entity.refresh_from_db()
        self.assertEqual(
            self.entity.metadata,
            {"resolution": "deterministic_scoped_key"},
        )
