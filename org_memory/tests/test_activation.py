import hashlib
import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from org_memory.activation import evaluate_claim_auto_activation
from org_memory.consolidation import consolidate_claim
from org_memory.extraction import ProviderResult, extract_source_version
from org_memory.kernel import capture_source_version
from org_memory.models import (
    MemoryClaim,
    MemoryClaimStatus,
    MemoryConsolidationRun,
    MemoryConsolidationStatus,
    MemoryExtractionRun,
    MemoryReviewItem,
    MemoryReviewStatus,
    MemorySourcePolicy,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationMembership,
)


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload

    def extract(self, *, source_data, target):
        return ProviderResult(
            payload=self.payload,
            response_id="resp_auto_activation_fixture",
            usage={"input_tokens": 50, "output_tokens": 20},
        )


@override_settings(
    ORG_MEMORY_EXTRACTION_MODEL="gpt-5.6-luna",
    ORG_MEMORY_EXTRACTOR_VERSION="org-memory-extractor-auto-test-v1",
    ORG_MEMORY_EXTRACTION_SCHEMA_VERSION="org-memory-schema-auto-test-v1",
    ORG_MEMORY_EXTRACTION_PROMPT_VERSION="org-memory-prompt-auto-test-v1",
    ORG_MEMORY_EXTRACTION_MAX_INPUT_CHARS=10000,
    ORG_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=2000,
    ORG_MEMORY_EXTRACTION_REASONING_EFFORT="none",
    ORG_MEMORY_CONSOLIDATION_MODEL="gpt-5.6-luna",
    ORG_MEMORY_CONSOLIDATOR_VERSION="org-memory-consolidator-auto-test-v1",
    ORG_MEMORY_CONSOLIDATION_SCHEMA_VERSION="org-memory-consolidation-auto-test-v1",
    ORG_MEMORY_CONSOLIDATION_PROMPT_VERSION="org-memory-consolidation-prompt-auto-test-v1",
    ORG_MEMORY_CONSOLIDATION_MAX_MATCHES=20,
    ORG_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS=1200,
    ORG_MEMORY_CONSOLIDATION_REASONING_EFFORT="none",
)
class StrongGroundingActivationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Auto Activation",
            domain="auto-activation.mlai.test",
        )
        self.operator = get_user_model().objects.create_user(
            email="activation-operator@mlai.test"
        )
        membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.operator,
        )
        OrganizationCapabilityGrant.objects.create(
            membership=membership,
            capability=OrganizationCapability.objects.get(key="manage_sources"),
        )
        self.policy = MemorySourcePolicy.objects.create(
            organization=self.organization,
            provider="google_drive",
            policy_key="committee-meeting-transcripts",
            name="Committee meeting transcripts",
            scope_type="meeting_transcript",
            classification="committee",
            authority_score=0.8,
            allowed_memory_kinds=["decision", "task", "project_update"],
            auto_activation_rules={
                "default": "review",
                "decisions_require_explicit_cue": True,
                "tasks_and_project_updates_may_auto_activate": True,
            },
            review_rules={
                "sensitivity": "human_review",
                "contradictions": "human_review",
                "low_confidence": "human_review",
            },
            reviewed_by=self.operator,
            reviewed_at=timezone.now(),
        )
        self.sequence = 0

    def extract_decision(
        self,
        *,
        source_text,
        statement,
        confidence=0.95,
        evidence_confidence=0.95,
    ):
        self.sequence += 1
        external_id = f"meeting-{self.sequence}"
        _source, version, _created = capture_source_version(
            organization=self.organization,
            provider="google_drive",
            external_account_id="drive-auto-activation",
            source_type="meeting_transcript",
            external_id=external_id,
            version_key="v1",
            content_hash=hashlib.sha256(source_text.encode()).hexdigest(),
            classification="committee",
            acl={
                "is_accessible": True,
                "provider_revision": f"acl-{external_id}",
                "principal_refs": ["group:committee"],
            },
            chunks=[{"ordinal": 0, "text": source_text}],
            title="Committee meeting",
        )
        result = extract_source_version(
            source_version=version,
            provider=FakeProvider(
                {
                    "source_summary": statement,
                    "entities": [],
                    "claims": [
                        {
                            "kind": "decision",
                            "epistemic_type": "decision",
                            "subject": None,
                            "predicate": "committee_decided",
                            "object_entity": None,
                            "object_value": statement,
                            "statement": statement,
                            "observed_at": None,
                            "event_start_at": None,
                            "event_end_at": None,
                            "valid_from": None,
                            "valid_until": None,
                            "confidence": confidence,
                            "importance": 0.8,
                            "classification": "committee",
                            "review_required": True,
                            "sensitivity_flags": [],
                            "evidence": [
                                {
                                    "chunk_id": str(version.chunks.get().pk),
                                    "quote": source_text,
                                    "evidence_role": "supports",
                                    "evidence_confidence": evidence_confidence,
                                }
                            ],
                        }
                    ],
                    "no_memory_reason": None,
                }
            ),
        )
        return MemoryClaim.objects.get(extraction_run_id=result["extraction_run_id"])

    def test_strong_explicit_decision_auto_activates_only_after_new_consolidation(self):
        claim = self.extract_decision(
            source_text="The committee agreed to adopt the revised sponsorship plan.",
            statement="The committee agreed to adopt the revised sponsorship plan.",
        )

        self.assertFalse(claim.review_required)
        self.assertFalse(MemoryReviewItem.objects.exists())
        result = consolidate_claim(candidate=claim)

        claim.refresh_from_db()
        run = MemoryConsolidationRun.objects.get(pk=result["consolidation_run_id"])
        self.assertEqual(claim.status, MemoryClaimStatus.ACTIVE)
        self.assertEqual(run.status, MemoryConsolidationStatus.APPLIED)
        self.assertEqual(
            claim.state_events.latest("created_at").reason,
            "auto_activation_strong_grounding_v1",
        )

    def test_unsettled_or_low_confidence_decision_remains_review_required(self):
        claim = self.extract_decision(
            source_text="The committee considered a proposed venue change pending approval.",
            statement="The committee considered a proposed venue change pending approval.",
            confidence=0.89,
        )

        self.assertTrue(claim.review_required)
        self.assertTrue(MemoryReviewItem.objects.filter(status="open").exists())
        result = consolidate_claim(candidate=claim)
        run = MemoryConsolidationRun.objects.get(pk=result["consolidation_run_id"])
        self.assertEqual(run.status, MemoryConsolidationStatus.REVIEW_REQUIRED)

    def test_existing_new_review_can_be_reconciled_after_reviewed_policy_allows_it(self):
        self.policy.auto_activation_rules = {
            "default": "review",
            "decisions_require_explicit_cue": False,
            "tasks_and_project_updates_may_auto_activate": True,
        }
        self.policy.save(update_fields=("auto_activation_rules", "updated_at"))
        claim = self.extract_decision(
            source_text="The committee approved the 2026 operating plan.",
            statement="The committee approved the 2026 operating plan.",
        )
        result = consolidate_claim(candidate=claim)
        run = MemoryConsolidationRun.objects.get(pk=result["consolidation_run_id"])
        self.assertEqual(run.status, MemoryConsolidationStatus.REVIEW_REQUIRED)

        self.policy.auto_activation_rules = {
            "default": "review",
            "decisions_require_explicit_cue": True,
            "tasks_and_project_updates_may_auto_activate": True,
        }
        self.policy.reviewed_at = timezone.now()
        self.policy.save(
            update_fields=("auto_activation_rules", "reviewed_at", "updated_at")
        )
        preview_output = io.StringIO()
        call_command(
            "reconcile_org_memory_auto_activation",
            organization_domain=self.organization.domain,
            provider="google_drive",
            stdout=preview_output,
        )
        preview = json.loads(preview_output.getvalue())
        self.assertEqual(preview["eligible"], 1)
        self.assertEqual(preview["activated"], 0)

        apply_output = io.StringIO()
        call_command(
            "reconcile_org_memory_auto_activation",
            organization_domain=self.organization.domain,
            provider="google_drive",
            operator_email=self.operator.email,
            apply=True,
            stdout=apply_output,
        )
        applied = json.loads(apply_output.getvalue())
        self.assertEqual(applied["activated"], 1)
        claim.refresh_from_db()
        run.refresh_from_db()
        review = MemoryReviewItem.objects.get(pk=run.review_item_id)
        self.assertEqual(claim.status, MemoryClaimStatus.ACTIVE)
        self.assertFalse(claim.review_required)
        self.assertEqual(run.status, MemoryConsolidationStatus.APPLIED)
        self.assertEqual(review.status, MemoryReviewStatus.RESOLVED)
        self.assertEqual(
            review.resolution["activation_policy_version"],
            "strong-grounding-v1",
        )

    def test_unknown_policy_keys_fail_closed(self):
        self.policy.auto_activation_rules["min_confidence"] = 0.1
        self.policy.save(update_fields=("auto_activation_rules", "updated_at"))

        claim = self.extract_decision(
            source_text="The committee approved the revised incident plan.",
            statement="The committee approved the revised incident plan.",
        )

        self.assertTrue(claim.review_required)
        self.assertTrue(MemoryReviewItem.objects.filter(status="open").exists())

    def test_only_known_non_blocking_extraction_flags_can_auto_activate(self):
        claim = self.extract_decision(
            source_text="The committee approved the revised incident plan.",
            statement="The committee approved the revised incident plan.",
        )
        MemoryExtractionRun.objects.filter(pk=claim.extraction_run_id).update(
            safety_flags=["partial_candidate_rejection"]
        )
        claim.refresh_from_db()
        self.assertTrue(evaluate_claim_auto_activation(claim).eligible)

        MemoryExtractionRun.objects.filter(pk=claim.extraction_run_id).update(
            safety_flags=["possible_secret"]
        )
        claim.refresh_from_db()
        self.assertFalse(evaluate_claim_auto_activation(claim).eligible)
