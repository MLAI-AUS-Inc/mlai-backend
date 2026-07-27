from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from org_memory.connectors.linear import LinearArtifactMemoryConnector
from org_memory.connectors.registry import MetadataOnlyMemoryConnector, connector_registry
from org_memory.connectors.slack import SlackArtifactMemoryConnector
from org_memory.models import (
    MemoryConnectionConfiguration,
    MemoryConnectionState,
    MemoryScopeStatus,
    MemorySource,
    MemorySourceLifecycle,
    MemorySourceScope,
)
from org_memory.runtime import _apply_removal, _capture_record
from startup_updates.models import (
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
    SlackChannelSelection,
    SlackThreadArtifact,
)


@override_settings(
    ORG_MEMORY_ARTIFACT_PAGE_SIZE=100,
    ORG_MEMORY_SLACK_THREAD_QUIET_SECONDS=900,
    ORG_MEMORY_SLACK_CHUNK_TARGET_CHARS=500,
)
class ArtifactMemoryConnectorTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Adapter Org",
            domain="adapters.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="adapters@mlai.test")

    def _configuration(self, provider, account_id, scope_type, scope_id):
        connection = ExternalServiceConnection.objects.create(
            provider=provider,
            user=self.user,
            organization=self.organization,
            external_account_id=account_id,
            account_label=f"Adapter {provider}",
        )
        configuration = MemoryConnectionConfiguration.objects.create(
            organization=self.organization,
            provider=provider,
            external_connection=connection,
            lifecycle_state=MemoryConnectionState.ACTIVE,
            created_by=self.user,
        )
        scope = MemorySourceScope.objects.create(
            configuration=configuration,
            scope_type=scope_type,
            external_id=scope_id,
            name=scope_id,
            selected=True,
            status=MemoryScopeStatus.SELECTED,
            default_classification="committee",
        )
        return connection, configuration, scope

    def _linear_artifacts(self, connection, *, private=False):
        LinearProjectSelection.objects.create(
            connection=connection,
            user=self.user,
            organization=self.organization,
            linear_project_id="project-1",
            project_name="Launch",
            selected=True,
        )
        project = LinearProjectArtifact.objects.create(
            organization=self.organization,
            connection=connection,
            linear_project_id="project-1",
            name="Launch",
            description="Launch the member product.",
            status_name="In progress",
            health="onTrack",
            url="https://linear.app/mlai/project/project-1",
            raw_payload={"private": private},
        )
        issue = LinearIssueArtifact.objects.create(
            organization=self.organization,
            connection=connection,
            project=project,
            linear_issue_id="issue-1",
            identifier="MLAI-1",
            title="Ship the launch",
            description="Complete the production rollout.",
            state_name="Started",
            assignee_name="Sam",
            updated_at_linear=timezone.now() - timedelta(hours=1),
            url="https://linear.app/mlai/issue/MLAI-1",
        )
        update = LinearProjectUpdateArtifact.objects.create(
            organization=self.organization,
            connection=connection,
            project=project,
            linear_project_update_id="update-1",
            body="Launch remains on track.",
            health="onTrack",
            updated_at_linear=timezone.now() - timedelta(minutes=30),
        )
        return project, issue, update

    def test_registry_installs_real_linear_and_slack_adapters(self):
        self.assertIsInstance(connector_registry.get("linear"), LinearArtifactMemoryConnector)
        self.assertIsInstance(connector_registry.get("slack"), SlackArtifactMemoryConnector)
        self.assertNotIsInstance(connector_registry.get("linear"), MetadataOnlyMemoryConnector)
        self.assertEqual(connector_registry.validate_conformance("linear"), [])
        self.assertEqual(connector_registry.validate_conformance("slack"), [])

    def test_linear_backfill_versions_acl_reconciliation_and_deletion(self):
        connection, configuration, scope = self._configuration(
            "linear", "linear-workspace", "project", "project-1"
        )
        _project, issue, _update = self._linear_artifacts(connection)
        connector = connector_registry.get("linear")

        preview = connector.preview(configuration, [scope], None)
        dry_run = connector.dry_run(configuration, [scope], None)
        self.assertEqual(preview.summary["record_count"], 3)
        self.assertFalse(dry_run.summary["active_memory_created"])
        self.assertFalse(MemorySource.objects.exists())

        records = []
        checkpoint = {}
        while True:
            page = connector.backfill(configuration, [scope], checkpoint)
            records.extend(page.records)
            if not page.has_more:
                break
            checkpoint = page.checkpoint
        self.assertFalse(page.has_more)
        self.assertEqual(len(records), 3)
        for record in records:
            _capture_record(configuration, record)

        issue_source = MemorySource.objects.get(source_type="linear_issue")
        self.assertEqual(issue_source.versions.count(), 1)
        self.assertEqual(
            issue_source.current_version.metadata["authority_fields"],
            ["issue_status", "issue_assignee", "issue_priority"],
        )
        self.assertNotIn("raw_payload", issue_source.current_version.metadata)

        unchanged = connector.incremental_sync(configuration, page.next_cursor)
        self.assertEqual(unchanged.records, ())

        issue.title = "Ship the launch safely"
        issue.save(update_fields=("title", "updated_at"))
        changed = connector.incremental_sync(configuration, page.next_cursor)
        self.assertEqual(len(changed.records), 1)
        _capture_record(configuration, changed.records[0])
        issue_source.refresh_from_db()
        self.assertEqual(issue_source.versions.count(), 2)
        self.assertIn("safely", issue_source.current_version.bounded_excerpt)

        connection.status = "disconnected"
        connection.save(update_fields=("status", "updated_at"))
        checkpoint = {}
        while True:
            permission_page = connector.refresh_permissions(configuration, checkpoint)
            for record in permission_page.records:
                _capture_record(configuration, record)
            if not permission_page.has_more:
                break
            checkpoint = permission_page.checkpoint
        issue_source.refresh_from_db()
        self.assertEqual(issue_source.lifecycle_state, MemorySourceLifecycle.ACCESS_REVOKED)
        self.assertFalse(issue_source.current_version.acl_snapshot.is_accessible)
        self.assertFalse(issue_source.current_version.chunks.filter(active_for_retrieval=True).exists())

        connection.status = "connected"
        connection.save(update_fields=("status", "updated_at"))
        checkpoint = {"mode": "permission_refresh", "completed": True}
        while True:
            restored_page = connector.refresh_permissions(configuration, checkpoint)
            for record in restored_page.records:
                _capture_record(configuration, record)
            if not restored_page.has_more:
                break
            checkpoint = restored_page.checkpoint
        issue_source.refresh_from_db()
        self.assertEqual(issue_source.lifecycle_state, MemorySourceLifecycle.ACTIVE)
        self.assertTrue(issue_source.current_version.acl_snapshot.is_accessible)

        issue.delete()
        deletion_page = connector.incremental_sync(configuration, changed.next_cursor)
        removal = next(
            row for row in deletion_page.removals if row["external_id"] == "linear_issue:issue-1"
        )
        self.assertEqual(_apply_removal(configuration, removal), 1)
        issue_source.refresh_from_db()
        self.assertEqual(issue_source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)

    def test_private_linear_project_excludes_children(self):
        connection, configuration, scope = self._configuration(
            "linear", "linear-private", "project", "project-1"
        )
        self._linear_artifacts(connection, private=True)
        page = connector_registry.get("linear").backfill(configuration, [scope], {})
        self.assertEqual(page.records, ())

    def test_linear_private_transition_revokes_and_public_transition_restores(self):
        connection, configuration, scope = self._configuration(
            "linear", "linear-visibility", "project", "project-1"
        )
        project, _issue, _update = self._linear_artifacts(connection)
        connector = connector_registry.get("linear")
        checkpoint = {}
        while True:
            page = connector.backfill(configuration, [scope], checkpoint)
            for record in page.records:
                _capture_record(configuration, record)
            if not page.has_more:
                break
            checkpoint = page.checkpoint

        project.raw_payload = {"private": True}
        project.save(update_fields=("raw_payload", "updated_at"))
        private_page = connector.incremental_sync(configuration, page.next_cursor)
        self.assertEqual(private_page.records, ())
        self.assertEqual(len(private_page.removals), 3)
        for removal in private_page.removals:
            _apply_removal(configuration, removal)
        self.assertEqual(
            MemorySource.objects.filter(
                configuration=configuration,
                lifecycle_state=MemorySourceLifecycle.ACCESS_REVOKED,
            ).count(),
            3,
        )
        self.assertFalse(
            MemorySource.objects.filter(
                configuration=configuration,
                lifecycle_state=MemorySourceLifecycle.TOMBSTONED,
            ).exists()
        )

        project.raw_payload = {"private": False}
        project.save(update_fields=("raw_payload", "updated_at"))
        public_page = connector.incremental_sync(configuration, private_page.next_cursor)
        self.assertEqual(len(public_page.records), 3)
        for record in public_page.records:
            _capture_record(configuration, record)
        self.assertEqual(
            MemorySource.objects.filter(
                configuration=configuration,
                lifecycle_state=MemorySourceLifecycle.ACTIVE,
            ).count(),
            3,
        )

    def test_slack_selected_channel_quiet_period_chunks_and_reconciliation(self):
        connection, configuration, scope = self._configuration(
            "slack", "T-ADAPTER", "channel", "C-SELECTED"
        )
        SlackChannelSelection.objects.create(
            connection=connection,
            user=self.user,
            organization=self.organization,
            channel_id="C-SELECTED",
            channel_name="leadership",
            selected=True,
        )
        SlackChannelSelection.objects.create(
            connection=connection,
            user=self.user,
            organization=self.organization,
            channel_id="G-MPIM",
            channel_name="group-direct-message",
            selected=True,
            raw_payload={"is_mpim": True},
        )
        SlackChannelSelection.objects.create(
            connection=connection,
            user=self.user,
            organization=self.organization,
            channel_id="D-DIRECT",
            channel_name="direct-message",
            selected=True,
        )
        first_time = timezone.now() - timedelta(hours=2)
        thread = SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=connection,
            channel_id="C-SELECTED",
            channel_name="leadership",
            thread_ts="1770000000.000100",
            source_message_ids=["m1", "m2"],
            source_message_count=2,
            cleaned_text="Leadership launch discussion",
            participant_summary={"participants": ["U1", "U2"]},
            message_payloads=[
                {
                    "message_id": "m1",
                    "author_id": "U1",
                    "author_name": "Alex",
                    "posted_at": first_time.isoformat(),
                    "cleaned_text": "A" * 360,
                },
                {
                    "message_id": "m2",
                    "author_id": "U2",
                    "author_name": "Sam",
                    "posted_at": (first_time + timedelta(minutes=5)).isoformat(),
                    "cleaned_text": "B" * 360,
                },
            ],
            latest_message_at=first_time + timedelta(minutes=5),
        )
        connector = connector_registry.get("slack")
        discovered = connector.discover_scopes(configuration)
        self.assertEqual([row.external_id for row in discovered.scopes], ["C-SELECTED"])

        page = connector.backfill(configuration, [scope], {})
        self.assertEqual(len(page.records), 1)
        self.assertEqual(len(page.records[0]["chunks"]), 2)
        locator = page.records[0]["chunks"][0]["source_locator"]
        self.assertEqual(locator["channel_id"], "C-SELECTED")
        self.assertTrue(locator["start_occurred_at"])
        self.assertTrue(locator["end_occurred_at"])
        _capture_record(configuration, page.records[0])
        source = MemorySource.objects.get(source_type="slack_thread")
        self.assertEqual(source.current_version.metadata["authority_fields"][0], "informal_context")
        self.assertNotIn("message_payloads", source.current_version.metadata)

        thread.latest_message_at = timezone.now()
        thread.cleaned_text = "An active edit that should wait."
        thread.save(update_fields=("latest_message_at", "cleaned_text", "updated_at"))
        active_page = connector.incremental_sync(configuration, page.next_cursor)
        self.assertEqual(active_page.records, ())
        self.assertFalse(
            any(row["external_id"] == source.external_id for row in active_page.removals)
        )

        thread.delete()
        deletion_page = connector.incremental_sync(configuration, active_page.next_cursor)
        removal = next(
            row for row in deletion_page.removals if row["external_id"] == source.external_id
        )
        _apply_removal(configuration, removal)
        source.refresh_from_db()
        self.assertEqual(source.lifecycle_state, MemorySourceLifecycle.TOMBSTONED)

    def test_slack_dm_scope_is_rejected(self):
        _connection, configuration, dm_scope = self._configuration(
            "slack", "T-DM", "channel", "D12345"
        )
        with self.assertRaisesMessage(ValueError, "direct-message"):
            connector_registry.get("slack").preview(configuration, [dm_scope], None)
