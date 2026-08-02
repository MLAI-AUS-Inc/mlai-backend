import io
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.models import (
    MemoryActionType,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryProviderEnablement,
    MemoryScopeStatus,
    MemorySourceActionRequest,
    MemorySourceScope,
    OrganizationMembership,
)


class RequestOrgMemoryReprocessCommandTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Reprocess Command",
            domain="reprocess-command.mlai.test",
        )
        self.user = get_user_model().objects.create_user(
            email="operator@reprocess-command.mlai.test"
        )
        OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.organization,
            external_account_id="workspace-1",
        )
        self.configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider="linear",
            external_connection=connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
        )
        MemorySourceScope.objects.create(
            configuration=self.configuration,
            scope_type="project",
            external_id="project-1",
            selected=True,
            status=MemoryScopeStatus.SELECTED,
        )
        MemoryProviderEnablement.objects.create(
            organization=self.organization,
            provider="linear",
            is_enabled=True,
            approved_by=self.user,
            approved_at=timezone.now(),
        )

    def invoke(self, *, apply=False):
        output = io.StringIO()
        arguments = {
            "organization_domain": self.organization.domain,
            "provider": "linear",
            "configuration_id": str(self.configuration.pk),
            "idempotency_key": "parser-v2-extraction-v2",
            "stdout": output,
        }
        if apply:
            arguments.update(operator_email=self.user.email, apply=True)
        with patch.dict(os.environ, {"ORG_MEMORY_ENABLED_PROVIDERS": "linear"}):
            call_command("request_org_memory_reprocess", **arguments)
        return json.loads(output.getvalue())

    def test_preview_is_content_free_and_does_not_request_action(self):
        result = self.invoke()

        self.assertFalse(result["apply"])
        self.assertEqual(result["selected_scope_count"], 1)
        self.assertIsNone(result["action_id"])
        self.assertFalse(MemorySourceActionRequest.objects.exists())

    def test_apply_is_audited_and_idempotent(self):
        first = self.invoke(apply=True)
        second = self.invoke(apply=True)
        action = MemorySourceActionRequest.objects.get()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["action_id"], second["action_id"])
        self.assertEqual(action.action, MemoryActionType.REPROCESS)
        self.assertEqual(action.requested_by, self.user)
        self.assertEqual(action.scope_external_ids, [])

    def test_apply_requires_active_operator_membership(self):
        OrganizationMembership.objects.all().delete()

        with self.assertRaisesMessage(CommandError, "active member"):
            self.invoke(apply=True)
