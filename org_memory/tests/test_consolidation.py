import hashlib
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from organizations.models import Organization
from org_memory.consolidation import (
    ConsolidationDecision,
    ConsolidationInvariantError,
    ConsolidationProviderResult,
    approve_consolidation,
    consolidate_claim,
    distinct_evidence_source_count,
    eligible_claims_as_of,
    entity_timeline,
    mark_stale_claims,
    merge_entities,
    process_consolidation_work,
    propose_correction,
    apply_correction,
    refresh_current_state,
    schedule_claim_consolidation,
    transition_claim,
)
from org_memory.evals import evaluate_consolidation_seed_suite
from org_memory.extraction import ProviderResult, extract_source_version
from org_memory.kernel import capture_source_version, revoke_source_access
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryConsolidationOperation,
    MemoryConsolidationRun,
    MemoryConsolidationStatus,
    MemoryCorrectionStatus,
    MemoryCurrentState,
    MemoryEntity,
    MemoryEntityResolutionOperation,
    MemoryEntityType,
    MemoryWorkItem,
    MemoryWorkTaskType,
)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeExtractionProvider:
    def __init__(self, payload):
        self.payload = payload

    def extract(self, *, source_data, target):
        return ProviderResult(
            payload=self.payload,
            response_id="resp_consolidation_extraction_fixture",
            usage={"input_tokens": 50, "output_tokens": 20},
        )


class FakeConsolidationProvider:
    def __init__(self, operation):
        self.operation = operation
        self.calls = 0

    def decide(self, *, candidate, matches, target):
        self.calls += 1
        return ConsolidationProviderResult(
            decision={
                "operation": self.operation,
                "matched_claim_id": matches[0]["claim_id"],
                "confidence": 0.85,
                "reason": "Offline ambiguity fixture.",
            },
            response_id="resp_consolidation_fixture",
            usage={"input_tokens": 80, "output_tokens": 12},
        )


