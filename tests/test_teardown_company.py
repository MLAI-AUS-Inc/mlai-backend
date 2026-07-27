"""Tests for the ``teardown_company`` management command.

Exercises the full apply path against an isolated test DB: it must clear the
PROTECT subgraph (so the otherwise-blocked Organization.delete() succeeds),
remove SET_NULL rows that a cascade would orphan, sweep domain-keyed residue
that carries no organization FK, and leave the founder's user + profile intact.
"""

from __future__ import annotations

from io import StringIO
import hashlib
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from content_factory.models import (
    AutomationRun,
    ContentFactoryHealingRecord,
    ContentFactoryJob,
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    NotificationDelivery,
    OrganizationContentConfig,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.kernel import capture_source_version
from org_memory.models import (
    AgentActionProposal,
    AgentActionRiskLevel,
    AgentActionStatus,
    AgentActionType,
    MemoryChunk,
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemorySource,
    MemorySourceLifecycle,
    ServicePrincipal,
)
from startup_updates.models import StartupProfile
from workflow_runs.models import ContentFactoryRun

User = get_user_model()

# Both reused helpers reach content-factory over HTTP to cancel runs. We're
# testing the command's orchestration, not those calls, so stub the seams.
CANCEL_SEAM = "startup_updates.data_deletion._cancel_open_startup_runs"
SCAFFOLD_SEAM = "content_factory.vibe_marketing_views._call_content_factory_run_action"


@patch(SCAFFOLD_SEAM, return_value={})
@patch(CANCEL_SEAM, return_value=[])
class TeardownCompanyTests(TestCase):
    DOMAIN = "teardownco.example.com"

    def setUp(self):
        self.org = Organization.objects.create(name="Teardown Co", domain=self.DOMAIN)
        self.config, _ = OrganizationContentConfig.objects.get_or_create(organization=self.org)

        # Founder account + company. The user and profile must SURVIVE teardown
        # (removing a company is not removing a person); the company must go.
        self.user = User.objects.create_user(email="founder@teardownco.example.com")
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Teardown Co",
            domain=self.DOMAIN,
            organization=self.org,
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])

        # PROTECT subgraph: NotificationChannel is referenced by ResearchAutomation
        # (PROTECT) and by NotificationDelivery (PROTECT) via AutomationRun.
        self.channel = NotificationChannel.objects.create(
            organization=self.org,
            channel_type=NotificationChannelType.EMAIL,
            route_id="founder@teardownco.example.com",
            consent_state=NotificationConsentState.ACTIVE,
        )
        self.automation = ResearchAutomation.objects.create(
            organization=self.org,
            notification_channel=self.channel,
            status=ResearchAutomationStatus.ACTIVE,
        )
        self.run = AutomationRun.objects.create(
            automation=self.automation,
            scheduled_for_at=timezone.now(),
            local_date=timezone.now().date(),
            idempotency_key="auto-run-1",
        )
        self.delivery = NotificationDelivery.objects.create(
            automation_run=self.run,
            channel=self.channel,
            event_type="daily_discovery",
            idempotency_key="delivery-1",
        )

        # SET_NULL row a plain org.delete() would orphan instead of remove.
        self.healing = ContentFactoryHealingRecord.objects.create(
            organization=self.org,
            domain=self.DOMAIN,
            failure_kind="build",
            failure_family_key="fam-1",
        )

        # Cross-app CASCADE witness.
        self.startup_profile = StartupProfile.objects.create(organization=self.org)

        # Domain-keyed residue (no organization FK; a cascade never reaches it).
        self.job = ContentFactoryJob.objects.create(
            job_id="job-1", slack_user_id="U1", domain=self.DOMAIN
        )
        self.residue_run = ContentFactoryRun.objects.create(
            run_id="run-1", workflow="discovery", domain=self.DOMAIN
        )

        self.memory_connection = ExternalServiceConnection.objects.create(
            provider="linear",
            user=self.user,
            organization=self.org,
            external_account_id="teardown-linear",
        )
        self.memory_configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.org,
            provider="linear",
            external_connection=self.memory_connection,
            created_by=self.user,
        )
        self.memory_principal = ServicePrincipal.objects.create(
            name="teardown-admin-roo",
            organization=self.org,
            scopes=["source.manage"],
            allowed_surfaces=["admin_roo"],
        )
        self.agent_action = AgentActionProposal.objects.create(
            organization=self.org,
            configuration=self.memory_configuration,
            requested_by=self.user,
            action_type=AgentActionType.CREATE_LINEAR_ISSUE,
            target_system="linear",
            input_payload={"title": "Teardown fixture"},
            input_hash="a" * 64,
            risk_level=AgentActionRiskLevel.MEDIUM,
            requires_approval=True,
            status=AgentActionStatus.AWAITING_APPROVAL,
            idempotency_key="teardown-action-fixture",
            creation_request_hash="b" * 64,
        )
        evidence_text = "This organisation has durable private memory."
        self.memory_source, _version, _created = capture_source_version(
            organization=self.org,
            provider="linear",
            external_account_id="teardown-linear",
            source_type="issue",
            external_id="TEAR-1",
            version_key="v1",
            content_hash=hashlib.sha256(evidence_text.encode("utf-8")).hexdigest(),
            classification="committee",
            acl={"is_accessible": True, "principal_refs": ["user:founder"]},
            chunks=[{"ordinal": 0, "text": evidence_text}],
            configuration=self.memory_configuration,
        )

    def _run(self, **opts):
        buf = StringIO()
        call_command("teardown_company", domain=self.DOMAIN, stdout=buf, stderr=buf, **opts)
        return buf.getvalue()

    # The premise of the whole command: a bare delete is blocked by PROTECT.
    def test_bare_org_delete_raises_protected_error(self, *_mocks):
        with self.assertRaises(ProtectedError):
            self.org.delete()

    def test_dry_run_changes_nothing(self, *_mocks):
        self._run(no_storage=True)  # no --apply
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertTrue(ResearchAutomation.objects.filter(pk=self.automation.pk).exists())
        self.assertTrue(ContentFactoryJob.objects.filter(pk=self.job.pk).exists())
        self.assertTrue(ContentFactoryRun.objects.filter(pk=self.residue_run.pk).exists())

    def test_apply_removes_company_completely(self, *_mocks):
        self._run(apply=True, no_storage=True)

        # Organization + the explicitly-ordered structural rows are gone.
        self.assertFalse(Organization.objects.filter(pk=self.org.pk).exists())
        self.assertFalse(ResearchAutomation.objects.filter(pk=self.automation.pk).exists())
        self.assertFalse(NotificationChannel.objects.filter(pk=self.channel.pk).exists())
        self.assertFalse(AutomationRun.objects.filter(pk=self.run.pk).exists())
        self.assertFalse(NotificationDelivery.objects.filter(pk=self.delivery.pk).exists())
        self.assertFalse(ContentFactoryHealingRecord.objects.filter(pk=self.healing.pk).exists())
        self.assertFalse(VibeRaisingCompany.objects.filter(pk=self.company.pk).exists())

        # Cascade children removed by Organization.delete().
        self.assertFalse(StartupProfile.objects.filter(pk=self.startup_profile.pk).exists())
        self.assertFalse(OrganizationContentConfig.objects.filter(pk=self.config.pk).exists())
        self.assertFalse(MemorySource.objects.filter(pk=self.memory_source.pk).exists())
        self.assertFalse(
            AgentActionProposal.objects.filter(pk=self.agent_action.pk).exists()
        )
        self.assertFalse(
            MemoryConnectionConfiguration.objects.filter(
                pk=self.memory_configuration.pk
            ).exists()
        )
        self.assertFalse(ServicePrincipal.objects.filter(pk=self.memory_principal.pk).exists())

        # Domain-keyed residue swept.
        self.assertFalse(ContentFactoryJob.objects.filter(pk=self.job.pk).exists())
        self.assertFalse(ContentFactoryRun.objects.filter(pk=self.residue_run.pk).exists())

        # Boundary: the founder's account and profile survive.
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(VibeRaisingProfile.objects.filter(pk=self.profile.pk).exists())

    def test_keep_org_resets_but_keeps_org(self, *_mocks):
        self.config.articles_scaffolded = True
        self.config.save(update_fields=["articles_scaffolded"])

        self._run(apply=True, keep_org=True, no_storage=True)

        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
        self.config.refresh_from_db()
        self.assertFalse(self.config.articles_scaffolded)
        # Reset-in-place does not purge automations or residue.
        self.assertTrue(ResearchAutomation.objects.filter(pk=self.automation.pk).exists())
        self.assertTrue(ContentFactoryJob.objects.filter(pk=self.job.pk).exists())
        self.memory_source.refresh_from_db()
        self.assertEqual(
            self.memory_source.lifecycle_state,
            MemorySourceLifecycle.TOMBSTONED,
        )
        self.assertFalse(
            MemoryChunk.objects.filter(
                source_version__source=self.memory_source,
                active_for_retrieval=True,
            ).exists()
        )
        self.memory_configuration.refresh_from_db()
        self.assertEqual(
            self.memory_configuration.lifecycle_state,
            MemoryConnectionState.DELETE_PENDING,
        )
        self.assertTrue(ServicePrincipal.objects.filter(pk=self.memory_principal.pk).exists())
