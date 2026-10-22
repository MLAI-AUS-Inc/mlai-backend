"""Current source policy, retained derivatives and exact-workspace controls."""
from datetime import timedelta
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from data_access.registry import get_resource
from data_access.resolvers import Actor
from integrations.models import CommunityBridgeChannel, ExternalServiceConnection
from integrations.services import external_connectors
from integrations.services.community_bridge.member_channels import rekey_channel
from integrations.services.slack_source_privacy import is_members_source, slack_source_queryset
from organizations.models import Organization
from org_memory.connectors.slack import SlackArtifactMemoryConnector
from org_memory.embeddings import EmbeddingInvariantError, process_embedding_work
from org_memory.kernel import EvidenceKernelError, capture_source_version
from org_memory.models import MemoryConnectionConfiguration, MemoryEvidence, MemorySource, MemorySourceScope
from org_memory.provider_events import schedule_artifact_wake
from org_memory.retrieval import eligible_evidence_queryset
from org_memory.search import search_memory_chunks
from org_memory.slack_privacy import exclude_members_claims, members_source_ids, source_is_members
from org_memory.tests import test_retrieval_answers as retrieval_fixtures
from startup_updates.models import SlackChannelSelection, SlackMessageArtifact, SlackThreadArtifact
from startup_updates.services import build_slack_run_context


