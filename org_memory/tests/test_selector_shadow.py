import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from org_memory.kernel import capture_source_version, revoke_source_access
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryEpistemicType,
    MemoryEvidence,
    MemoryEvidenceSufficiency,
    MemoryExtractionRun,
    MemoryExtractionStatus,
    MemoryFeedback,
    MemoryFeedbackType,
    MemoryQueryLog,
    MemoryQueryStatus,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationIdentityProvider,
    OrganizationMembership,
)
from org_memory.selector_shadow import (
    FEATURE_NAMES,
    SelectorShadowDisabled,
    build_selector_dataset,
    write_selector_dataset,
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@override_settings(
    ORG_MEMORY_SELECTOR_EXPORT_SECRET="selector-test-secret-with-at-least-32-bytes",
    ORG_MEMORY_SELECTOR_EXPORT_ENABLED=False,
    ORG_MEMORY_SELECTOR_SHADOW_LIMIT=100,
)
class SelectorDatasetExportTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Selector",
            domain="selector.mlai.test",
        )
        self.user = get_user_model().objects.create_user(
            email="selector-reader@mlai.test"
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        capability, _ = OrganizationCapability.objects.get_or_create(
            key="view_general_memory",
            defaults={"name": "View general memory"},
        )
        OrganizationCapabilityGrant.objects.create(
            membership=self.membership,
            capability=capability,
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider=OrganizationIdentityProvider.SLACK,
            external_tenant_id="TSELECTOR",
            external_user_id="USELECTOR",
            verified_at=timezone.now(),
        )
        self.positive_claim = self._make_claim(
            external_id="SELECTOR-POSITIVE",
            statement="Project Roo launch status is green and private.",
        )
        self.negative_claim = self._make_claim(
            external_id="SELECTOR-NEGATIVE",
            statement="Project Roo launch status is an obsolete draft.",
        )
        self.query_log = self._make_query_log()
        MemoryFeedback.objects.create(
            organization=self.organization,
            query_log=self.query_log,
            claim=self.positive_claim,
            user=self.user,
            feedback_type=MemoryFeedbackType.RELEVANT,
            idempotency_key="selector-positive-feedback",
        )
        MemoryFeedback.objects.create(
            organization=self.organization,
            query_log=self.query_log,
            claim=self.negative_claim,
            user=self.user,
            feedback_type=MemoryFeedbackType.IRRELEVANT,
            idempotency_key="selector-negative-feedback",
        )

    def _make_claim(self, *, external_id, statement):
        source, version, _ = capture_source_version(
            organization=self.organization,
            provider="linear",
            external_account_id="selector-linear",
            source_type="issue",
            external_id=external_id,
            version_key="v1",
            content_hash=digest(statement),
            classification="internal",
            acl={
                "is_accessible": True,
                "provider_revision": "acl-v1",
                "principal_refs": ["team:committee"],
            },
            chunks=[{"ordinal": 0, "text": statement}],
            title=external_id,
        )
        extraction = MemoryExtractionRun.objects.create(
            organization=self.organization,
            source_version=version,
            idempotency_key=digest(f"extract:{external_id}"),
            status=MemoryExtractionStatus.EXTRACTED,
            extractor_version="selector-test-extractor-v1",
            schema_version="selector-test-schema-v1",
            prompt_version="selector-test-prompt-v1",
            model="test-model",
            prompt_input_hash=digest(f"input:{external_id}"),
            candidate_payload_hash=digest(f"payload:{external_id}"),
        )
        claim = MemoryClaim.objects.create(
            organization=self.organization,
            extraction_run=extraction,
            candidate_key=digest(f"candidate:{external_id}"),
            kind=MemoryClaimKind.PROJECT_STATUS,
            epistemic_type=MemoryEpistemicType.OBSERVATION,
            predicate="has_status",
            object_value={"status": "fixture"},
            statement=statement,
            normalized_key=digest(f"normalized:{external_id}"),
            status=MemoryClaimStatus.ACTIVE,
            classification="internal",
            confidence=0.9,
            importance=0.8,
            source_authority=0.75,
            review_required=False,
            extractor_version="selector-test-extractor-v1",
            extractor_model="test-model",
            extractor_prompt_version="selector-test-prompt-v1",
            extractor_schema_version="selector-test-schema-v1",
        )
        chunk = version.chunks.get()
        MemoryEvidence.objects.create(
            claim=claim,
            source=source,
            source_version=version,
            chunk=chunk,
            quote=statement,
            quote_hash=digest(statement),
            source_locator={"fixture": external_id},
        )
        return claim

    def _make_query_log(self):
        trace = [
            {
                "candidate_id": f"claim:{self.negative_claim.pk}",
                "score": 0.9,
                "lane_ranks": {"structured": 1, "claim_text": 1},
                "features": {
                    "lexical_relevance": 0.7,
                    "entity_match": True,
                    "current_state": True,
                    "structured_match": True,
                    "source_authority": 0.75,
                    "claim_confidence": 0.9,
                    "status": "active",
                    "untrusted_text": "TRACE-SECRET-MUST-NOT-EXPORT",
                },
                "selected": True,
                "untrusted_row": "TRACE-ROW-SECRET-MUST-NOT-EXPORT",
            },
            {
                "candidate_id": f"claim:{self.positive_claim.pk}",
                "score": 0.2,
                "lane_ranks": {"claim_text": 2},
                "features": {
                    "lexical_relevance": 0.4,
                    "source_authority": 0.75,
                    "claim_confidence": 0.9,
                    "status": "active",
                },
                "selected": False,
            },
        ]
        return MemoryQueryLog.objects.create(
            organization=self.organization,
            requester_user=self.user,
            requester_slack_id="USELECTOR",
            channel_id="GPRIVATECHANNEL",
            request_id="raw-request-id",
            query="What is the secret Roo launch status?",
            query_hash=digest("raw query"),
            query_plan={
                "mode": "current_state",
                "entity_names": ["Secret Roo Project"],
                "time_start": "2026-07-01T00:00:00Z",
            },
            candidate_trace=trace,
            selected_claim_ids=[str(self.negative_claim.pk)],
            answer="The secret answer must not be exported.",
            citation_data=[{"source": "private-source"}],
            status=MemoryQueryStatus.ANSWERED,
            evidence_sufficiency=MemoryEvidenceSufficiency.SUFFICIENT,
            confidence=0.9,
            selector_version="org-memory-rules-selector-v1",
        )

    def test_dataset_is_content_free_pseudonymised_and_explicitly_labeled(self):
        dataset = build_selector_dataset(organization=self.organization)
        serialized = json.dumps(dataset.as_dict(), sort_keys=True)

        self.assertEqual(dataset.manifest["eligible_trace_count"], 1)
        self.assertEqual(dataset.manifest["labeled_trace_count"], 1)
        self.assertEqual(dataset.manifest["pairwise_trace_count"], 1)
        for forbidden in (
            "What is the secret",
            "The secret answer",
            "GPRIVATECHANNEL",
            "USELECTOR",
            "raw-request-id",
            "Secret Roo Project",
            "TRACE-SECRET",
            "TRACE-ROW-SECRET",
            str(self.positive_claim.pk),
            str(self.negative_claim.pk),
        ):
            self.assertNotIn(forbidden, serialized)
        candidates = dataset.records[0]["candidates"]
        self.assertEqual(set(candidates[0]["features"]), set(FEATURE_NAMES))
        self.assertEqual(
            sorted(candidate["label"] for candidate in candidates),
            [0, 1],
        )
        self.assertEqual(len(dataset.records[0]["pairwise_labels"]), 1)
        self.assertFalse(
            dataset.manifest["feedback_policy"]["implicit_negatives"]
        )

    def test_pseudonyms_are_stable_for_one_secret_and_change_after_rotation(self):
        first = build_selector_dataset(organization=self.organization)
        second = build_selector_dataset(organization=self.organization)
        self.assertEqual(first.dataset_hash, second.dataset_hash)
        self.assertEqual(
            first.records[0]["query_ref"],
            second.records[0]["query_ref"],
        )

        with override_settings(
            ORG_MEMORY_SELECTOR_EXPORT_SECRET=(
                "rotated-selector-test-secret-with-at-least-32-bytes"
            )
        ):
            rotated = build_selector_dataset(organization=self.organization)
        self.assertNotEqual(first.dataset_hash, rotated.dataset_hash)
        self.assertNotEqual(
            first.records[0]["query_ref"],
            rotated.records[0]["query_ref"],
        )

    def test_conflicting_feedback_is_omitted_instead_of_inventing_a_label(self):
        MemoryFeedback.objects.create(
            organization=self.organization,
            query_log=self.query_log,
            claim=self.positive_claim,
            user=self.user,
            feedback_type=MemoryFeedbackType.HARMFUL,
            idempotency_key="selector-conflicting-feedback",
        )

        dataset = build_selector_dataset(organization=self.organization)
        labels = {
            row["candidate_ref"]: row["label"]
            for row in dataset.records[0]["candidates"]
        }
        self.assertEqual(list(labels.values()).count(None), 1)
        self.assertEqual(dataset.records[0]["pairwise_labels"], [])

    def test_revoked_candidate_excludes_the_whole_trace(self):
        revoke_source_access(
            self.positive_claim.evidence.get().source,
            reason="selector_test_revocation",
        )

        dataset = build_selector_dataset(organization=self.organization)

        self.assertEqual(dataset.records, ())
        self.assertEqual(dataset.manifest["eligible_trace_count"], 0)
        self.assertEqual(
            dataset.manifest["excluded_counts"],
            {"candidate_not_currently_authorized": 1},
        )

    def test_inactive_requester_excludes_trace(self):
        self.membership.is_active = False
        self.membership.save(update_fields=("is_active", "updated_at"))

        dataset = build_selector_dataset(organization=self.organization)

        self.assertEqual(dataset.records, ())
        self.assertEqual(
            dataset.manifest["excluded_counts"],
            {"requester_not_currently_authorized": 1},
        )

    def test_export_is_disabled_by_default_and_writes_private_file_when_enabled(self):
        dataset = build_selector_dataset(organization=self.organization)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "selector.json"
            with self.assertRaises(SelectorShadowDisabled):
                write_selector_dataset(dataset, output)
            with override_settings(ORG_MEMORY_SELECTOR_EXPORT_ENABLED=True):
                written = write_selector_dataset(dataset, output)
            self.assertEqual(os.stat(written).st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(written.read_text())["dataset_hash"],
                dataset.dataset_hash,
            )

    def test_export_command_fails_closed_when_disabled(self):
        with self.assertRaises(CommandError):
            call_command(
                "export_org_memory_selector_data",
                organization_domain=self.organization.domain,
                output="/tmp/disabled-selector-export.json",
            )

    def test_enabled_export_command_writes_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            export_path = Path(directory) / "selector-dataset.json"
            export_stdout = io.StringIO()
            with override_settings(ORG_MEMORY_SELECTOR_EXPORT_ENABLED=True):
                call_command(
                    "export_org_memory_selector_data",
                    organization_domain=self.organization.domain,
                    output=str(export_path),
                    stdout=export_stdout,
                )

        export_result = json.loads(export_stdout.getvalue())
        self.assertEqual(export_result["eligible_trace_count"], 1)
