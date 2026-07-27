from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class OrganizationAuthorizationMigrationTests(TransactionTestCase):
    migrate_from = [("org_memory", "0001_service_identity")]
    migrate_to = [("org_memory", "0002_organization_authorization")]
    migrate_latest = [
        (
            "org_memory",
            "0021_memory_selector_shadow",
        )
    ]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        Organization = old_apps.get_model("organizations", "Organization")
        Workspace = old_apps.get_model("org_memory", "OrganizationSlackWorkspace")
        SlackIdentity = old_apps.get_model("org_memory", "OrganizationSlackIdentity")

        organization = Organization.objects.create(name="Migration", domain="migration.test")
        user = get_user_model().objects.create_user(email="migration@mlai.test")
        workspace = Workspace.objects.create(
            organization=organization,
            slack_team_id="TMIGRATION1",
        )
        SlackIdentity.objects.create(
            workspace=workspace,
            slack_user_id="UMIGRATION1",
            user_id=user.pk,
        )
        SlackIdentity.objects.create(
            workspace=workspace,
            slack_user_id="UMIGRATION2",
            user_id=user.pk,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.migrate_latest)
        super().tearDown()

    def test_initial_capabilities_are_seeded_and_duplicate_legacy_users_fail_closed(self):
        Capability = self.apps.get_model("org_memory", "OrganizationCapability")
        Identity = self.apps.get_model("org_memory", "OrganizationIdentity")

        self.assertEqual(Capability.objects.count(), 9)
        identities = Identity.objects.filter(
            provider="slack",
            external_tenant_id="TMIGRATION1",
        )
        self.assertEqual(identities.count(), 2)
        self.assertEqual(identities.filter(user__isnull=False).count(), 1)
        duplicate = identities.get(user__isnull=True)
        self.assertIsNone(duplicate.verified_at)
