"""Past publication survives freshness expiry only with actual private lineage."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import cms_goal_achievement as achievement
from . import cms_goal_contract as goals
from . import cms_goal_consent as consent
from . import cms_goal_budget as budget
from . import cms_goal_status as status
from . import cms_goal_evidence as evidence
from . import cms_goal_start as start
from . import cms_article_publication as publications
from . import tests_cms_goal_evidence_unit as fixtures
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


@override_settings(**{**fixtures.CmsGoalEvidenceTests._overridden_settings,
    "ARTICLES_CMS_GOALS_ENABLED": True, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": True,
    "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED": True, "ARTICLES_CMS_STATUS_ENABLED": True,
    "ARTICLES_WEBSITE_DOCTOR_ENABLED": True, "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED": True})
class CmsGoalAchievementTests(SimpleTestCase):
    platform = "wordpress"
    reserve = fixtures.CmsGoalEvidenceTests.reserve
    set_handoff = fixtures.CmsGoalEvidenceTests.set_handoff
    acknowledge = fixtures.CmsGoalEvidenceTests.acknowledge
    advance = fixtures.CmsGoalEvidenceTests.advance
    collect = fixtures.CmsGoalEvidenceTests.collect

    def setUp(self):
        fixtures.CmsGoalEvidenceTests.setUp(self)
        self.enrollment = override_settings(ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)],
            ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=[str(self.org.pk)])
        self.enrollment.enable(); self.addCleanup(self.enrollment.disable)
        self.goal = goals.new_goal(self.goal["goal_id"], self.scope, now=self.now - timedelta(seconds=2))
        self.advance(); self.advance(); self.advance()
        self.completed_at = goals._time(self.operation.payload["public_observation"]["checked_at"]) + timedelta(seconds=1)
        proofs, original, references = self.collect()
        self.goal = goals.bind_source(self.goal, original, expected_scope=self.scope, now=self.completed_at)
        self.goal = goals.refresh(self.goal, proofs, expected_scope=self.scope, now=self.completed_at)
        self.assertTrue(goals.complete(self.goal, expected_scope=self.scope, now=self.completed_at))
        choice = consent.seal(self.goal, user_id=self.user.pk, review_policy="auto", expected_points=6,
            confirmed=True, now=goals._time(self.goal["started_at"]))
        policy = evidence.policies.current_policy(self.context, self.config, user=self.user)
        self.root = WebsiteConnectionOperation(id=self.goal["goal_id"], action="cms-goal", connection=self.connection,
            generation=self.binding["generation"], state="completed", next_attempt_at=None,
            payload={**start.original_payload(self.context, binding=self.binding, policy=policy, user=self.user, choice=choice),
                "source_run_id": self.run.run_id, "capture_operation_id": str(self.capture.pk)},
            receipt={"goal": self.goal, "native_budget": budget.initial(self.goal), "references": references,
                "status": "first_article_live", "current_step": "first_article_live"})
        self.root.receipt["publication_achievement"] = achievement.capture(self.goal, self.operation, now=self.completed_at)
        self.root.updated_at = self.completed_at
        self.connection.refresh_from_db = Mock()
        self.saved.append(self.root)
        self.fetch.reset_mock(); self.http.reset_mock(); self.public_get.reset_mock()

    def read(self, **options):
        return achievement.read(self.root, user=self.user, now=options.get("now", self.completed_at + timedelta(days=30)))

    def reseal(self, value):
        value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})

    def test_actual_publication_is_retained_without_claiming_fresh_live_health(self):
        self.assertFalse(goals.complete(self.goal, expected_scope=self.scope, now=self.completed_at + timedelta(hours=1)))
        before = deepcopy((self.root.payload, self.root.receipt, self.operation.payload))
        value = self.read()
        self.assertEqual(value["public_url"], self.operation.payload["public_observation"]["public_url"])
        self.assertEqual(value["published_at"], self.operation.payload["public_observation"]["checked_at"])
        self.assertEqual(value["goal"], self.goal)
        self.assertEqual(before, (self.root.payload, self.root.receipt, self.operation.payload))
        self.fetch.assert_not_called(); self.http.assert_not_called(); self.public_get.assert_not_called()

    def test_original_completion_uses_the_existing_bounded_clock_allowance(self):
        self.assertEqual(self.read(now=self.completed_at - timedelta(seconds=1))["published_at"],
            self.operation.payload["public_observation"]["checked_at"])
        with self.assertRaises(WebsiteAuthorityError): self.read(now=self.completed_at - timedelta(seconds=31))

    def test_readonly_status_keeps_original_proof_dates_and_historical_wording(self):
        with patch.object(status.dispatch, "original_dispatch", return_value=None):
            value = status.project(self.root, self.context, self.config, user=self.user, binding=self.binding,
                now=self.completed_at + timedelta(days=30))
        self.assertEqual((value["state"], value["title"], value["action"], value["nextAttemptAt"]),
            ("done", "First article published", None, None))
        self.assertEqual(value["publishedAt"], self.read()["published_at"])
        self.assertTrue(all(not row["required"] or row["state"] == "passed" for row in value["milestones"]))
        self.assertEqual(next(row["at"] for row in value["evidence"] if row["id"] == "milestone:first_article_live"), value["publishedAt"])
        self.fetch.assert_not_called(); self.http.assert_not_called(); self.public_get.assert_not_called()

    def test_same_post_get_recheck_cannot_erase_or_freshen_original_publication(self):
        original = self.read()
        publications.request_public_recheck(self.operation, now=self.completed_at + timedelta(hours=1))
        self.assertIsNone(self.operation.payload["public_observation"])
        self.assertEqual(self.operation.state, "pending")
        self.assertEqual(self.read(), original)
        self.http.assert_not_called(); self.public_get.assert_not_called()

    def test_current_owner_key_and_original_paid_approval_are_still_required(self):
        self.user.is_active = False
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.user.is_active = True; self.private.state = "deleted"
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.private.state = "completed"; self.refund.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.read()

    def test_copied_completion_boolean_url_or_checksum_without_private_snapshot_is_not_success(self):
        snapshot = self.root.receipt.pop("publication_achievement")
        self.root.receipt.update(publishing_verified=True, public_url=snapshot["observation"]["public_url"])
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.root.receipt["publication_achievement"] = snapshot
        self.saved.remove(self.operation)
        with self.assertRaises(WebsiteAuthorityError): self.read()

    def test_rechecksummed_foreign_reference_source_body_timestamp_or_goal_is_refused(self):
        original = deepcopy(self.root.receipt["publication_achievement"])
        variants = [{"schema_version": True}, {"extra": True}, {"goal_digest": "f" * 64},
            {"completed_at": (self.completed_at - timedelta(days=1)).isoformat()},
            {"completed_at": (self.completed_at + timedelta(days=40)).isoformat()}]
        for changes in variants:
            candidate = {**deepcopy(original), **changes}; self.reseal(candidate)
            self.root.receipt["publication_achievement"] = candidate
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.read()
        for key, changed in (("provider_id", 999), ("source_run_id", "another-paid-run"),
                ("body_source_sha256", "f" * 64), ("checked_at", (self.completed_at - timedelta(hours=1)).isoformat())):
            candidate = deepcopy(original); candidate["observation"][key] = changed
            self.reseal(candidate["observation"]); self.reseal(candidate)
            self.root.receipt["publication_achievement"] = candidate
            with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError): self.read()

    def test_running_or_different_goal_generation_cannot_use_a_past_achievement(self):
        self.root.state = "running"
        with self.assertRaises(WebsiteAuthorityError): self.read()
        self.root.state = "completed"; self.run.result["generation"] += 1
        with self.assertRaises(WebsiteAuthorityError): self.read()

    def test_achievement_cannot_replace_fresh_body_checks_or_start_another_publication(self):
        with self.assertRaises(WebsiteAuthorityError):
            publications.current_publication(self.operation, now=self.completed_at + timedelta(hours=1))
        self.assertEqual(self.read()["goal"], self.goal)
        self.assertEqual(self.operation.payload["post_state"]["create_count"], 1)
        self.http.assert_not_called(); self.public_get.assert_not_called()
