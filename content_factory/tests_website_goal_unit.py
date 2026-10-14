"""Goal proof truth tables and real runner regressions without database access."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_goal as goals, website_prepare as prepare
from . import tests_one_click_operations_unit as fixtures


def scope():
    return {"company_id": "9", "organization_id": "7", "connection_id": "website",
        "connection_generation": 3, "repository_id": "12", "branch": "main", "app_root": "",
        "configuration_revision": 1, "deletion_epoch": 0}


def proof_set(now, *, target="ready"):
    return {name: goals.make_proof(name, sorted(goals.PROOF_KINDS[name])[0], scope=scope(),
        source_sha="a" * 40, sha="a" * 40, run_id="child", evidence={"name": name}, at=now)
        for name in goals.MILESTONES[:7 if target == "ready" else 9]}


class GoalProofTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.goal = goals.new_goal("prepare", scope(), now=self.now)

    def test_every_milestone_requires_its_exact_proof(self):
        proofs = proof_set(self.now)
        for name in goals.MILESTONES[:7]:
            with self.subTest(name=name):
                broken = deepcopy(proofs)
                broken.pop(name)
                result = goals.refresh_goal(self.goal, broken, scope=scope(), source_sha="a" * 40, now=self.now)
                self.assertEqual(goals.next_milestone(result), name)
                self.assertFalse(goals.complete(result))
        self.assertTrue(goals.complete(goals.refresh_goal(self.goal, proofs, scope=scope(), source_sha="a" * 40, now=self.now)))

    def test_wrong_kind_scope_source_digest_or_future_timestamp_never_passes(self):
        original = proof_set(self.now)["previewed"]
        for update in ({"kind": "worker_completed"}, {"scope": {**scope(), "company_id": "other"}},
                       {"scope": {**scope(), "connection_generation": True}}, {"source_sha": "b" * 40},
                       {"sha": None}, {"evidence_digest": "invalid"}, {"at": "invalid"},
                       {"at": (self.now + timedelta(hours=1)).isoformat()},
                       {"at": (self.now - timedelta(days=8)).isoformat()}):
            with self.subTest(update=update):
                self.assertFalse(goals.valid_proof("previewed", {**original, **update}, scope=scope(), source_sha="a" * 40, now=self.now))

    def test_first_article_target_does_not_finish_at_live(self):
        goal = goals.new_goal("prepare", scope(), target="first_article_live", now=self.now)
        goal = goals.refresh_goal(goal, proof_set(self.now), scope=scope(), source_sha="a" * 40, now=self.now)
        self.assertEqual(goals.next_milestone(goal), "first_draft")
        self.assertFalse(goals.complete(goal))
        goal = goals.refresh_goal(goal, proof_set(self.now, target="first_article_live"), scope=scope(), source_sha="a" * 40, now=self.now)
        self.assertTrue(goals.complete(goal))

    def test_source_movement_invalidates_proof_and_preserves_bounded_history(self):
        goal = goals.refresh_goal(self.goal, proof_set(self.now), scope=scope(), source_sha="a" * 40, now=self.now)
        result = goals.refresh_goal(goal, proof_set(self.now), scope=scope(), source_sha="b" * 40, now=self.now)
        self.assertFalse(goals.complete(result))
        self.assertEqual(result["milestones"][1]["state"], "invalidated")
        self.assertTrue(result["proof_history"])

    def test_all_failure_classes_count_once_and_waiting_never_counts(self):
        for name in goals.CLASSES:
            with self.subTest(name=name):
                goal = goals.record_failure(self.goal, failure_class=name, fingerprint="c" * 64, occurrence="occurrence")
                goal = goals.record_failure(goal, failure_class=name, fingerprint="c" * 64, occurrence="occurrence")
                self.assertEqual(goal["attempts_by_class"][name], 0 if name == "waiting" else 1)
                self.assertEqual(len(goal["failure_occurrences"]), 0 if name == "waiting" else 1)

    def test_malformed_goal_cannot_complete_with_fake_ticks(self):
        goal = deepcopy(self.goal)
        goal["milestones"] = [{"required": False, "state": "passed"} for _ in goals.MILESTONES]
        self.assertFalse(goals.complete(goal))


@override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
                   ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
                   ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
                   ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class GoalRunnerRegressions(SimpleTestCase):
    def operation(self):
        op = fixtures.PrepareJourneyTests().operation()
        op.connection.repository_id = 12
        op.connection.branch, op.connection.app_root = "main", ""
        op.connection.configuration_version = 1
        op.connection.blockers = []
        op.receipt["goal"] = goals.new_goal(str(op.pk), scope(), now=timezone.now())
        return op

    def test_legacy_can_publish_flag_without_proofs_cannot_finish_an_enrolled_goal(self):
        op = self.operation()
        context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7))
        config = SimpleNamespace(website_connection=op.connection)
        # Isolated orchestrator regression; the accompanying real-chain fixture
        # executes the context loader, journey, proof reader and dispatcher.
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(context, config, SimpleNamespace(pk=4))), \
                patch.object(prepare, "journey_for_context", return_value={"capabilities": {"canPublishArticle": True}}), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kw: operation):
            prepare.advance_prepare(op.pk, handlers={"verify-access": Mock(return_value={"status": "completed"})})
        self.assertNotEqual(op.state, "completed")

    def test_lost_compare_and_set_does_not_return_uncommitted_goal(self):
        op = self.operation()
        op._claimed_at = op.updated_at
        op._claimed_goal_revision = 0
        with patch.object(prepare.WebsiteConnectionOperation.objects, "filter") as rows:
            rows.return_value.filter.return_value = rows.return_value
            rows.return_value.update.return_value = 0
            self.assertIsNone(prepare._persist_prepare(op, now=timezone.now()))


    def test_double_click_reuses_the_current_goal_and_stale_completed_proof_creates_successor(self):
        from . import website_goal_runner as runner
        from .website_models import WebsiteConnectionOperation
        from . import tests_prepare_journey_unit as journey_fixtures
        op = self.operation()
        op.payload["company_id"] = "9"
        op.connection.verified_sha = "a" * 40
        rows = journey_fixtures.FrozenPrepareContextTests.rows(first=op)
        op.connection.operations = rows
        with patch.object(WebsiteConnectionOperation.objects, "get_or_create") as create:
            self.assertIs(runner.start_goal(op.connection, company_id=9, user=SimpleNamespace(pk=4), retry=False, target="ready", now=timezone.now()), op)
            create.assert_not_called()
            op.state = "completed"
            op.receipt["goal"]["completed_source_sha"] = "a" * 40
            successor = self.operation()
            create.return_value = (successor, True)
            self.assertIs(runner.start_goal(op.connection, company_id=9, user=SimpleNamespace(pk=4), retry=False, target="ready", now=timezone.now()), successor)
            self.assertEqual(successor.receipt["goal"]["predecessor_goal_id"], str(op.pk))

    def test_b1_canonical_fingerprint_survives_replayed_failures(self):
        goal = goals.record_failure(self.operation().receipt["goal"], failure_class="repairable", fingerprint="sha256:" + "c" * 64, occurrence="one")
        goal = goals.record_failure(goal, failure_class="repairable", fingerprint="sha256:" + "c" * 64, occurrence="one")
        self.assertEqual(goal["fingerprints_seen"], ["sha256:" + "c" * 64])
        self.assertEqual(goal["attempts_by_class"]["repairable"], 1)
