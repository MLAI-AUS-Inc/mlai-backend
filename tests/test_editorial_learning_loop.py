"""Comments→spec learning loop (mlai-backend half).

Submitted article comments become ContentFactoryHealingRecord candidates; the content-factory
revision run distills a scope + generalized rule onto each; accepting the revision promotes
durable preferences (and archives one-off fixes) and asks content-factory to fold them into the
site's article-kit specs. Founders can list and retract learned rules.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content_factory.models import (
    ContentFactoryHealingPromotionState,
    ContentFactoryHealingRecord,
    OrganizationContentConfig,
    VibeMarketingComponentComment,
    VibeMarketingComponentCommentStatus,
)
from content_factory.vibe_marketing_views import (
    _create_editorial_feedback_candidates,
    _feedback_family_key,
    _promote_editorial_feedback_batch,
    _remote_comment_payload,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()

FEEDBACK_KIND = "article_component_feedback"


class EditorialLearningLoopBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder-learning@example.com",
            password="password",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="MLAI",
            domain="mlai.au",
            registered=True,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo="MLAI-AUS-Inc/mlai-au")
        self.run = ContentFactoryRun.objects.create(
            run_id="article-run-learning",
            workflow="article_generation",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            result={},
        )
        self.client.force_authenticate(user=self.user)

    def _comment(self, *, body="Make the hero image less stocky.", component_type="ArticleHeroHeader"):
        return VibeMarketingComponentComment.objects.create(
            run=self.run,
            component_id="hero-1",
            component_type=component_type,
            component_label="Hero",
            selector="[data-cf-component-id='hero-1']",
            body=body,
            status=VibeMarketingComponentCommentStatus.DRAFT,
        )

    def _candidate_for(self, comment, batch_id="batch-1"):
        _create_editorial_feedback_candidates(
            organization=self.organization,
            run=self.run,
            comments=[comment],
            batch_id=batch_id,
        )
        return ContentFactoryHealingRecord.objects.get(
            failure_kind=FEEDBACK_KIND,
            failure_family_key=_feedback_family_key(
                domain=self.run.domain, github_repo=self.run.github_repo, comment=comment
            ),
        )

    @staticmethod
    def _distill(record, *, scope, rule="", targets=None, spec_amendment=""):
        normalized = dict(record.normalized_failure or {})
        normalized["distilled"] = {
            "scope": scope,
            "rule": rule,
            "spec_amendment": spec_amendment,
            "applies_to_component_types": list(targets or []),
            "reason": "test",
        }
        record.normalized_failure = normalized
        record.save(update_fields=["normalized_failure", "updated_at"])
        return record


class RemoteCommentPayloadTests(EditorialLearningLoopBase):
    def test_payload_carries_feedback_family_key(self):
        comment = self._comment()
        payload = _remote_comment_payload(comment, run=self.run)
        self.assertEqual(
            payload["feedback_family_key"],
            _feedback_family_key(domain=self.run.domain, github_repo=self.run.github_repo, comment=comment),
        )
        self.assertEqual(payload["comment_id"], str(comment.id))

    def test_payload_without_run_keeps_empty_family_key(self):
        comment = self._comment()
        self.assertEqual(_remote_comment_payload(comment)["feedback_family_key"], "")


class CandidateCreationTests(EditorialLearningLoopBase):
    def test_candidate_records_family_key_in_normalized_and_evidence(self):
        comment = self._comment()
        record = self._candidate_for(comment)
        family_key = _feedback_family_key(
            domain=self.run.domain, github_repo=self.run.github_repo, comment=comment
        )
        self.assertEqual(record.normalized_failure["feedback_family_key"], family_key)
        self.assertEqual(record.evidence_artifacts["feedback_family_key"], family_key)
        self.assertEqual(record.promotion_state, ContentFactoryHealingPromotionState.CANDIDATE)


class PromotionScopeTests(EditorialLearningLoopBase):
    def test_durable_preference_promotes_and_one_off_archives(self):
        durable_comment = self._comment(body="Headings should be sentence case everywhere.")
        one_off_comment = self._comment(body="Fix the typo in this paragraph.", component_type="ArticleProse")
        durable = self._distill(
            self._candidate_for(durable_comment),
            scope="durable_preference",
            rule="Use sentence case for all article headings.",
            targets=["ArticleHeroHeader"],
        )
        one_off = self._distill(self._candidate_for(one_off_comment), scope="one_off")

        promoted, archived = _promote_editorial_feedback_batch(
            run=self.run, batch_id="batch-1", revision_run_id="rev-1"
        )

        self.assertEqual((promoted, archived), (1, 1))
        durable.refresh_from_db()
        one_off.refresh_from_db()
        self.assertEqual(durable.promotion_state, ContentFactoryHealingPromotionState.PROMOTED)
        self.assertEqual(durable.promoted_payload["scope"], "durable_preference")
        self.assertEqual(one_off.promotion_state, ContentFactoryHealingPromotionState.ARCHIVED)
        self.assertEqual(one_off.promoted_payload["scope"], "one_off")

    def test_records_without_distillation_promote_as_before(self):
        record = self._candidate_for(self._comment())
        promoted, archived = _promote_editorial_feedback_batch(
            run=self.run, batch_id="batch-1", revision_run_id="rev-1"
        )
        self.assertEqual((promoted, archived), (1, 0))
        record.refresh_from_db()
        self.assertEqual(record.promotion_state, ContentFactoryHealingPromotionState.PROMOTED)

    def test_archived_records_are_never_resurrected(self):
        record = self._candidate_for(self._comment())
        record.promotion_state = ContentFactoryHealingPromotionState.ARCHIVED
        record.save(update_fields=["promotion_state", "updated_at"])
        promoted, archived = _promote_editorial_feedback_batch(
            run=self.run, batch_id="batch-1", revision_run_id="rev-1"
        )
        self.assertEqual((promoted, archived), (0, 0))
        record.refresh_from_db()
        self.assertEqual(record.promotion_state, ContentFactoryHealingPromotionState.ARCHIVED)


class AcceptRevisionLearningTests(EditorialLearningLoopBase):
    def _revision_run(self, batch_id="batch-1"):
        return ContentFactoryRun.objects.create(
            run_id="revision-run-learning",
            workflow="article_revision",
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"source_run_id": self.run.run_id, "feedback_batch_id": batch_id},
            result={"source_run_id": self.run.run_id, "feedback_batch_id": batch_id},
        )

    def test_accept_revision_promotes_archives_and_requests_fold(self):
        durable_comment = self._comment(body="Use warmer CTA copy.")
        one_off_comment = self._comment(body="Change this heading to 'Pricing'.", component_type="ArticleProse")
        for comment in (durable_comment, one_off_comment):
            comment.status = VibeMarketingComponentCommentStatus.SUBMITTED
            comment.batch_id = "batch-1"
            comment.save(update_fields=["status", "batch_id", "updated_at"])
        self._distill(
            self._candidate_for(durable_comment),
            scope="durable_preference",
            rule="Write CTAs in a warm, invitational tone.",
            targets=["ArticleResourceCTA"],
        )
        self._distill(self._candidate_for(one_off_comment), scope="one_off")
        revision_run = self._revision_run()

        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_editorial_learnings_apply",
            return_value={"status": "queued"},
        ) as fold_call:
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments/accept-revision",
                {"sourceRunId": self.run.run_id, "batchId": "batch-1"},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        fold_call.assert_called_once()
        self.assertEqual(
            fold_call.call_args.kwargs["payload"],
            {"domain": "mlai.au", "github_repo": "MLAI-AUS-Inc/mlai-au"},
        )
        self.run.refresh_from_db()
        latest_batch = self.run.result["component_feedback_latest_batch"]
        self.assertEqual(latest_batch["promotedLearningCount"], 1)
        self.assertEqual(latest_batch["archivedLearningCount"], 1)
        durable_states = set(
            ContentFactoryHealingRecord.objects.filter(failure_kind=FEEDBACK_KIND).values_list(
                "promotion_state", flat=True
            )
        )
        self.assertEqual(
            durable_states,
            {ContentFactoryHealingPromotionState.PROMOTED, ContentFactoryHealingPromotionState.ARCHIVED},
        )
        comment_statuses = set(
            VibeMarketingComponentComment.objects.filter(batch_id="batch-1").values_list("status", flat=True)
        )
        self.assertEqual(comment_statuses, {VibeMarketingComponentCommentStatus.APPLIED})

    def test_accept_revision_skips_fold_when_nothing_promoted(self):
        one_off_comment = self._comment(body="Fix this sentence.")
        one_off_comment.status = VibeMarketingComponentCommentStatus.SUBMITTED
        one_off_comment.batch_id = "batch-1"
        one_off_comment.save(update_fields=["status", "batch_id", "updated_at"])
        self._distill(self._candidate_for(one_off_comment), scope="one_off")
        revision_run = self._revision_run()

        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_editorial_learnings_apply"
        ) as fold_call:
            response = self.client.post(
                f"/api/v1/vibe-marketing/runs/{revision_run.run_id}/comments/accept-revision",
                {"sourceRunId": self.run.run_id, "batchId": "batch-1"},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        fold_call.assert_not_called()


class LearnedRulesEndpointTests(EditorialLearningLoopBase):
    def test_list_returns_rules_and_hides_archived_by_default(self):
        visible = self._distill(
            self._candidate_for(self._comment(body="Prefer illustrations over photos.")),
            scope="durable_preference",
            rule="Prefer brand illustrations over stock photography.",
            targets=["ArticleHeroHeader"],
            spec_amendment="Hero art should use the brand illustration set.",
        )
        archived_comment = self._comment(body="One off fix.", component_type="ArticleProse")
        archived = self._distill(self._candidate_for(archived_comment), scope="one_off")
        archived.promotion_state = ContentFactoryHealingPromotionState.ARCHIVED
        archived.save(update_fields=["promotion_state", "updated_at"])

        response = self.client.get("/api/v1/vibe-marketing/learned-rules/")

        self.assertEqual(response.status_code, 200, response.data)
        rules = response.data["rules"]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule["id"], visible.id)
        self.assertEqual(rule["rule"], "Prefer brand illustrations over stock photography.")
        self.assertEqual(rule["scope"], "durable_preference")
        self.assertEqual(rule["appliesToComponentTypes"], ["ArticleHeroHeader"])
        self.assertEqual(rule["specAmendment"], "Hero art should use the brand illustration set.")
        self.assertFalse(rule["foldedToSpec"])

        with_archived = self.client.get("/api/v1/vibe-marketing/learned-rules/?include_archived=1")
        self.assertEqual(len(with_archived.data["rules"]), 2)

    def test_retract_archives_and_refolds_promoted_rule(self):
        record = self._distill(
            self._candidate_for(self._comment()),
            scope="durable_preference",
            rule="Keep hero headlines under eight words.",
            targets=["ArticleHeroHeader"],
        )
        record.promotion_state = ContentFactoryHealingPromotionState.PROMOTED
        record.save(update_fields=["promotion_state", "updated_at"])

        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_editorial_learnings_apply",
            return_value={"status": "queued"},
        ) as fold_call:
            response = self.client.delete(f"/api/v1/vibe-marketing/learned-rules/{record.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        fold_call.assert_called_once()
        record.refresh_from_db()
        self.assertEqual(record.promotion_state, ContentFactoryHealingPromotionState.ARCHIVED)
        self.assertTrue(record.promoted_payload["retracted_at"])
        self.assertEqual(response.data["rule"]["status"], "archived")

    def test_retract_candidate_does_not_trigger_fold(self):
        record = self._distill(
            self._candidate_for(self._comment()),
            scope="durable_preference",
            rule="Rule.",
            targets=["ArticleHeroHeader"],
        )

        with patch(
            "content_factory.vibe_marketing_views._call_content_factory_editorial_learnings_apply"
        ) as fold_call:
            response = self.client.delete(f"/api/v1/vibe-marketing/learned-rules/{record.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        fold_call.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.promotion_state, ContentFactoryHealingPromotionState.ARCHIVED)

    def test_rules_are_scoped_to_the_founders_domain(self):
        other_org = Organization.objects.create(name="Other", domain="other.com")
        ContentFactoryHealingRecord.objects.create(
            organization=other_org,
            domain="other.com",
            github_repo="other/site",
            failure_kind=FEEDBACK_KIND,
            failure_family_key="other-fam",
            snippet_or_rule="Other site rule.",
        )

        response = self.client.get("/api/v1/vibe-marketing/learned-rules/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["rules"], [])
