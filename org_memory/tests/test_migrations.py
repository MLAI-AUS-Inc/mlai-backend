from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag


# Rewinding the org_memory graph to 0001 and replaying it costs ~116s, which
# was more than the other ~1,050 tests in the `checks` job combined. Measured
# on both engines, that cost is the number of migration operations rather than
# the engine — PostgreSQL is no cheaper than SQLite here — so it cannot be
# optimised away by relocating it. It gets its own parallel CI job instead, on
# PostgreSQL because that is the engine the migration runs on in production.
# The tag keeps it out of the SQLite `checks` job.
@tag("postgres-only")
class OrganizationAuthorizationMigrationTests(TransactionTestCase):
    migrate_from = [("org_memory", "0001_service_identity")]
    migrate_to = [("org_memory", "0002_organization_authorization")]

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
        # Rewinding org_memory to 0001 also unapplies migrations in the apps
        # that depend on it, so the restore has to target every leaf in the
        # graph rather than org_memory alone. Reading the leaves from the
        # loader also keeps this from silently going stale: it was pinned to
        # org_memory.0021 while the app had moved on to 0024, which left the
        # database three migrations behind for anything running afterwards.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
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
