"""C7 tier 4 "delivered, not yet live": never Ready, never published, never the free article; no database."""
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import tests_one_click_operations_unit as operation_fixtures
from . import website_content_files as content_files
from . import website_prepare as prepare
from . import website_staff_fixing as staff

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=dt_timezone.utc)
PR = "https://github.com/acme/site/pull/12"
LADDER = {
    "schema_version": 1, "outcome": "delivered_not_live", "tier": "content_files", "tier_number": 4,
    "counts_as_published": False, "consumes_free_article": False,
    "rungs": [{"tier": "native", "outcome": "left", "reason": "no_adapter"},
              {"tier": "agentic", "outcome": "left", "reason": "repair_exhausted"},
              {"tier": "static_dropin", "outcome": "left", "reason": "spa_host_needs_host_preview_proof"},
              {"tier": "content_files", "outcome": "passed"}],
    "content_files": {"pr_url": PR, "pr_number": 12, "content_root": "mlai-content-files"},
}
ENTITLED = {"roo_points_billing_status": "entitled", "roo_points_entitlement": "first_article"}


def _operation():
    op = operation_fixtures.PrepareJourneyTests().operation()
    op.connection.organization = SimpleNamespace(pk=7, domain="example.com")
    op.connection.capabilities = {}
    op.connection.github_repo = "acme/site"
    return op


def _child(result):
    return SimpleNamespace(run_id="setup-run-1", workflow="article_system_setup", status="pr_opened",
                           organization_id=7, result=result, error="")


class _NoSideEffects:
    def __init__(self, case):
        self.alert = Mock()
        for target, mock in (("content_factory.founder_journey_alerts.queue_founder_journey_alert", self.alert),
                             ("content_factory.founder_transitions.notify_staff_fixing", Mock()),
                             ("content_factory.website_first_article_refunds.reserve_refund", Mock()),
                             ("founder_tools.website_leads.record_staff_lead", Mock())):
            patcher = patch(target, mock)
            patcher.start()
            case.addCleanup(patcher.stop)


class ReadingTheLadderReceiptTests(SimpleTestCase):
    def test_tier_four_is_read_from_the_result_or_its_setup(self):
        self.assertEqual(content_files.setup_delivered_not_live({"setup_ladder": LADDER}), LADDER)
        self.assertEqual(content_files.setup_delivered_not_live({"article_system_setup": {"setup_ladder": LADDER}}), LADDER)

    def test_anything_else_is_not_this_state(self):
        for ladder in ({**LADDER, "counts_as_published": True}, {**LADDER, "tier": "static_dropin"},
                       {**LADDER, "outcome": "ready"}, {**LADDER, "counts_as_published": None}):
            self.assertIsNone(content_files.setup_delivered_not_live({"setup_ladder": ladder}))
        self.assertIsNone(content_files.setup_delivered_not_live(None))
        self.assertIsNone(content_files.setup_delivered_not_live({"setup_ladder": "delivered_not_live"}))


@override_settings(ARTICLES_SETUP_LADDER_ENABLED=True, ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True)
class DeliveredNotLivePreparationTests(SimpleTestCase):
    def setUp(self):
        self.mocks = _NoSideEffects(self)

    def test_the_founder_sees_delivered_not_live_with_the_pull_request_and_no_try_again(self):
        op = _operation()
        op.receipt = {"reason_code": "DIRECTORY_REPAIR_EXHAUSTED", "failure": {"code": "X"}, "user_action": {"id": "open_run"}}
        content_files.settle_prepare(op, _child({"setup_ladder": LADDER}), LADDER, now=NOW)
        self.assertEqual(op.state, "needs_attention")
        self.assertIsNone(op.next_attempt_at)
        self.assertEqual(op.receipt["status"], "delivered_not_live")
        self.assertIn("not live yet", op.receipt["message"])
        self.assertEqual(op.receipt["content_files"]["pr_url"], PR)
        self.assertEqual(op.receipt["content_files"]["pr_number"], 12)
        for key in ("user_action", "failure", "reason_code"):
            self.assertNotIn(key, op.receipt)
        from .website_operations import prepare_receipt
        from .website_prepare_counters import owner_can_retry
        projected = prepare_receipt(op)
        self.assertEqual(projected["status"], "delivered_not_live")
        self.assertFalse(projected["owner_can_retry"])
        self.assertFalse(owner_can_retry(op.state, op.receipt))

    def test_the_founder_sentence_fits_what_clients_show(self):
        # Desktop and mobile show a server sentence only up to 240 characters.
        self.assertLessEqual(len(content_files.MESSAGE), 240)
        self.assertIn("not live yet", content_files.MESSAGE)

    def test_a_pull_request_url_off_github_is_never_shown(self):
        op = _operation()
        ladder = {**LADDER, "content_files": {"pr_url": "https://evil.example/acme/site/pull/1"}}
        content_files.settle_prepare(op, _child({"setup_ladder": ladder}), ladder, now=NOW)
        self.assertNotIn("pr_url", op.receipt["content_files"])
        self.assertEqual(op.receipt["status"], "delivered_not_live")

    def test_an_open_staff_fix_ends_with_the_honest_answer_not_ready(self):
        op = _operation()
        staff.enter(op, now=NOW, code="DIRECTORY_REPAIR_EXHAUSTED", step="setup",
                    repair_receipt={"exhausted": True, "stop": "repair_cap_reached", "rounds": 3})
        content_files.settle_prepare(op, _child({"setup_ladder": LADDER}), LADDER, now=NOW)
        self.assertEqual(op.receipt["staff_fixing"]["exit"], "delivered_not_live")
        self.assertFalse(staff.active(op))
        self.assertEqual(op.receipt["status"], "delivered_not_live")
        self.assertNotIn("hold_owner", op.receipt)
        self.assertNotEqual(op.state, "completed")

    @override_settings(ARTICLES_SETUP_LADDER_ENABLED=False)
    def test_flag_off_a_content_files_setup_is_staff_fixing_never_ready(self):
        op = _operation()
        content_files.settle_prepare(op, _child({"setup_ladder": LADDER}), LADDER, now=NOW)
        self.assertEqual(op.receipt["status"], "staff_fixing")
        self.assertEqual(op.state, "needs_attention")
        self.assertNotIn("content_files", op.receipt)

    def test_prepare_routes_a_tier_four_setup_child_here_and_never_merges_it(self):
        op = _operation()
        op.receipt = {"child_run_id": "setup-run-1", "current_step": "setup"}
        child = _child({"setup_ladder": LADDER, "article_system_setup": {"status": "pr_created"}})
        config = SimpleNamespace(website_connection=op.connection, website_connection_id=op.connection_id, article_system={})
        context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7))
        journey = {"nextAction": {"id": "setup"}, "capabilities": {"canPublishArticle": False}}
        runs = Mock()
        runs.filter.return_value.first.return_value = child
        merge = Mock()
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(context, config, SimpleNamespace(pk=4))), \
                patch.object(prepare, "journey_for_context", return_value=journey), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kwargs: operation), \
                patch.object(prepare, "_reconcile_setup_approval_receipt"), \
                patch.object(prepare, "maybe_merge_prepared_setup", merge), \
                patch("content_factory.website_first_article_billing.reservation_ready", return_value=True), \
                patch("content_factory.website_superseded_setup_prs.close_superseded_setup_prs"), \
                patch("workflow_runs.models.ContentFactoryRun.objects", runs):
            result = prepare.advance_prepare(op.pk, handlers={"setup": Mock(return_value={"status": "completed"})})
        self.assertEqual(result.state, "needs_attention")
        self.assertEqual(result.receipt["status"], "delivered_not_live")
        self.assertEqual(result.receipt["content_files"]["pr_url"], PR)
        merge.assert_not_called()