@override_settings(
    ORG_MEMORY_EXTRACTION_MODEL="gpt-5.6-luna",
    ORG_MEMORY_EXTRACTOR_VERSION="org-memory-extractor-consolidation-test-v1",
    ORG_MEMORY_EXTRACTION_SCHEMA_VERSION="org-memory-extraction-consolidation-test-v1",
    ORG_MEMORY_EXTRACTION_PROMPT_VERSION="org-memory-extraction-prompt-test-v1",
    ORG_MEMORY_EXTRACTION_MAX_INPUT_CHARS=10000,
    ORG_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=2000,
    ORG_MEMORY_EXTRACTION_REASONING_EFFORT="none",
    ORG_MEMORY_CONSOLIDATION_MODEL="gpt-5.6-luna",
    ORG_MEMORY_CONSOLIDATOR_VERSION="org-memory-consolidator-test-v1",
    ORG_MEMORY_CONSOLIDATION_SCHEMA_VERSION="org-memory-consolidation-schema-test-v1",
    ORG_MEMORY_CONSOLIDATION_PROMPT_VERSION="org-memory-consolidation-prompt-test-v1",
    ORG_MEMORY_CONSOLIDATION_MAX_MATCHES=20,
    ORG_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS=1200,
    ORG_MEMORY_CONSOLIDATION_REASONING_EFFORT="none",
)
class MemoryConsolidationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Consolidation",
            domain="consolidation.mlai.test",
        )
        self.reviewer = get_user_model().objects.create_user(
            email="reviewer-consolidation@mlai.test"
        )
        self.january = datetime(2026, 1, 1, 9, tzinfo=datetime_timezone.utc)
        self.february = datetime(2026, 2, 1, 9, tzinfo=datetime_timezone.utc)

    def make_claim(
        self,
        *,
        external_id,
        object_value,
        occurred_at,
        version_key="v1",
        kind=MemoryClaimKind.PROJECT_STATUS,
        predicate="has_status",
        statement=None,
        source_text=None,
    ):
        statement = statement or f"Pilot status is {object_value}."
        source_text = source_text or statement
        _source, version, _created = capture_source_version(
            organization=self.organization,
            provider="google_drive",
            external_account_id="drive-consolidation",
            source_type="meeting_transcript",
            external_id=external_id,
            version_key=version_key,
            content_hash=digest(source_text),
            classification="committee",
            acl={
                "is_accessible": True,
                "provider_revision": f"acl-{external_id}-{version_key}",
                "principal_refs": ["group:committee"],
            },
            chunks=[{"ordinal": 0, "text": source_text}],
            title="Pilot review",
            occurred_at=occurred_at,
        )
        payload = {
            "source_summary": statement,
            "entities": [
                {
                    "entity_type": "project",
                    "canonical_name": "Pilot",
                    "description": None,
                    "external_refs": [],
                }
            ],
            "claims": [
                {
                    "kind": kind,
                    "epistemic_type": "observation",
                    "subject": "Pilot",
                    "predicate": predicate,
                    "object_entity": None,
                    "object_value": object_value,
                    "statement": statement,
                    "observed_at": occurred_at.isoformat(),
                    "event_start_at": None,
                    "event_end_at": None,
                    "valid_from": occurred_at.isoformat(),
                    "valid_until": None,
                    "confidence": 0.9,
                    "importance": 0.7,
                    "classification": "committee",
                    "review_required": True,
                    "sensitivity_flags": [],
                    "evidence": [
                        {
                            "chunk_id": str(version.chunks.get().pk),
                            "quote": statement,
                            "evidence_role": "supports",
                            "evidence_confidence": 1.0,
                        }
                    ],
                }
            ],
            "no_memory_reason": None,
        }
        result = extract_source_version(
            source_version=version,
            provider=FakeExtractionProvider(payload),
        )
        return MemoryClaim.objects.get(extraction_run_id=result["extraction_run_id"])

    def activate_new(self, claim):
        result = consolidate_claim(candidate=claim)
        run = MemoryConsolidationRun.objects.get(pk=result["consolidation_run_id"])
        self.assertEqual(run.operation, MemoryConsolidationOperation.NEW)
        self.assertEqual(run.status, MemoryConsolidationStatus.REVIEW_REQUIRED)
        approve_consolidation(run=run, actor=self.reviewer)
        claim.refresh_from_db()
        self.assertEqual(claim.status, MemoryClaimStatus.ACTIVE)
        return run

    def test_strict_consolidation_schema_closes_every_object(self):
        schema = ConsolidationDecision.model_json_schema()

        def assert_closed(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node.get("additionalProperties", True))
                    self.assertEqual(
                        set(node.get("required", [])), set(node.get("properties", {}))
                    )
                for value in node.values():
                    assert_closed(value)
            elif isinstance(node, list):
                for value in node:
                    assert_closed(value)

        assert_closed(schema)

    def test_new_claim_requires_review_and_populates_current_state(self):
        claim = self.make_claim(
            external_id="meeting-new",
            object_value="green",
            occurred_at=self.january,
        )

        self.activate_new(claim)

        state = MemoryCurrentState.objects.get(organization=self.organization)
        self.assertEqual(state.claim_id, claim.pk)
        self.assertFalse(state.has_conflict)
        self.assertEqual(state.distinct_source_count, 1)

    def test_supersession_changes_current_and_historical_as_of_state(self):
        old_claim = self.make_claim(
            external_id="meeting-old",
            object_value="amber",
            occurred_at=self.january,
        )
        self.activate_new(old_claim)
        new_claim = self.make_claim(
            external_id="meeting-newer",
            object_value="green",
            occurred_at=self.february,
        )

        result = consolidate_claim(candidate=new_claim)
        run = MemoryConsolidationRun.objects.get(pk=result["consolidation_run_id"])
        self.assertEqual(run.operation, MemoryConsolidationOperation.SUPERSEDES)
        approve_consolidation(run=run, actor=self.reviewer)
        old_claim.refresh_from_db()
        new_claim.refresh_from_db()

        self.assertEqual(old_claim.status, MemoryClaimStatus.SUPERSEDED)
        self.assertEqual(old_claim.valid_until, self.february)
        self.assertEqual(new_claim.status, MemoryClaimStatus.ACTIVE)
        january_state = eligible_claims_as_of(
            organization=self.organization,
            as_of=self.january + timedelta(days=10),
            historical=True,
        )
        february_state = eligible_claims_as_of(
            organization=self.organization,
            as_of=self.february + timedelta(days=10),
            historical=True,
        )
        self.assertEqual(list(january_state), [old_claim])
        self.assertEqual(list(february_state), [new_claim])
        self.assertEqual(MemoryCurrentState.objects.get().claim_id, new_claim.pk)

    def test_copied_source_version_does_not_inflate_corroboration(self):
        original = self.make_claim(
            external_id="copied-meeting",
            object_value="green",
            occurred_at=self.january,
            source_text="Pilot status is green. Original transcript copy.",
        )
        self.activate_new(original)
        duplicate = self.make_claim(
            external_id="copied-meeting",
            version_key="v2",
            object_value="green",
            occurred_at=self.january,
            source_text="Pilot status is green. Copied transcript formatting.",
        )

        result = consolidate_claim(candidate=duplicate)
        duplicate.refresh_from_db()

        self.assertEqual(result["operation"], MemoryConsolidationOperation.DUPLICATE)
        self.assertEqual(result["evidence_added"], 0)
        self.assertEqual(duplicate.status, MemoryClaimStatus.ARCHIVED)
        self.assertEqual(original.evidence.count(), 1)
        self.assertEqual(distinct_evidence_source_count(original), 1)
        self.assertEqual(MemoryCurrentState.objects.get().distinct_source_count, 1)

    def test_unresolved_contradiction_is_a_visible_warning_until_review(self):
        old_claim = self.make_claim(
            external_id="decision-old",
            object_value="Sydney",
            occurred_at=self.january,
            kind=MemoryClaimKind.DECISION,
            predicate="launch_city",
            statement="The pilot launch city is Sydney.",
        )
        self.activate_new(old_claim)
        candidate = self.make_claim(
            external_id="decision-conflict",
            object_value="Melbourne",
            occurred_at=self.february,
            kind=MemoryClaimKind.DECISION,
            predicate="launch_city",
            statement="The pilot launch city is Melbourne.",
        )
        provider = FakeConsolidationProvider(MemoryConsolidationOperation.CONTRADICTS)

        result = consolidate_claim(candidate=candidate, provider=provider)
        run = MemoryConsolidationRun.objects.get(pk=result["consolidation_run_id"])
        state = MemoryCurrentState.objects.get()

        self.assertEqual(provider.calls, 1)
        self.assertEqual(run.status, MemoryConsolidationStatus.REVIEW_REQUIRED)
        self.assertTrue(state.has_conflict)
        self.assertIn("unresolved_conflict", state.warnings)
        approve_consolidation(run=run, actor=self.reviewer, winner_claim=old_claim)
        candidate.refresh_from_db()
        state.refresh_from_db()
        self.assertEqual(candidate.status, MemoryClaimStatus.CONTRADICTED)
        self.assertFalse(state.has_conflict)

    def test_correction_preserves_original_history_and_evidence(self):
        original = self.make_claim(
            external_id="correction-original",
            object_value="amber",
            occurred_at=self.january,
        )
        self.activate_new(original)
        replacement = self.make_claim(
            external_id="correction-replacement",
            object_value="green",
            occurred_at=self.february,
        )
        proposal = propose_correction(
            original_claim=original,
            replacement_claim=replacement,
            correction_text="The later committee record corrects the pilot status.",
            requested_by=self.reviewer,
        )

        apply_correction(proposal=proposal, actor=self.reviewer)
        proposal.refresh_from_db()
        original.refresh_from_db()
        replacement.refresh_from_db()

        self.assertEqual(proposal.status, MemoryCorrectionStatus.APPLIED)
        self.assertEqual(original.status, MemoryClaimStatus.SUPERSEDED)
        self.assertEqual(replacement.status, MemoryClaimStatus.ACTIVE)
        self.assertTrue(original.evidence.exists())
        self.assertTrue(replacement.evidence.exists())
        timeline = entity_timeline(
            original.subject_entity,
            include_superseded=True,
        )
        self.assertEqual(set(timeline), {original, replacement})

    def test_staleness_is_computed_and_visible_without_deleting_claim(self):
        claim = self.make_claim(
            external_id="stale-project",
            object_value="green",
            occurred_at=self.january,
        )
        self.activate_new(claim)

        result = mark_stale_claims(
            organization=self.organization,
            at=self.january + timedelta(days=31),
        )
        claim.refresh_from_db()
        state = MemoryCurrentState.objects.get()

        self.assertEqual(result["marked_stale"], 1)
        self.assertEqual(claim.status, MemoryClaimStatus.STALE)
        self.assertTrue(state.is_stale)
        self.assertIn("stale", state.warnings)

    def test_access_revocation_removes_claim_from_current_projection(self):
        claim = self.make_claim(
            external_id="revoked-project",
            object_value="green",
            occurred_at=self.january,
        )
        self.activate_new(claim)
        source = claim.evidence.select_related("source").get().source

        revoke_source_access(source, reason="provider_permission_removed")

        self.assertFalse(MemoryCurrentState.objects.filter(organization=self.organization).exists())
        self.assertFalse(
            eligible_claims_as_of(organization=self.organization).filter(pk=claim.pk).exists()
        )

    def test_illegal_transition_and_ambiguous_person_merge_are_blocked(self):
        claim = self.make_claim(
            external_id="illegal-transition",
            object_value="green",
            occurred_at=self.january,
        )
        transition_claim(
            claim=claim,
            to_status=MemoryClaimStatus.ARCHIVED,
            reason="test_archive",
        )
        with self.assertRaises(ConsolidationInvariantError):
            transition_claim(
                claim=claim,
                to_status=MemoryClaimStatus.ACTIVE,
                reason="illegal_reactivation",
            )

        first = MemoryEntity.objects.create(
            organization=self.organization,
            entity_type=MemoryEntityType.PERSON,
            canonical_name="Alex Smith",
            normalized_name="alex smith",
            resolved_key="person:alex-one",
            aliases=["Alex Smith"],
        )
        second = MemoryEntity.objects.create(
            organization=self.organization,
            entity_type=MemoryEntityType.PERSON,
            canonical_name="Alex Smith",
            normalized_name="alex smith",
            resolved_key="person:alex-two",
            aliases=["Alex Smith"],
        )
        with self.assertRaises(ConsolidationInvariantError):
            merge_entities(primary=first, duplicate=second, reason="same display name")

        first.external_refs = {"slack": "U123"}
        first.save(update_fields=("external_refs", "updated_at"))
        second.external_refs = {"slack": "U123"}
        second.save(update_fields=("external_refs", "updated_at"))
        event = merge_entities(
            primary=first,
            duplicate=second,
            actor=self.reviewer,
            reason="shared Slack identity",
        )
        second.refresh_from_db()
        self.assertEqual(event.operation, MemoryEntityResolutionOperation.MERGE)
        self.assertEqual(second.merged_into_id, first.pk)

    def test_consolidation_work_payload_is_identifier_only_and_idempotent(self):
        claim = self.make_claim(
            external_id="consolidation-work",
            object_value="green",
            occurred_at=self.january,
        )

        first = schedule_claim_consolidation(claim=claim)
        second = schedule_claim_consolidation(claim=claim)
        work = MemoryWorkItem.objects.get(task_type=MemoryWorkTaskType.CONSOLIDATE)

        self.assertEqual(first["scheduled"], 1)
        self.assertEqual(second["existing"], 1)
        self.assertEqual(set(work.payload), {
            "claim_id",
            "model",
            "consolidator_version",
            "schema_version",
            "prompt_version",
            "target_fingerprint",
        })
        result = process_consolidation_work(work)
        self.assertEqual(result["operation"], MemoryConsolidationOperation.NEW)
        replay = process_consolidation_work(work)
        self.assertFalse(replay["created"])


class MemoryConsolidationEvalTests(TestCase):
    def test_seed_suite_and_management_command(self):
        result = evaluate_consolidation_seed_suite()
        self.assertTrue(result["ok"], result["errors"])
        call_command("evaluate_org_memory_consolidation")