def add_retained_member_evidence(claim):
    text = "Synthetic Members evidence"
    source, version, _ = capture_source_version(
        organization=claim.organization, provider="slack", external_account_id="TPRIMARY",
        source_type="slack_thread", external_id="slack_thread:CEXCLUDED:1790000000.000001",
        version_key="v1", content_hash=hashlib.sha256(text.encode()).hexdigest(),
        classification=claim.classification,
        acl={"is_accessible": True, "principal_refs": ["synthetic"]},
        chunks=[{"ordinal": 0, "text": text}], metadata={"channel_id": "CEXCLUDED"},
    )
    evidence = MemoryEvidence.objects.create(
        claim=claim, source=source, source_version=version, chunk=version.chunks.get(),
        quote=text, quote_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    return source, evidence


class MembersSourcePrivacyTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Source policy", domain="source-policy.test")
        self.user = get_user_model().objects.create_user(email="source-policy@mlai.test")
        self.observed_at = timezone.now() - timedelta(hours=2)
        self.connection = self.connection_for("TPRIMARY")

    def connection_for(self, workspace):
        return ExternalServiceConnection.objects.create(
            user=self.user, organization=self.organization, provider="slack",
            external_account_id=workspace, account_label="Synthetic workspace",
        )

    def members_mapping(self, *, channel="CEXCLUDED", destination=""):
        return CommunityBridgeChannel.objects.create(
            slack_workspace_id="TPRIMARY", slack_channel_id=channel,
            destination_platform="buzz", destination_channel_id=destination,
            audience="members", enabled=False, mirror_decision="hold", hold_reason="source_policy",
        )

    def artifact(self, connection, channel):
        SlackChannelSelection.objects.create(
            user=self.user, organization=self.organization, connection=connection,
            channel_id=channel, channel_name=channel, selected=True,
        )
        message = SlackMessageArtifact.objects.create(
            organization=self.organization, connection=connection, channel_id=channel,
            slack_message_ts="1790000000.000001", thread_ts="1790000000.000001",
            posted_at=self.observed_at, text="Synthetic retained body", cleaned_text="Synthetic retained body",
        )
        thread = SlackThreadArtifact.objects.create(
            organization=self.organization, connection=connection, channel_id=channel,
            thread_ts="1790000000.000001", cleaned_text="Synthetic retained body",
            latest_message_at=self.observed_at,
        )
        return message, thread

    def capture(self, channel="CEXCLUDED", *, workspace="TPRIMARY", metadata=None):
        text = "Synthetic confidential roadmap"
        return capture_source_version(
            organization=self.organization, provider="slack", external_account_id=workspace,
            source_type="slack_thread", external_id=f"slack_thread:{channel}:1790000000.000001",
            version_key="v1", content_hash=hashlib.sha256(text.encode()).hexdigest(),
            classification="internal", acl={"is_accessible": True, "principal_refs": ["synthetic"]},
            chunks=[{"ordinal": 0, "text": text}], metadata=metadata or {"channel_id": channel},
        )

    def test_retained_artifacts_metadata_counts_and_context_exclude_only_exact_members_source(self):
        hidden, _ = self.artifact(self.connection, "CEXCLUDED")
        public, _ = self.artifact(self.connection, "CCOMMUNITY")
        foreign, _ = self.artifact(self.connection_for("TOTHER"), "CEXCLUDED")
        self.members_mapping()
        visible = set(slack_source_queryset(SlackMessageArtifact).values_list("pk", flat=True))
        self.assertEqual(visible, {public.pk, foreign.pk})
        context = build_slack_run_context(organization=self.organization, connection=self.connection)
        self.assertEqual(context["selected_channel_ids"], ["CCOMMUNITY"])
        actor = Actor(slack_id="USTAFF", roles=frozenset({"django_staff"}))
        resource = get_resource("slack_messages")
        result = resource.execute(actor, {"operation": "list", "fields": ["id", "channel_id"]})
        self.assertEqual({row["id"] for row in result["rows"]}, {public.pk, foreign.pk})
        self.assertTrue(SlackMessageArtifact.objects.filter(pk=hidden.pk).exists())

    def test_selected_members_scope_discovery_and_preview_are_excluded(self):
        self.artifact(self.connection, "CEXCLUDED")
        config = MemoryConnectionConfiguration.objects.create(
            organization=self.organization, provider="slack", external_connection=self.connection,
        )
        scope = MemorySourceScope.objects.create(
            configuration=config, scope_type="channel", external_id="CEXCLUDED", selected=True, status="selected",
        )
        self.members_mapping()
        connector = SlackArtifactMemoryConnector()
        self.assertEqual(connector.discover_scopes(config).scopes, ())
        with self.assertRaisesMessage(ValueError, "Members"):
            connector.preview(config, [scope], None)

    def test_members_provider_page_and_direct_upsert_never_fetch_or_persist(self):
        self.artifact(self.connection, "CEXCLUDED")
        selection = SlackChannelSelection.objects.get(connection=self.connection, channel_id="CEXCLUDED")
        self.members_mapping()
        with patch.object(external_connectors, "_lock_slack_connection_authority", return_value=self.connection), \
                patch.object(external_connectors, "_slack_api_request") as provider:
            with self.assertRaises(external_connectors.ConnectorConfigurationError):
                external_connectors._slack_api_request_and_persist_messages(
                    object(), selection_id=selection.pk, expected_sync_cursor={},
                    method="conversations.history", params={"channel": "CEXCLUDED"},
                )
        provider.assert_not_called()
        self.assertIsNone(external_connectors._upsert_slack_message(
            connection=self.connection, channel_id="CEXCLUDED", channel_name="",
            message={"ts": "1790000001.000001", "text": "Must not persist"},
        ))
        self.assertEqual(SlackMessageArtifact.objects.count(), 1)

    def test_retained_chunks_search_and_queued_embeddings_are_denied_without_erasure(self):
        source, version, _ = self.capture()
        foreign, _, _ = self.capture(workspace="TOTHER")
        self.members_mapping()
        result = search_memory_chunks(
            organization=self.organization, query="confidential roadmap", generate_vector=False,
        )
        self.assertEqual({hit.chunk.source_version.source_id for hit in result.hits}, {foreign.pk})
        provider = Mock()
        work = SimpleNamespace(payload={"chunk_id": str(version.chunks.get().pk)})
        with self.assertRaises(EmbeddingInvariantError):
            process_embedding_work(work, provider=provider)
        provider.embed.assert_not_called()
        self.assertTrue(source.versions.exists())
        self.assertTrue(version.chunks.exists())

    def test_retained_mixed_claim_and_citations_are_denied(self):
        claim = retrieval_fixtures.MemoryRetrievalAndAnswerTests.make_claim(
            self, external_id="synthetic-linear", statement="Synthetic confidential roadmap",
            object_value="synthetic",
        )
        source, version, _ = self.capture()
        MemoryEvidence.objects.create(
            claim=claim, source=source, source_version=version, chunk=version.chunks.get(),
            quote="Synthetic confidential roadmap", quote_hash=hashlib.sha256(b"Synthetic confidential roadmap").hexdigest(),
        )
        self.members_mapping()
        self.assertFalse(exclude_members_claims(type(claim).objects.filter(pk=claim.pk)).exists())
        self.assertFalse(eligible_evidence_queryset().filter(source=source).exists())
        self.assertTrue(type(claim).objects.filter(pk=claim.pk).exists())
        self.assertEqual(claim.evidence.count(), 2)

    def test_current_policy_survives_conflicting_metadata_and_missing_scope_configuration(self):
        source, _, _ = self.capture(metadata={"channel_id": "CCOMMUNITY"})
        self.members_mapping()
        self.assertIsNone(source.source_scope_id)
        self.assertIsNone(source.configuration_id)
        self.assertTrue(source_is_members(source))
        self.assertTrue(members_source_ids().filter(pk=source.pk).exists())

    def test_members_new_memory_capture_and_wake_are_denied_even_flags_off(self):
        self.members_mapping()
        with self.assertRaisesMessage(EvidenceKernelError, "Members"):
            self.capture()
        self.assertEqual(schedule_artifact_wake(
            provider="slack", external_account_id="TPRIMARY", external_scope_id="CEXCLUDED",
        ), 0)
        self.assertFalse(MemorySource.objects.exists())

    def test_unmapped_members_rekey_preserves_old_source_policy(self):
        self.artifact(self.connection, "CEXCLUDED")
        mapping = self.members_mapping()
        self.assertFalse(rekey_channel("CEXCLUDED", "CNEWIDENTITY"))
        mapping.refresh_from_db()
        self.assertEqual(mapping.slack_channel_id, "CEXCLUDED")
        self.assertEqual(mapping.room_sync_error, "rekey_requires_room_migration")
        self.assertTrue(is_members_source("TPRIMARY", "CEXCLUDED"))
        self.assertFalse(slack_source_queryset(SlackMessageArtifact).exists())

    def test_real_mapping_public_action_permission_and_deselection_cleanup(self):
        from community_chat.slack_action_views import SlackMessageActionView
        self.artifact(self.connection, "CEXCLUDED")
        self.artifact(self.connection, "CCOMMUNITY")
        members = self.members_mapping()
        CommunityBridgeChannel.objects.filter(pk=members.pk).update(enabled=True)
        CommunityBridgeChannel.objects.create(
            slack_workspace_id="TPRIMARY", slack_channel_id="CCOMMUNITY", audience="community",
            destination_platform="buzz", destination_channel_id="00000000-0000-4000-8000-000000000001",
            enabled=True,
        )
        prefix = "community_chat.slack_action_views."
        grant = SimpleNamespace(slack_workspace_id="TPRIMARY")
        data = {"workspace_id": "TPRIMARY", "message_ts": "1790000000.000001",
                "thread_ts": "1790000000.000001", "action_id": "confirm_topic_btn_0", "action_hash": "a" * 64}
        with self.settings(ROO_SERVICE_URL="https://roo.test", ROO_INTERNAL_MENTION_API_KEY="synthetic"), \
                patch(prefix + "active_grant_for_user", return_value=grant), \
                patch(prefix + "SlackDmMirrorConversation.objects.filter") as private, \
                patch(prefix + "_capture_slack_grant_api_authority", return_value=SimpleNamespace(slack_user_id="UOWNER")), \
                patch(prefix + "_lock_slack_grant_api_authority"), \
                patch(prefix + "requests.post", return_value=Mock(status_code=200, json=lambda: {"message": {"text": "done"}})) as provider:
            private.return_value.first.return_value = None
            view = SlackMessageActionView()
            request = SimpleNamespace(user=self.user)
            self.assertEqual(view._handoff(request, {**data, "channel_id": "CEXCLUDED"}, perform=False).status_code, 403)
            provider.assert_not_called()
            self.assertEqual(view._handoff(request, {**data, "channel_id": "CCOMMUNITY"}, perform=False).status_code, 200)
        with patch.object(external_connectors, "_latest_slack_connection", return_value=self.connection), \
                patch.object(external_connectors, "_lock_slack_connection_authority", return_value=self.connection), \
                patch.object(external_connectors, "_slack_connection_authority", return_value=object()):
            external_connectors.update_slack_channel_selections(self.user, ["CCOMMUNITY"])
        self.assertFalse(SlackChannelSelection.objects.get(connection=self.connection, channel_id="CEXCLUDED").selected)
        self.assertTrue(SlackChannelSelection.objects.get(connection=self.connection, channel_id="CCOMMUNITY").selected)

    def test_retained_mixed_review_and_action_evidence_denied_but_audit_remains(self):
        from org_memory.actions import AgentActionError, _assert_evidence_still_valid, _validate_evidence
        from org_memory.action_views import _action_payload
        from org_memory.review_views import _authorized_review_evidence
        claim = retrieval_fixtures.MemoryRetrievalAndAnswerTests.make_claim(
            self, external_id="mixed-actions", statement="Synthetic mixed statement", object_value="synthetic",
        )
        source, _ = add_retained_member_evidence(claim)
        authorization = SimpleNamespace(may_view_memory_class=lambda value: True)
        review = SimpleNamespace(target=claim, organization=self.organization)
        self.assertEqual(_authorized_review_evidence(review, authorization).count(), 2)
        self.assertTrue(_validate_evidence(
            organization=self.organization, authorization=authorization, claim_ids=[str(claim.pk)], source_ids=[],
        )[0])
        self.members_mapping()
        self.assertFalse(_authorized_review_evidence(review, authorization).exists())
        with self.assertRaisesMessage(AgentActionError, "Members"):
            _validate_evidence(organization=self.organization, authorization=authorization,
                               claim_ids=[str(claim.pk)], source_ids=[])
        with self.assertRaisesMessage(AgentActionError, "Members"):
            _validate_evidence(organization=self.organization, authorization=authorization,
                               claim_ids=[], source_ids=[str(source.pk)])
        proposal = SimpleNamespace(pk="synthetic", action_type="synthetic", status="awaiting_approval",
            evidence_claim_ids=[str(claim.pk)], evidence_source_ids=[], reversal_supported=True,
            created_at=timezone.now(), updated_at=timezone.now(), input_payload={"text": "hidden"})
        proposal.organization = self.organization
        with self.assertRaisesMessage(AgentActionError, "Members"):
            _assert_evidence_still_valid(proposal, authorization)
        self.assertNotIn("input_payload", _action_payload(proposal, include_content=True, include_events=True))
        self.assertTrue(MemorySource.objects.filter(pk=source.pk).exists())
