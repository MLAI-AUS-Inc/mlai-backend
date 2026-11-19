"""Sticky stops and honest numbers (plan B3, B4), without a database.

Pawshare (13 Nov 2026): every push re-opened the stopped preparation and paid
for another scan and setup that failed the same way, and the founder message
said "We stopped after 1 attempt" next to 230 worker claims.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import vibe_marketing_views  # noqa: F401 -- decorates with transaction.atomic at import; load before it is patched
from . import website_prepare as prepare
from . import website_prepare_counters as counters
from . import website_prepare_sticky_stop as sticky
from .tests_one_click_operations_unit import PrepareJourneyTests
from .website_contract import WebsiteAuthorityError
from .website_operations import prepare_receipt

OLD, NEW = "a" * 40, "b" * 40
STOP = {"version": 1, "reason_code": "DIRECTORY_DEPENDENCY_VALIDATION_FAILED", "failure_class": "repairable",
        "source_sha": OLD, "relevant_paths": ["src/lib/articles.ts", "src/content/articles/registry.ts"],
        "relevant_routes": ["blog"], "attempt": 1, "held": False}


def compare(*names, status="ahead"):
    return {"status": status, "files": [{"filename": name, "status": "modified"} for name in names]}


class JudgeTests(SimpleTestCase):
    def test_a_change_outside_the_stop_does_not_reopen(self):
        decision = sticky.judge(STOP, compare("README.md", "src/components/Hero.tsx"))
        self.assertEqual((decision["reopen"], decision["reason"]), (False, "no_relevant_change"))

    def test_a_change_to_the_articles_route_reopens(self):
        for name in ("src/app/blog/page.tsx", "src/pages/blog.tsx", "app/(site)/blog/[slug]/page.tsx"):
            with self.subTest(name=name):
                self.assertTrue(sticky.judge(STOP, compare(name))["reopen"])

    def test_a_setup_file_or_build_manifest_reopens(self):
        self.assertTrue(sticky.judge(STOP, compare("src/lib/articles.ts"))["reopen"])
        self.assertTrue(sticky.judge(STOP, compare("package.json"))["reopen"])
        self.assertTrue(sticky.judge(STOP, compare("apps/web/pnpm-lock.yaml"))["reopen"])

    def test_a_route_name_inside_another_word_is_not_the_route(self):
        self.assertFalse(sticky.judge(STOP, compare("src/components/BlogCard.tsx", "docs/weblog-notes.md"))["reopen"])

    def test_unclear_comparisons(self):
        self.assertEqual(sticky.judge(STOP, None), {"reopen": False, "reason": "compare_unreadable"})
        self.assertTrue(sticky.judge(STOP, compare("README.md", status="diverged"))["reopen"])
        many = [f"src/file{index}.ts" for index in range(300)]
        self.assertEqual(sticky.judge(STOP, compare(*many))["reason"], "compare_truncated")


class DecideTests(SimpleTestCase):
    def op(self, **receipt):
        return SimpleNamespace(pk="prepare", state="needs_attention", receipt={"stop_v1": dict(STOP), **receipt})

    def decide(self, op, comparison=None, source=NEW):
        with patch("content_factory.website_source_carry.compare_sources", return_value=comparison) as read:
            return sticky.decide(op, SimpleNamespace(pk="website"), source), read

    def test_transient_waiting_and_moved_source_stops_reopen_without_a_github_read(self):
        for stop in ({"failure_class": "transient"}, {"failure_class": "waiting"},
                     {"reason_code": "website_source_changed", "failure_class": "repairable"}):
            with self.subTest(stop=stop):
                op = self.op()
                op.receipt["stop_v1"].update(stop)
                decision, read = self.decide(op)
                self.assertTrue(decision["reopen"])
                read.assert_not_called()

    def test_staff_held_and_unsupported_stops_never_reopen_on_a_push(self):
        for receipt, stop in (({"setup_dispatch_cap": {"limit": 3}}, {}), ({"child_dispatch_budget": {"scope": "day"}}, {}),
                              ({}, {"failure_class": "unsupported"}), ({}, {"reason_code": "website_prepare_contract_invalid"})):
            with self.subTest(receipt=receipt, stop=stop):
                op = self.op(**receipt)
                op.receipt["stop_v1"].update(stop)
                decision, read = self.decide(op, compare("src/app/blog/page.tsx"))
                self.assertFalse(decision["reopen"])
                read.assert_not_called()

    def test_a_source_dependent_stop_reads_one_compare_from_its_own_commit(self):
        decision, read = self.decide(self.op(), compare("src/app/blog/page.tsx"))
        self.assertTrue(decision["reopen"])
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1:], (OLD, NEW))

    def test_a_judged_push_is_not_read_again(self):
        op = self.op(source_changed_since_stop={"source_sha": NEW, "reopen": False, "reason": "no_relevant_change"})
        decision, read = self.decide(op, compare("src/app/blog/page.tsx"))
        self.assertFalse(decision["reopen"])
        read.assert_not_called()

    def test_a_stop_without_its_commit_stays_stopped(self):
        op = self.op()
        op.receipt["stop_v1"]["source_sha"] = ""
        decision, read = self.decide(op, compare("src/app/blog/page.tsx"))
        self.assertEqual(decision, {"reopen": False, "reason": "no_stop_source"})
        read.assert_not_called()

    def test_completed_preparation_on_a_carried_push_is_left_to_the_free_recheck(self):
        done = SimpleNamespace(pk="prepare", state="completed", receipt={})
        self.assertTrue(sticky.hold_push(SimpleNamespace(pk="website"), done, source_sha=NEW, carried=True))
        self.assertFalse(sticky.hold_push(SimpleNamespace(pk="website"), done, source_sha=NEW, carried=False))
        with override_settings(ARTICLES_PREPARE_STICKY_STOP_ENABLED=False):
            self.assertFalse(sticky.hold_push(SimpleNamespace(pk="website"), done, source_sha=NEW, carried=True))


class BudgetTests(SimpleTestCase):
    def test_scans_count_toward_the_operation_budget(self):
        op = SimpleNamespace(pk="prepare", receipt={}, connection=SimpleNamespace(pk="website", organization_id=7))
        for index in range(sticky.OPERATION_SCAN_LIMIT):
            sticky.record_scan(op, f"scan-{index}")
        sticky.record_scan(op, "scan-0")  # A replayed dispatch counts once.
        self.assertEqual(op.receipt["scan_dispatches"], sticky.OPERATION_SCAN_LIMIT)
        with patch.object(sticky, "daily_scans", return_value=0):
            self.assertEqual(sticky.scan_budget_reached(op, now=None),
                ("operation", sticky.OPERATION_SCAN_LIMIT, sticky.OPERATION_SCAN_LIMIT))
            with override_settings(ARTICLES_PREPARE_STICKY_STOP_ENABLED=False):
                self.assertIsNone(sticky.scan_budget_reached(op, now=None))

    def test_a_prepare_scan_past_the_budget_waits_for_staff(self):
        runner = PrepareJourneyTests()
        op = runner.operation()
        op.receipt = {"scan_dispatches": sticky.OPERATION_SCAN_LIMIT}
        handler = patch.object(sticky, "queue_budget_alert")
        with handler as alert, patch.object(sticky, "daily_scans", return_value=0), \
                patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(SimpleNamespace(company=SimpleNamespace(pk=9),
                    organization=SimpleNamespace(pk=7)), SimpleNamespace(website_connection=op.connection,
                    website_connection_id=op.connection_id), SimpleNamespace(pk=4))), \
                patch.object(prepare, "journey_for_context", return_value={"nextAction": {"id": "scan"},
                    "capabilities": {"canPublishArticle": False}}), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda op, **kwargs: op):
            scan = {"scan": lambda *args, **kwargs: {"run_id": "another-scan"}}
            prepare.advance_prepare(op.pk, handlers=scan)
        self.assertEqual(op.state, "needs_attention")
        self.assertNotIn("child_run_id", op.receipt)
        self.assertEqual(op.receipt["child_dispatch_budget"]["scope"], "operation")
        self.assertFalse(prepare_receipt(op)["owner_can_retry"])
        alert.assert_called_once()


    def test_a_daily_hold_lapses_after_a_day_and_an_operation_hold_waits_for_staff(self):
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.now()
        day = {"child_dispatch_budget": {"scope": "day", "held_at": now.isoformat()}}
        self.assertTrue(sticky.budget_hold_active(day, now=now + timedelta(hours=23)))
        self.assertFalse(sticky.budget_hold_active(day, now=now + timedelta(days=1, minutes=1)))
        operation = {"child_dispatch_budget": {"scope": "operation", "held_at": now.isoformat()}}
        self.assertTrue(sticky.budget_hold_active(operation, now=now + timedelta(days=30)))
        lapsed = {"reason_code": "prepare_no_progress", "child_dispatch_budget": {
            "scope": "day", "held_at": (now - timedelta(days=2)).isoformat()}}
        self.assertTrue(counters.owner_can_retry("needs_attention", lapsed))

    def test_a_finished_preparation_starts_its_scan_budget_and_streak_again(self):
        op = SimpleNamespace(pk="prepare", state="completed", attempts=4, payload={"attempt": 2}, connection=None,
            receipt={"scan_dispatch_run_ids": ["a", "b"], "scan_dispatches": sticky.OPERATION_SCAN_LIMIT,
                     "child_dispatch_budget": {"scope": "operation"}, "stop_streak": {"key": "X", "count": 2}})
        sticky.reset_scan_budget(op)
        counters.observe(op)
        for key in ("scan_dispatch_run_ids", "scan_dispatches", "child_dispatch_budget", "stop_streak"):
            self.assertNotIn(key, op.receipt)
        with patch.object(sticky, "daily_scans", return_value=0):
            self.assertIsNone(sticky.scan_budget_reached(op, now=None))


class HonestNumbersTests(SimpleTestCase):
    """B4: after 3 identical failures the message says 3 and Try again is not offered at the cap."""

    def fail(self, op):
        op.attempts += 1  # One worker claim per failure.
        prepare.prepare_failure(op, "setup", WebsiteAuthorityError(
            "DIRECTORY_DEPENDENCY_VALIDATION_FAILED", "The articles listing does not read the registry.", retryable=False))

    def reopen(self, op):
        values = prepare._source_recheck_values(op, message="Rechecking the changed website source.")
        op.state, op.receipt, op.payload = values["state"], values["receipt"], values["payload"]

    def test_the_message_counts_identical_stops_across_reopens(self):
        op = PrepareJourneyTests().operation()
        op.state = "running"
        self.fail(op)
        first = prepare_receipt(op)
        self.assertEqual(op.state, "needs_attention")
        self.assertIn("We stopped after 1 attempt:", first["message"])
        self.assertTrue(first["owner_can_retry"])
        self.assertFalse(first["retryable"])
        self.reopen(op)
        self.fail(op)
        second = prepare_receipt(op)
        self.assertIn("We stopped after 2 attempts:", second["message"])
        self.assertIn("Use Try again", second["message"])
        self.assertTrue(second["owner_can_retry"])
        self.reopen(op)
        self.fail(op)
        third = prepare_receipt(op)
        self.assertIn("We stopped after 3 attempts:", third["message"])
        self.assertNotIn("Try again", third["message"])
        self.assertIn("told the MLAI team", third["message"])
        self.assertFalse(third["owner_can_retry"])
        # ``retryable`` keeps meaning "the system retries by itself".
        self.assertFalse(third["retryable"])
        self.assertEqual((op.receipt["failures_total"], op.receipt["tries"]), (3, 3))

    def test_counters_never_reset_on_try_again_or_a_recheck(self):
        op = PrepareJourneyTests().operation()
        op.state = "running"
        self.fail(op)
        counters.count_child(op, "scan-1")
        counters.count_child(op, "scan-1")
        self.reopen(op)
        for key in ("failures_total", "child_runs", "stop_streak"):
            self.assertIn(key, op.receipt)
        self.assertEqual(op.receipt["child_runs"], 1)
        self.assertEqual(op.receipt["failures_total"], 1)

    def test_a_different_failure_starts_a_new_streak(self):
        op = PrepareJourneyTests().operation()
        op.state = "running"
        self.fail(op)
        self.reopen(op)
        self.fail(op)
        self.reopen(op)
        op.attempts += 1
        prepare.prepare_failure(op, "setup", WebsiteAuthorityError("DIRECTORY_ROUTE_SOURCE_MISSING", "No route.", retryable=False))
        receipt = prepare_receipt(op)
        self.assertIn("We stopped after 1 attempt:", receipt["message"])
        self.assertTrue(receipt["owner_can_retry"])
        self.assertEqual(op.receipt["failures_total"], 3)

    def test_a_replayed_save_of_one_stop_is_one_stop(self):
        op = PrepareJourneyTests().operation()
        op.state = "needs_attention"
        op.receipt = {"reason_code": "X_FAILED", "failure_class": "repairable"}
        counters.observe(op)
        counters.observe(op)
        self.assertEqual(op.receipt["stop_streak"]["count"], 1)

    def test_owner_retry_by_state(self):
        self.assertTrue(counters.owner_can_retry("cancelled", {}))
        self.assertFalse(counters.owner_can_retry("pending", {}))
        self.assertFalse(counters.owner_can_retry("needs_attention", {"setup_dispatch_cap": {"limit": 3}}))
        self.assertTrue(counters.owner_can_retry("needs_attention", {"reason_code": "X"}))


class PageChoiceStopTests(SimpleTestCase):
    """Plan A9 follow-up: under the failure policy the page choice stays an ask, not a "Try again" stop."""

    @override_settings(ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
    def test_the_page_choice_is_needs_user_whatever_the_policy_view(self):
        op = PrepareJourneyTests().operation()
        op.state = "running"
        op.attempts = 1
        error = prepare._page_choice_required(["/blog", "/news"])
        from . import website_failure_contract as contract
        real = contract.resolve_failure

        def p2_view(*args, **kwargs):
            # The registry's P2 view files the page choice as strategy with no action.
            return {**real(*args, **kwargs), "failure_class": "strategy"}
        with patch.object(contract, "resolve_failure", side_effect=p2_view):
            prepare.prepare_failure(op, "setup", error)
        self.assertEqual(op.state, "needs_user")
        self.assertEqual(op.receipt["user_action"]["id"], "choose_articles_page")
        self.assertEqual(op.receipt["failure_class"], "needs_user")
