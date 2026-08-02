import hashlib
import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from organizations.models import Organization
from org_memory.answering import (
    ABSTENTION_ANSWER,
    AnswerProviderResult,
    GroundedAnswerProviderError,
    answer_memory_query,
    search_memory_query,
)
from org_memory.authorization import OrganizationAuthorizationContext
from org_memory.assertions import actor_identity_headers, build_actor_assertion
from org_memory.consolidation import refresh_current_state, transition_claim
from org_memory.extraction import ProviderResult, extract_source_version
from org_memory.evals import evaluate_retrieval_seed_suite
from org_memory.kernel import capture_source_version, revoke_source_access
from org_memory.models import (
    MemoryClaim,
    MemoryClaimKind,
    MemoryClaimStatus,
    MemoryEvidenceSufficiency,
    MemoryFeedback,
    MemoryCorrectionProposal,
    MemoryEntity,
    MemoryPilotDeployment,
    MemoryPilotDeploymentState,
    MemoryQueryLog,
    MemoryQueryMode,
    MemoryQueryStatus,
    OrganizationCapability,
    OrganizationCapabilityGrant,
    OrganizationIdentity,
    OrganizationMembership,
    OrganizationRole,
    OrganizationRoleAssignment,
    OrganizationSlackWorkspace,
    ServicePrincipal,
)
from org_memory.pilot_deployment import approval_allowlist_hashes
from org_memory.retrieval import plan_memory_query, select_memory
from org_memory.service_principals import issue_service_principal_credential
from roo.models import PointsAdmin


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeExtractionProvider:
    def __init__(self, payload):
        self.payload = payload

    def extract(self, *, source_data, target):
        return ProviderResult(payload=self.payload, response_id="resp_retrieval_extract", usage={})


class FakeAnswerProvider:
    def __init__(self, *, answer="The pilot status is green.", cited_memory_ids=None):
        self.answer_text = answer
        self.cited_memory_ids = cited_memory_ids
        self.calls = 0
        self.evidence_bundle = None

    def answer(self, *, query, evidence_bundle, target):
        self.calls += 1
        self.evidence_bundle = evidence_bundle
        cited = self.cited_memory_ids or [evidence_bundle["memories"][0]["memory_id"]]
        return AnswerProviderResult(
            output={
                "answer": self.answer_text,
                "cited_memory_ids": cited,
                "confidence": 0.92,
                "suggested_follow_up": None,
            },
            response_id="resp_grounded_answer",
            usage={"input_tokens": 200, "output_tokens": 30},
        )


class NeverAnswerProvider:
    def __init__(self):
        self.calls = 0

    def answer(self, **kwargs):
        self.calls += 1
        raise AssertionError("The provider must not run for deterministic abstention.")