class NeverPublishedTests(SimpleTestCase):
    def _article(self, **values):
        from .models import ArticlePublishStatus
        base = {"publish_status": ArticlePublishStatus.MERGED, "on_main_verified_at": NOW, "content_path": ""}
        return SimpleNamespace(**{**base, **values})

    def test_a_content_file_on_main_is_still_publishing(self):
        from .article_publish_status import article_bucket
        self.assertEqual(article_bucket(self._article(content_path="mlai-content-files/first-post/index.md")), "publishing")

    def test_only_the_live_check_of_its_real_url_publishes_it(self):
        from .article_publish_status import article_bucket
        from .models import ArticlePublishStatus
        self.assertEqual(article_bucket(self._article(content_path="mlai-content-files/first-post/index.md",
                                                      publish_status=ArticlePublishStatus.LIVE)), "published")

    def test_an_ordinary_article_on_main_keeps_counting_as_published(self):
        from .article_publish_status import article_bucket
        self.assertEqual(article_bucket(self._article(content_path="src/content/blog/first-post.md")), "published")
        self.assertEqual(article_bucket(self._article(on_main_verified_at=None)), "publishing")

    def test_going_live_settles_the_free_article_only_for_content_files(self):
        from . import article_publish_status as status
        settle = Mock()
        with patch("content_factory.first_article_entitlement.settle_entitlement_for_run", settle), \
                patch.object(status, "_source_run_for_article", return_value="run"):
            status._settle_content_files_entitlement(self._article(content_path="src/blog/x.md", pk=1))
            settle.assert_not_called()
            status._settle_content_files_entitlement(self._article(content_path="mlai-content-files/x/index.md", pk=2))
            settle.assert_called_once_with("run")


class FreeArticleNotConsumedTests(SimpleTestCase):
    def _run(self, result):
        return SimpleNamespace(run_id="article-run-1", organization_id=7, status="completed", run_request=dict(ENTITLED),
                               result=result)

    def test_a_content_files_delivery_is_not_the_free_articles_delivery(self):
        from . import first_article_entitlement as entitlement
        consume = Mock()
        for result in ({"content_files_delivery": {"kind": "content_files_v1", "state": "delivered_not_live"}},
                       {"setup_ladder": LADDER}):
            with self.subTest(result=result), \
                    patch.object(content_files, "run_has_live_article", return_value=False), \
                    patch.object(entitlement, "consume_entitlement", consume):
                run = self._run(result)
                self.assertFalse(entitlement._delivered(run))
                entitlement.settle_entitlement_for_run(run)
        consume.assert_not_called()

    def test_once_an_article_of_the_run_is_live_the_free_article_is_used(self):
        from . import first_article_entitlement as entitlement
        consume = Mock()
        run = self._run({"content_files_delivery": {"state": "delivered_not_live"}})
        with patch.object(content_files, "run_has_live_article", return_value=True), \
                patch("content_factory.run_reconciliation.authoritative_run", return_value=False), \
                patch.object(entitlement, "consume_entitlement", consume):
            self.assertTrue(entitlement._delivered(run))
            entitlement.settle_entitlement_for_run(run)
        consume.assert_called_once()

    def test_an_ordinary_completed_run_still_delivers(self):
        from . import first_article_entitlement as entitlement
        with patch("content_factory.run_reconciliation.authoritative_run", return_value=False):
            self.assertTrue(entitlement._delivered(self._run({"article": {"status": "ready"}})))