@override_settings(
    ORG_MEMORY_EXTRACTION_MODEL="gpt-5.6-luna",
    ORG_MEMORY_EXTRACTOR_VERSION="org-memory-retrieval-test-extractor-v1",
    ORG_MEMORY_EXTRACTION_SCHEMA_VERSION="org-memory-retrieval-test-schema-v1",
    ORG_MEMORY_EXTRACTION_PROMPT_VERSION="org-memory-retrieval-test-prompt-v1",
    ORG_MEMORY_EXTRACTION_MAX_INPUT_CHARS=10000,
    ORG_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=2000,
    ORG_MEMORY_EXTRACTION_REASONING_EFFORT="none",
    ORG_MEMORY_QUERY_VECTOR_ENABLED=False,
    ORG_MEMORY_QUERY_CANDIDATE_LIMIT=50,
    ORG_MEMORY_QUERY_RESULT_LIMIT=20,
    ORG_MEMORY_SELECTOR_VERSION="org-memory-selector-test-v1",
    ORG_MEMORY_ANSWER_MODEL="gpt-5.6-terra",
    ORG_MEMORY_ANSWERER_VERSION="org-memory-answerer-test-v1",
    ORG_MEMORY_ANSWER_SCHEMA_VERSION="org-memory-answer-schema-test-v1",
    ORG_MEMORY_ANSWER_PROMPT_VERSION="org-memory-answer-prompt-test-v1",
    ORG_MEMORY_ANSWER_MAX_OUTPUT_TOKENS=1000,
    ORG_MEMORY_ANSWER_REASONING_EFFORT="none",
)
class MemoryRetrievalAndAnswerTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Retrieval",
            domain="retrieval.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="reader@retrieval.test")
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        self.authorization = OrganizationAuthorizationContext(
            membership=self.membership,
            role_slugs=("reader",),
            allowed_capabilities=frozenset({"view_general_memory"}),
            denied_capabilities=frozenset(),
        )
        self.actor = SimpleNamespace(
            organization=self.organization,
            user=self.user,
            slack_user_id="UREADER123",
            slack_channel_id="GADMIN123",
            slack_thread_ts="1700000000.123",
            request_id="retrieval-request-1",
        )
        self.observed_at = datetime(2026, 7, 15, 9, tzinfo=datetime_timezone.utc)

    def make_claim(
        self,
        *,
        external_id,
        statement,
        object_value,
        classification="internal",
        accessible=True,
        kind=MemoryClaimKind.PROJECT_STATUS,
        predicate="has_status",
        organization=None,
        observed_at=None,
    ):
        organization = organization or self.organization
        observed_at = observed_at or self.observed_at
        _source, version, _created = capture_source_version(
            organization=organization,
            provider="linear",
            external_account_id=f"linear-{organization.pk}",
            source_type="issue",
            external_id=external_id,
            version_key="v1",
            content_hash=digest(statement),
            classification=classification,
            acl={
                "is_accessible": accessible,
                "provider_revision": "acl-v1",
                "principal_refs": ["team:committee"],
            },
            chunks=[{"ordinal": 0, "text": statement}],
            title=f"Linear {external_id}",
            canonical_url=f"https://linear.example/{external_id}",
            occurred_at=observed_at,
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
                    "observed_at": observed_at.isoformat(),
                    "event_start_at": None,
                    "event_end_at": None,
                    "valid_from": observed_at.isoformat(),
                    "valid_until": None,
                    "confidence": 0.9,
                    "importance": 0.8,
                    "classification": classification,
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
        claim = MemoryClaim.objects.get(extraction_run_id=result["extraction_run_id"])
        transition_claim(
            claim=claim,
            to_status=MemoryClaimStatus.ACTIVE,
            reason="retrieval_fixture_activation",
            actor=self.user,
        )
        refresh_current_state(organization)
        claim.refresh_from_db()
        return claim

    def test_query_planner_detects_temporal_and_open_loop_modes(self):
        timeline = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="What changed on Pilot last week?",
        )
        open_loops = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="What open tasks remain?",
        )
        historical = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="What was true then?",
            as_of=self.observed_at,
        )

        self.assertEqual(timeline.mode, MemoryQueryMode.TIMELINE)
        self.assertIsNotNone(timeline.time_start)
        self.assertEqual(open_loops.mode, MemoryQueryMode.OPEN_LOOPS)
        self.assertIn(MemoryClaimKind.TASK, open_loops.kinds)
        self.assertEqual(historical.mode, MemoryQueryMode.HISTORICAL_AS_OF)

    def test_query_planner_recognises_counted_recent_decisions(self):
        plan = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="Summarise the five most recent decisions and explain what changed.",
        )

        self.assertEqual(plan.mode, MemoryQueryMode.TIMELINE)
        self.assertIn(MemoryClaimKind.DECISION, plan.kinds)
        self.assertIn(MemoryClaimKind.TASK, plan.kinds)
        self.assertEqual(plan.required_kind, MemoryClaimKind.DECISION)
        self.assertEqual(plan.requested_count, 5)
        self.assertTrue(plan.recency_priority)

    def test_query_planner_matches_only_complete_meaningful_entity_phrases(self):
        one_letter = MemoryEntity.objects.create(
            organization=self.organization,
            entity_type="project",
            canonical_name="R",
            normalized_name="r",
            resolved_key="test-project-r",
        )
        pilot = MemoryEntity.objects.create(
            organization=self.organization,
            entity_type="project",
            canonical_name="Project Atlas",
            normalized_name="project atlas",
            resolved_key="test-project-atlas",
        )

        broad_plan = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="Summarise our five most recent decisions and explain what changed.",
        )
        explicit_plan = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="What changed on Project Atlas?",
        )
        partial_plan = plan_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            query="What changed on the Project Atlases portfolio?",
        )

        self.assertNotIn(str(one_letter.pk), broad_plan.entity_ids)
        self.assertEqual(explicit_plan.entity_ids, (str(pilot.pk),))
        self.assertEqual(partial_plan.entity_ids, ())

    def test_retrieval_seed_suite(self):
        result = evaluate_retrieval_seed_suite()
        self.assertTrue(result["ok"], result["errors"])

    def test_hard_filters_run_before_ranking_and_trace(self):
        visible = self.make_claim(
            external_id="VISIBLE-1",
            statement="Pilot roadmap priority is partner onboarding.",
            object_value="partner onboarding",
        )
        finance = self.make_claim(
            external_id="FINANCE-1",
            statement="Pilot roadmap finance priority is acquisition diligence.",
            object_value="acquisition diligence",
            classification="finance",
        )
        revoked = self.make_claim(
            external_id="REVOKED-1",
            statement="Pilot roadmap priority is confidential acquisition diligence.",
            object_value="confidential acquisition diligence",
        )
        revoke_source_access(revoked.evidence.get().source, reason="permission_removed")
        other = Organization.objects.create(name="Other", domain="other-retrieval.test")
        foreign = self.make_claim(
            external_id="FOREIGN-1",
            statement="Pilot roadmap priority is a foreign confidential record.",
            object_value="foreign confidential record",
            organization=other,
        )

        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="Pilot roadmap priority partner onboarding",
        )
        traced_ids = {row["candidate_id"] for row in selection.candidate_trace}

        self.assertIn(f"claim:{visible.pk}", traced_ids)
        self.assertNotIn(f"claim:{finance.pk}", traced_ids)
        self.assertNotIn(f"claim:{revoked.pk}", traced_ids)
        self.assertNotIn(f"claim:{foreign.pk}", traced_ids)
        self.assertTrue(
            all(
                item.candidate.claim is None or item.candidate.claim.pk != finance.pk
                for item in selection.selected
            )
        )

    def test_irrelevant_query_abstains_without_calling_answer_provider(self):
        self.make_claim(
            external_id="PILOT-1",
            statement="Pilot status is green.",
            object_value="green",
        )
        provider = NeverAnswerProvider()

        query_log, selection, answer = answer_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            actor=self.actor,
            query="What is the lunar geology programme budget?",
            provider=provider,
        )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(selection.sufficiency, MemoryEvidenceSufficiency.INSUFFICIENT)
        self.assertEqual(answer["answer"], ABSTENTION_ANSWER)
        self.assertEqual(query_log.status, MemoryQueryStatus.ABSTAINED)
        self.assertEqual(query_log.citation_data, [])

    def test_grounded_answer_uses_only_packed_evidence_and_authorized_citations(self):
        claim = self.make_claim(
            external_id="PILOT-GREEN",
            statement="Pilot status is green.",
            object_value="green",
        )
        provider = FakeAnswerProvider()

        query_log, selection, answer = answer_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            actor=self.actor,
            query="What is the current Pilot status?",
            provider=provider,
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(provider.evidence_bundle["memories"]), 1)
        self.assertEqual(provider.evidence_bundle["memories"][0]["claim_id"], str(claim.pk))
        self.assertEqual(answer["answer"], "The pilot status is green.")
        self.assertEqual(answer["citations"][0]["provider"], "linear")
        self.assertEqual(answer["citations"][0]["source_url"], "https://linear.example/PILOT-GREEN")
        self.assertEqual(query_log.status, MemoryQueryStatus.ANSWERED)
        self.assertEqual(query_log.input_tokens, 200)
        self.assertNotIn("Pilot status is green.", json.dumps(query_log.candidate_trace))

    def test_model_abstention_overrides_selector_confidence_and_citations(self):
        self.make_claim(
            external_id="PILOT-ABSTENTION",
            statement="Pilot status is green.",
            object_value="green",
        )
        provider = FakeAnswerProvider(answer=ABSTENTION_ANSWER)

        query_log, selection, answer = answer_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            actor=self.actor,
            query="What is the current Pilot status?",
            provider=provider,
        )

        self.assertNotEqual(selection.sufficiency, MemoryEvidenceSufficiency.INSUFFICIENT)
        self.assertEqual(query_log.status, MemoryQueryStatus.ABSTAINED)
        self.assertEqual(
            query_log.evidence_sufficiency,
            MemoryEvidenceSufficiency.INSUFFICIENT,
        )
        self.assertEqual(query_log.confidence, 0)
        self.assertEqual(query_log.citation_data, [])
        self.assertEqual(answer["citations"], [])

    def test_recent_decision_query_is_ordered_by_evidence_time(self):
        older = self.make_claim(
            external_id="DECISION-OLDER",
            statement="The committee decided to keep the old venue.",
            object_value="old venue",
            kind=MemoryClaimKind.DECISION,
            predicate="selected_venue",
            observed_at=datetime(2026, 6, 1, 9, tzinfo=datetime_timezone.utc),
        )
        newer = self.make_claim(
            external_id="DECISION-NEWER",
            statement="The committee decided to use the new venue.",
            object_value="new venue",
            kind=MemoryClaimKind.DECISION,
            predicate="selected_venue",
            observed_at=datetime(2026, 7, 1, 9, tzinfo=datetime_timezone.utc),
        )

        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="Summarise the two most recent decisions.",
        )

        self.assertIn(MemoryClaimKind.DECISION, selection.plan.kinds)
        self.assertEqual(selection.plan.required_kind, MemoryClaimKind.DECISION)
        self.assertEqual(selection.sufficiency, MemoryEvidenceSufficiency.SUFFICIENT)
        self.assertEqual(selection.selected[0].candidate.claim.pk, newer.pk)
        self.assertEqual(selection.selected[1].candidate.claim.pk, older.pk)

    def test_decision_timeline_includes_a_superseded_decision(self):
        older = self.make_claim(
            external_id="DECISION-SUPERSEDED",
            statement="The committee decided to keep the old venue.",
            object_value="old venue",
            kind=MemoryClaimKind.DECISION,
            predicate="selected_venue",
            observed_at=datetime(2026, 6, 1, 9, tzinfo=datetime_timezone.utc),
        )
        newer = self.make_claim(
            external_id="DECISION-CURRENT",
            statement="The committee decided to use the new venue instead.",
            object_value="new venue",
            kind=MemoryClaimKind.DECISION,
            predicate="selected_venue",
            observed_at=datetime(2026, 7, 1, 9, tzinfo=datetime_timezone.utc),
        )
        transition_claim(
            claim=older,
            to_status=MemoryClaimStatus.SUPERSEDED,
            reason="replaced_by_later_committee_decision",
            actor=self.user,
            effective_at=datetime(2026, 7, 1, 9, tzinfo=datetime_timezone.utc),
        )
        refresh_current_state(self.organization)

        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="Summarise the two most recent decisions and explain what changed.",
        )

        selected_claim_ids = [item.candidate.claim.pk for item in selection.selected]
        self.assertEqual(selected_claim_ids[:2], [newer.pk, older.pk])
        self.assertEqual(selection.sufficiency, MemoryEvidenceSufficiency.SUFFICIENT)

    def test_decision_query_does_not_treat_unreviewed_raw_chunks_as_decisions(self):
        capture_source_version(
            organization=self.organization,
            provider="google_drive",
            external_account_id="drive-retrieval",
            source_type="meeting_transcript",
            external_id="RAW-DECISIONS",
            version_key="v1",
            content_hash=digest("The committee approved the plan."),
            classification="internal",
            acl={"is_accessible": True, "principal_refs": ["group:committee"]},
            chunks=[{"ordinal": 0, "text": "The committee approved the plan."}],
            title="Committee notes",
        )

        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="What were the five most recent decisions?",
        )

        self.assertEqual(selection.candidates, ())
        self.assertEqual(selection.sufficiency, MemoryEvidenceSufficiency.INSUFFICIENT)

    def test_answer_cannot_cite_memory_outside_selected_bundle(self):
        self.make_claim(
            external_id="PILOT-CITATION",
            statement="Pilot status is green.",
            object_value="green",
        )
        provider = FakeAnswerProvider(cited_memory_ids=["claim:not-selected"])

        with self.assertRaises(GroundedAnswerProviderError) as raised:
            answer_memory_query(
                organization=self.organization,
                authorization=self.authorization,
                actor=self.actor,
                query="What is the Pilot status?",
                provider=provider,
            )

        self.assertTrue(raised.exception.query_id)
        self.assertEqual(MemoryQueryLog.objects.get().status, MemoryQueryStatus.FAILED)

    def test_query_log_redacts_secrets_and_search_trace_is_versioned(self):
        self.make_claim(
            external_id="PILOT-SEARCH",
            statement="Pilot status is green.",
            object_value="green",
        )
        raw_query = "What is Pilot status? api_key=super-secret-value"

        query_log, selection = search_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            actor=self.actor,
            query=raw_query,
        )

        self.assertNotIn("super-secret-value", query_log.query)
        self.assertIn("[REDACTED]", query_log.query)
        self.assertEqual(query_log.query_hash, digest(raw_query))
        self.assertEqual(query_log.selector_version, "org-memory-selector-test-v1")
        self.assertTrue(query_log.candidate_trace)
        self.assertEqual(query_log.status, MemoryQueryStatus.SEARCH_ONLY)

    def test_duplicate_source_excerpts_are_packed_once(self):
        text = "The committee handbook explains the volunteer pathway."
        for external_id in ("DOC-1", "DOC-2"):
            capture_source_version(
                organization=self.organization,
                provider="google_drive",
                external_account_id="drive-retrieval",
                source_type="document",
                external_id=external_id,
                version_key="v1",
                content_hash=digest(f"{external_id}:{text}"),
                classification="internal",
                acl={"is_accessible": True, "principal_refs": ["group:committee"]},
                chunks=[{"ordinal": 0, "text": text}],
                title=external_id,
            )

        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="committee handbook volunteer pathway",
        )

        self.assertEqual(len(selection.candidates), 2)
        self.assertEqual(len(selection.selected), 1)

    def test_query_log_deduplicates_citations_by_source_document(self):
        _source, version, _created = capture_source_version(
            organization=self.organization,
            provider="google_drive",
            external_account_id="drive-retrieval",
            source_type="document",
            external_id="DOC-MULTI-CHUNK",
            version_key="v1",
            content_hash=digest("alpha handbook beta pathway"),
            classification="internal",
            acl={"is_accessible": True, "principal_refs": ["group:committee"]},
            chunks=[
                {"ordinal": 0, "text": "Alpha committee handbook context."},
                {"ordinal": 1, "text": "Beta volunteer pathway context."},
            ],
            title="Committee handbook",
        )

        query_log, selection = search_memory_query(
            organization=self.organization,
            authorization=self.authorization,
            actor=self.actor,
            query="alpha committee handbook beta volunteer pathway",
        )

        self.assertEqual(len(selection.selected), 2)
        self.assertEqual(len(query_log.citation_data), 1)
        self.assertEqual(
            query_log.citation_data[0]["source_version_id"],
            str(version.pk),
        )

    def test_explicit_time_filter_cannot_be_bypassed_by_raw_chunk_lane(self):
        claim = self.make_claim(
            external_id="OUTSIDE-RANGE-1",
            statement="Pilot status outside the requested window is green.",
            object_value="green",
        )
        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="Pilot status outside the requested window",
            time_start=datetime(2026, 7, 16, tzinfo=datetime_timezone.utc),
            time_end=datetime(2026, 7, 17, tzinfo=datetime_timezone.utc),
        )

        self.assertNotIn(
            f"chunk:{claim.evidence.get().chunk_id}",
            {row["candidate_id"] for row in selection.candidate_trace},
        )
        self.assertEqual(selection.sufficiency, MemoryEvidenceSufficiency.INSUFFICIENT)

    def test_stale_claim_and_unhealthy_source_emit_warnings(self):
        claim = self.make_claim(
            external_id="STALE-1",
            statement="Pilot status is green.",
            object_value="green",
        )
        transition_claim(
            claim=claim,
            to_status=MemoryClaimStatus.STALE,
            reason="staleness_fixture",
        )
        refresh_current_state(self.organization)

        selection = select_memory(
            organization=self.organization,
            authorization=self.authorization,
            query="What is Pilot status?",
        )

        self.assertIn("stale_memory", selection.warnings)


@override_settings(
    ORG_MEMORY_QUERY_API_ENABLED=True,
    ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION="test-v1",
    ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY="q" * 32,
    ORG_MEMORY_QUERY_VECTOR_ENABLED=False,
    ORG_MEMORY_QUERY_CANDIDATE_LIMIT=50,
    ORG_MEMORY_QUERY_RESULT_LIMIT=20,
    ORG_MEMORY_SELECTOR_VERSION="org-memory-selector-api-test-v1",
    ORG_MEMORY_ANSWER_MODEL="gpt-5.6-terra",
    ORG_MEMORY_ANSWERER_VERSION="org-memory-answerer-api-test-v1",
    ORG_MEMORY_ANSWER_SCHEMA_VERSION="org-memory-answer-schema-api-test-v1",
    ORG_MEMORY_ANSWER_PROMPT_VERSION="org-memory-answer-prompt-api-test-v1",
    ORG_MEMORY_ANSWER_MAX_OUTPUT_TOKENS=1000,
    ORG_MEMORY_ANSWER_REASONING_EFFORT="none",
    ORG_MEMORY_ANSWER_MAX_CONTEXT_TOKENS=6000,
    ORG_MEMORY_EXTRACTION_MODEL="gpt-5.6-luna",
    ORG_MEMORY_EXTRACTOR_VERSION="org-memory-api-test-extractor-v1",
    ORG_MEMORY_EXTRACTION_SCHEMA_VERSION="org-memory-api-test-schema-v1",
    ORG_MEMORY_EXTRACTION_PROMPT_VERSION="org-memory-api-test-prompt-v1",
    ORG_MEMORY_EXTRACTION_MAX_INPUT_CHARS=10000,
    ORG_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS=2000,
    ORG_MEMORY_EXTRACTION_REASONING_EFFORT="none",
)
class MemoryQueryApiTests(TestCase):
    make_claim = MemoryRetrievalAndAnswerTests.make_claim

    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(
            name="Query API",
            domain="query-api.mlai.test",
        )
        self.user = get_user_model().objects.create_user(email="reader@query-api.test")
        self.observed_at = datetime(2026, 7, 15, 9, tzinfo=datetime_timezone.utc)
        self.workspace = OrganizationSlackWorkspace.objects.create(
            organization=self.organization,
            slack_team_id="TQUERY123",
            name="Query API",
        )
        OrganizationIdentity.objects.create(
            organization=self.organization,
            user=self.user,
            provider="slack",
            external_tenant_id="TQUERY123",
            external_user_id="UQUERY123",
            email_at_link_time=self.user.email,
            verified_at=timezone.now(),
        )
        self.membership = OrganizationMembership.objects.create(
            organization=self.organization,
            user=self.user,
        )
        PointsAdmin.objects.create(
            slack_user_id="UQUERY123",
            user=self.user,
            role="committee",
            is_active=True,
        )
        role = OrganizationRole.objects.create(
            organization=self.organization,
            slug="admin-roo-query-reader",
            name="Admin Roo query reader",
        )
        OrganizationRoleAssignment.objects.create(
            membership=self.membership,
            role=role,
        )
        OrganizationCapabilityGrant.objects.create(
            role=role,
            capability=OrganizationCapability.objects.get(key="view_general_memory"),
        )
        self.principal = ServicePrincipal.objects.create(
            name="admin-roo-query-api",
            organization=self.organization,
            scopes=["org_memory.read"],
            allowed_surfaces=["admin_roo"],
        )
        self.credential, self.token = issue_service_principal_credential(self.principal)
        allowlist = approval_allowlist_hashes(
            self.organization,
            {
                "pilot_admin_refs": ["slack:UQUERY123"],
                "allowed_slack_contexts": ["channel:GQUERY123"],
            },
        )
        MemoryPilotDeployment.objects.create(
            organization=self.organization,
            state=MemoryPilotDeploymentState.ACTIVE,
            approval_manifest_hash="a" * 64,
            approval_review_due_at=timezone.now() + timedelta(days=30),
            allowlist_key_version=allowlist["key_version"],
            actor_ref_hashes=allowlist["actor_hashes"],
            context_ref_hashes=allowlist["context_hashes"],
            approved_provider_count=1,
            approved_source_scope_count=1,
            stage_idempotency_key="query-api-test-stage",
            activation_idempotency_key="query-api-test-activate",
            activated_at=timezone.now(),
        )
        self.request_number = 0

    def headers(self, *, surface="admin_roo"):
        self.request_number += 1
        request_id = f"query-api-request-{self.request_number}"
        event_id = f"EvQUERY{self.request_number}"
        assertion = build_actor_assertion(
            self.token,
            credential_id=str(self.credential.pk),
            surface=surface,
            slack_team_id="TQUERY123",
            acting_slack_user_id="UQUERY123",
            slack_channel_id="GQUERY123",
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        identity = actor_identity_headers(
            assertion=assertion,
            surface=surface,
            slack_team_id="TQUERY123",
            acting_slack_user_id="UQUERY123",
            slack_channel_id="GQUERY123",
            slack_thread_ts="1700000000.123",
            event_id=event_id,
            request_id=request_id,
        )
        return {
            "HTTP_AUTHORIZATION": f"ServicePrincipal {self.token}",
            **{
                f"HTTP_{key.upper().replace('-', '_')}": value
                for key, value in identity.items()
            },
        }

    def test_answer_trace_feedback_and_timeline_api_contract(self):
        claim = self.make_claim(
            external_id="API-PILOT",
            statement="Pilot status is green.",
            object_value="green",
        )
        provider = FakeAnswerProvider()
        with patch(
            "org_memory.answering.OpenAIGroundedAnswerProvider",
            return_value=provider,
        ):
            answer_response = self.client.post(
                "/api/v1/org-memory/answer",
                {
                    "organization_domain": self.organization.domain,
                    "query": "What is the current Pilot status?",
                    "channel_id": "GQUERY123",
                    "thread_ts": "1700000000.123",
                    "answer_mode": "auto",
                    "max_context_tokens": 6000,
                },
                format="json",
                **self.headers(),
            )

        self.assertEqual(answer_response.status_code, 200, answer_response.data)
        self.assertEqual(answer_response.data["answer"], "The pilot status is green.")
        self.assertEqual(answer_response.data["intent"]["mode"], "CURRENT_STATE")
        self.assertEqual(answer_response.data["citations"][0]["provider"], "linear")
        query_id = answer_response.data["query_id"]

        trace_response = self.client.get(
            f"/api/v1/org-memory/queries/{query_id}/trace",
            **self.headers(),
        )
        self.assertEqual(trace_response.status_code, 200, trace_response.data)
        self.assertEqual(trace_response.data["query_id"], query_id)
        self.assertEqual(
            trace_response.data["versions"]["selector"],
            "org-memory-selector-api-test-v1",
        )
        self.assertTrue(trace_response.data["candidate_trace"])

        feedback_response = self.client.post(
            "/api/v1/org-memory/feedback",
            {
                "query_id": query_id,
                "claim_id": str(claim.pk),
                "feedback_type": "incorrect",
                "correction_text": "The pilot status should be amber based on the later review.",
            },
            format="json",
            **self.headers(),
        )
        self.assertEqual(feedback_response.status_code, 201, feedback_response.data)
        self.assertTrue(feedback_response.data["correction_proposal_id"])
        self.assertEqual(MemoryFeedback.objects.count(), 1)
        self.assertEqual(MemoryCorrectionProposal.objects.count(), 1)

        timeline_response = self.client.get(
            f"/api/v1/org-memory/entities/{claim.subject_entity_id}/timeline",
            **self.headers(),
        )
        self.assertEqual(timeline_response.status_code, 200, timeline_response.data)
        self.assertEqual(timeline_response.data["timeline"][0]["claim_id"], str(claim.pk))
        self.assertEqual(timeline_response.data["timeline"][0]["citations"][0]["provider"], "linear")

    def test_search_api_returns_evidence_pack_without_answer_generation(self):
        claim = self.make_claim(
            external_id="API-SEARCH",
            statement="Pilot status is green.",
            object_value="green",
        )

        response = self.client.post(
            "/api/v1/org-memory/search",
            {"query": "Pilot status green", "max_context_tokens": 6000},
            format="json",
            **self.headers(),
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["memories"][0]["claim_id"], str(claim.pk))
        self.assertEqual(MemoryQueryLog.objects.get().status, MemoryQueryStatus.SEARCH_ONLY)

    def test_answer_api_reports_model_abstention_as_insufficient(self):
        self.make_claim(
            external_id="API-ABSTENTION",
            statement="Pilot status is green.",
            object_value="green",
        )
        provider = FakeAnswerProvider(answer=ABSTENTION_ANSWER)
        with patch(
            "org_memory.answering.OpenAIGroundedAnswerProvider",
            return_value=provider,
        ):
            response = self.client.post(
                "/api/v1/org-memory/answer",
                {
                    "query": "What is the current Pilot status?",
                    "max_context_tokens": 6000,
                },
                format="json",
                **self.headers(),
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["answer"], ABSTENTION_ANSWER)
        self.assertEqual(
            response.data["evidence_sufficiency"],
            MemoryEvidenceSufficiency.INSUFFICIENT,
        )
        self.assertEqual(response.data["confidence"], 0)
        self.assertEqual(response.data["citations"], [])
        self.assertIn("answer_model_abstained", response.data["warnings"])

    def test_revoked_source_cannot_be_recovered_from_old_query_trace(self):
        claim = self.make_claim(
            external_id="API-REVOKE-TRACE",
            statement="Pilot status is green.",
            object_value="green",
        )
        response = self.client.post(
            "/api/v1/org-memory/search",
            {"query": "Pilot status green", "max_context_tokens": 6000},
            format="json",
            **self.headers(),
        )
        self.assertEqual(response.status_code, 200, response.data)

        revoke_source_access(claim.evidence.get().source, reason="permission_removed")
        trace_response = self.client.get(
            f"/api/v1/org-memory/queries/{response.data['query_id']}/trace",
            **self.headers(),
        )

        self.assertEqual(trace_response.status_code, 403, trace_response.data)

    @override_settings(ORG_MEMORY_QUERY_API_ENABLED=False)
    def test_query_api_feature_flag_defaults_fail_closed(self):
        response = self.client.post(
            "/api/v1/org-memory/answer",
            {"query": "What is Pilot status?"},
            format="json",
            **self.headers(),
        )

        self.assertEqual(response.status_code, 503)

    def test_body_cannot_override_verified_organization_or_channel(self):
        response = self.client.post(
            "/api/v1/org-memory/search",
            {
                "organization_domain": "other.example",
                "channel_id": "GOTHER123",
                "query": "Pilot status",
            },
            format="json",
            **self.headers(),
        )

        self.assertEqual(response.status_code, 400)
