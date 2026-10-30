"""P0.2b: one free first article per startup, kept as zero-point ledger rows.

29 Oct 2026 funnel: 0 of 80 new users reached an article and 45 of them had 0
points. A new startup now gets its first copy-ready article free, with at most
two attempts, one live grant per account email and three grants in any outcome
per rolling 30 days.
"""
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from core.models import User
from integrations.services.article_generation import InsufficientRooPointsError
from organizations.models import Organization
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun

from . import first_article_entitlement as first_article
from . import vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .models import OrganizationContentConfig

FREE = {"ARTICLES_FREE_FIRST_ARTICLE_ENABLED": True, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": False,
    "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 25}


def free_rows(org=None):
    rows = Ledger.objects.filter(reference_type=first_article.REFERENCE_TYPE)
    if org is not None:
        rows = rows.filter(idempotency_key__startswith=f"content_factory:first_article:org:{org.pk}:")
    return rows


def keys(org):
    prefix = f"content_factory:first_article:org:{org.pk}:"
    return sorted(key.removeprefix(prefix) for key in free_rows(org).values_list("idempotency_key", flat=True))


class EntitlementFixture:
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="founder@synthetic.example.test")
        self.org = self.make_org("first.example.test")
        self.debits = []
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        self.enterContext(patch.object(views, "_refresh_article_editorial_payload", return_value=None))
        self.enterContext(patch.object(views, "_validate_article_start_admission"))
        self.enterContext(patch.object(views, "_roo_points_balance_for_user", return_value=0))
        self.enterContext(patch.object(views, "charge_content_factory_request_for_user", side_effect=self.debit))
        self.alerts = self.enterContext(patch.object(first_article, "_queue_alert"))

    def debit(self, **kwargs):
        # Every founder here has 0 points: a paid start can never succeed.
        self.debits.append(kwargs)
        raise InsufficientRooPointsError({"code": "roo_points_required", "requiredPoints": 12, "currentBalance": 0})

    @staticmethod
    def make_org(domain, **config):
        org = Organization.objects.create(domain=domain, name=domain)
        OrganizationContentConfig.objects.create(organization=org, **config)
        return org

    def start(self, *, org=None, user=None, key="first-request", expected=0, mode="content_only", topic="First article"):
        org, user = org or self.org, user or self.user
        payload = {"domain": org.domain, "github_repo": "", "topic": topic, "client_request_id": key,
            "delivery_mode": mode, "delivery_mode_confirmed": True, "delivery_mode_explicit": True,
            "slack_user_id": views.founder_actor_id_for_user(user)}
        result = views._charge_roo_points_for_article(SimpleNamespace(user=user, data={"expectedCostPoints": expected}),
            context=SimpleNamespace(organization=org), payload=payload)
        self.charged = result
        return result[3], payload

    def started(self, **kwargs):
        error, payload = self.start(**kwargs)
        self.assertIsNone(error, getattr(error, "data", None))
        return payload

    def make_run(self, payload, *, org=None, status="running", step="research_topic", result=None, run_id=None):
        org = org or self.org
        return ContentFactoryRun.objects.create(run_id=run_id or f"run-{uuid4().hex[:12]}", workflow="article_generation",
            domain=org.domain, organization=org, status=status, current_step=step, run_request=payload,
            result=result if result is not None else {"generation": 0, "state_version": 4})

    def state(self, org=None, user=None):
        return first_article.entitlement_state(org or self.org, user or self.user)

    @staticmethod
    def stop_receipt(run, **changes):
        """Content Factory's bound no-effect stop receipt for a content-only draft."""
        from .website_failure_contract import canonical_bytes
        receipt = {"version": 1, "purpose": "article_cancellation", "run_id": run.run_id, "goal_id": None,
            "scope": {"github_repo": None, "client_request_id": run.run_request["client_request_id"]},
            "generation": 0, "state_version": 5, "outcome": "no_article_effect", "cancelled": True,
            "delivered_value": False, "watermark": {"operation_id": None, "attempt": None, "cancelled": True}, **changes}
        receipt["receipt_id"] = sha256(canonical_bytes(receipt)).hexdigest()
        return receipt


@override_settings(**FREE)
class FirstArticleStartTests(EntitlementFixture, TestCase):
    def test_zero_balance_first_article_starts_free_with_one_outbox_and_no_spend(self):
        payload = self.started()
        attempt = free_rows(self.org).get()
        self.assertEqual(keys(self.org), ["attempt:1"])
        self.assertEqual((attempt.delta, attempt.kind, attempt.user_id), (0, "ADJUST", self.user.pk))
        self.assertEqual((payload["roo_points_billing_status"], payload["roo_points_entitlement"],
            payload["roo_points_cost"], payload["roo_points_required"], payload["roo_points_ledger_id"]),
            ("entitled", "first_article", 0, 0, str(attempt.pk)))
        # Content Factory never sees a balance or budget for the free article.
        self.assertNotIn("roo_points_balance", payload)
        self.assertEqual(self.debits, [])
        self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())
        outbox = ContentFactoryDispatchOutbox.objects.get()
        self.assertEqual(outbox.payload["request"]["roo_points_billing_status"], "entitled")
        self.assertEqual(self.state()["status"], "in_progress")

    def test_stopped_journey_states_still_reach_a_free_copy_ready_draft(self):
        # No connection, a saved but unverified repository: content-only needs neither.
        saved_repo = self.make_org("saved-repo.example.test", github_repo="founder/site")
        for org in (self.org, saved_repo):
            user = User.objects.create_user(email=f"owner@{org.domain}")
            with self.subTest(org=org.domain):
                payload = self.started(org=org, user=user, key=f"start-{org.pk}")
                self.assertEqual(payload["roo_points_billing_status"], "entitled")
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 2)

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=False)
    def test_flag_off_quotes_the_price_and_reserves_nothing(self):
        error, _ = self.start()
        self.assertEqual((error.status_code, error.data["costPoints"]), (409, 12))
        self.assertFalse(free_rows().exists())
        self.assertEqual(self.state()["limit"], "disabled")

    def test_old_client_review_draft_at_zero_keeps_the_price(self):
        # Older desktops default to review_draft; their 0 quote must not spend an attempt.
        error, _ = self.start(mode="review_draft")
        self.assertEqual((error.status_code, error.data["costPoints"]), (409, 12))
        self.assertFalse(free_rows().exists())
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())

    def test_replay_reserves_once_and_a_second_press_quotes_the_price(self):
        first = self.started()
        replay = self.started()
        self.assertEqual(first["roo_points_ledger_id"], replay["roo_points_ledger_id"])
        self.assertEqual(keys(self.org), ["attempt:1"])
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        error, _ = self.start(key="second-press", topic="Another article")
        self.assertEqual((error.status_code, error.data["costPoints"]), (409, 12))
        self.assertEqual(keys(self.org), ["attempt:1"])

    def test_a_quote_lost_between_check_and_lock_rolls_back_and_quotes_the_price(self):
        with patch.object(first_article, "reserve_entitlement", return_value=None):
            error, _ = self.start()
        self.assertEqual((error.status_code, error.data["costPoints"]), (409, 12))
        self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
        self.assertFalse(free_rows().exists())

    def test_released_attempt_offers_one_retry_then_the_price_and_tells_ops(self):
        first = self.started()
        self.assertTrue(first_article.release_entitlement_for_request(organization=self.org, request=first, reason="test"))
        state = self.state()
        self.assertEqual((state["status"], state["attempts_used"]), ("available", 1))
        # The released request itself stays released: a replay quotes the price.
        error, _ = self.start()
        self.assertEqual(error.data["costPoints"], 12)
        second = self.started(key="retry-request")
        self.assertTrue(first_article.release_entitlement_for_request(organization=self.org, request=second, reason="test"))
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released", "attempt:2", "attempt:2:released"])
        error, _ = self.start(key="third-request")
        self.assertEqual((error.status_code, error.data["costPoints"]), (409, 12))
        self.assertEqual(self.state()["limit"], "attempts")
        self.alerts.assert_any_call(organization=self.org, alert_reason="first_article_attempts_exhausted")
        self.assertEqual(len(keys(self.org)), 4)

    def test_account_cap_spans_startups_and_stops_a_release_loop_after_three(self):
        self.started()
        other = self.make_org("second.example.test")
        # One live free article per account email, across startups.
        self.assertEqual(self.state(other)["limit"], "account")
        first_article.release_entitlement_for_request(organization=self.org,
            request={"client_request_id": "first-request"}, reason="test")
        for number, domain in enumerate(("second.example.test", "third.example.test"), start=2):
            org = other if number == 2 else self.make_org(domain)
            payload = self.started(org=org, key=f"loop-{number}")
            first_article.release_entitlement_for_request(organization=org, request=payload, reason="test")
        fourth = self.make_org("fourth.example.test")
        self.assertEqual(self.state(fourth)["limit"], "account")
        error, _ = self.start(org=fourth, key="loop-4")
        self.assertEqual(error.data["costPoints"], 12)
        self.assertEqual(free_rows().filter(idempotency_key__regex=r":attempt:\d+$").count(), 3)

    def test_established_startups_keep_todays_price(self):
        ContentFactoryRun.objects.create(run_id="paid-run", workflow="article_generation", domain=self.org.domain,
            organization=self.org, status="completed", run_request={"roo_points_billing_status": "charged"})
        self.assertEqual(self.state()["limit"], "established")
        connected = self.make_org("connected.example.test")
        with patch("content_factory.portable_drafts.has_connected_website", return_value=True):
            self.assertEqual(self.state(connected)["limit"], "established")
        error, _ = self.start()
        self.assertEqual(error.data["costPoints"], 12)
        self.assertFalse(free_rows().exists())

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=False)
    def test_flag_off_still_tells_established_startups_from_new_ones(self):
        # IF-21: the client offers the paid copy-ready primary for 'disabled' and
        # keeps the review path for 'established', so the flag must not blur them.
        from .activation import article_capabilities

        def summary(state):
            return state["status"], state["free"], state["topic_batch_free"], state["limit"]

        self.assertEqual(summary(self.state()), ("unavailable", False, False, "disabled"))
        ContentFactoryRun.objects.create(run_id="paid-run", workflow="article_generation", domain=self.org.domain,
            organization=self.org, status="completed", run_request={"roo_points_billing_status": "charged"})
        self.assertEqual(summary(self.state()), ("unavailable", False, False, "established"))
        connected = self.make_org("connected.example.test")
        with patch("content_factory.portable_drafts.has_connected_website", return_value=True):
            self.assertEqual(self.state(connected)["limit"], "established")
        newcomer = self.make_org("newcomer.example.test")
        self.assertEqual(self.state(newcomer)["limit"], "disabled")
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True):
            self.assertEqual((self.state()["limit"], self.state(newcomer)["limit"]), ("established", "disabled"))
        config = OrganizationContentConfig.objects.get(organization=self.org)
        capabilities = article_capabilities(config, domain=self.org.domain, user=self.user, first_article=self.state())
        self.assertEqual((capabilities["prices"]["article"], capabilities["firstArticle"]["limit"]), (12, "established"))
        self.assertFalse(free_rows().exists())

    def test_goal_flag_turns_the_free_article_off_and_warns(self):
        from .checks import free_first_article_goal_check
        with override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True):
            self.assertEqual(self.state()["limit"], "disabled")
            self.assertEqual([warning.id for warning in free_first_article_goal_check(None)], ["content_factory.W001"])
        self.assertEqual(free_first_article_goal_check(None), [])

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=1)
    def test_global_daily_cap(self):
        self.started()
        neighbour = User.objects.create_user(email="neighbour@synthetic.example.test")
        other = self.make_org("neighbour.example.test")
        self.assertEqual(self.state(other, neighbour)["limit"], "daily_cap")

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD=1)
    def test_reservation_spike_alerts_ops_once_an_hour_after_commit(self):
        from django.core.cache import cache
        cache.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.started()
        self.alerts.assert_any_call(organization=self.org, alert_reason="free_cap_spike", run=None)
        self.alerts.reset_mock()
        # Child runs of the free article count too, but ops hear once an hour.
        run = self.make_run({**self.charged[2], "original_billing_source_run_id": "parent"})
        with self.captureOnCommitCallbacks(execute=True):
            first_article.note_entitled_child_run(run)
        self.alerts.assert_not_called()
        self.assertEqual(first_article._recent_child_runs(run.created_at.replace(year=2000)), 1)

    def test_capabilities_quote_zero_while_free(self):
        from .activation import article_capabilities
        config = OrganizationContentConfig.objects.get(organization=self.org)
        capabilities = article_capabilities(config, domain=self.org.domain, user=self.user, first_article=self.state())
        self.assertEqual(capabilities["prices"]["article"], 0)
        self.assertEqual((capabilities["prices"]["researchTopic"], capabilities["prices"]["research"]), (0, 0))
        self.assertEqual(capabilities["firstArticle"], {"version": 1, "status": "available", "free": True,
            "attemptsUsed": 0, "attemptsAllowed": 2, "topicBatchFree": True, "runId": None, "limit": None,
            "deliveryMode": "content_only"})
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=False):
            paid = article_capabilities(config, domain=self.org.domain, user=self.user, first_article=self.state())
        self.assertEqual(paid["prices"]["article"], 12)
        self.assertEqual(paid["firstArticle"]["limit"], "disabled")


@override_settings(**FREE)
class FirstArticleSettlementTests(EntitlementFixture, TestCase):
    def test_delivery_consumes_once_and_reports_used(self):
        run = self.make_run(self.started(), status="completed", step="finalize")
        for _ in range(2):
            first_article.settle_entitlement_for_run(run)
        self.assertEqual(keys(self.org), ["attempt:1", "consumed"])
        self.assertEqual((self.state()["status"], self.state()["run_id"]), ("used", run.run_id))
        # A consumed attempt is never released afterwards.
        self.assertFalse(first_article.release_entitlement(organization=None, run=run, reason="late_failure"))
        self.assertEqual(first_article.entitlement_payload(run), {"entitlementStatus": "consumed",
            "entitlementRetryAvailable": False})

    def cancel(self, run, *, receipt=True):
        """The owner's local cancel, with Content Factory's bound stop receipt or none."""
        remote = {"article_cancellation_v1": self.stop_receipt(run)} if receipt else {}
        return views._cancel_local_article_run(run=run, organization=self.org, remote_data=remote)

    def test_owner_cancel_before_writing_releases_and_after_writing_consumes(self):
        early = self.make_run(self.started(), step="research_topic")
        self.cancel(early)
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])
        self.assertEqual(first_article.entitlement_payload(early), {"entitlementStatus": "released",
            "entitlementRetryAvailable": True})
        late = self.make_run(self.started(key="second-try"), step="draft_section:introduction")
        self.cancel(late)
        self.assertIn("consumed", keys(self.org))
        self.assertEqual(self.state()["status"], "used")

    def test_owner_delete_of_a_failed_free_draft_does_not_use_it_up(self):
        # PawShare-style: the free draft failed at plan_article and delivered nothing.
        run = self.make_run(self.started(), status="failed", step="plan_article")
        self.cancel(run)
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])
        self.assertEqual(self.state()["status"], "available")

    def test_owner_cancel_without_a_stop_receipt_waits_for_the_stop_sweep(self):
        # The worker may still be running: a release now could run two free drafts at once.
        run = self.make_run(self.started(), step="research_topic")
        cancelled = self.cancel(run, receipt=False)
        self.assertEqual(keys(self.org), ["attempt:1"])
        self.assertEqual((cancelled.result["article_terminal_refund"]["status"],
            cancelled.result["reconciliation_refund_pending"]), ("stop_required", True))
        from .article_publication_evidence import exhausted_article_candidate
        self.assertTrue(exhausted_article_candidate(cancelled))
        from .billing import public_run_billing_receipt
        # Nothing was paid, so the client never sees a refund on the way.
        self.assertEqual(public_run_billing_receipt(cancelled.run_request, cancelled.result),
            {"billingStatus": "entitled", "refundStatus": "none"})
        # The sweep's stop then releases the attempt once.
        from .article_publication_evidence import cancellation_result
        cancelled.result = cancellation_result(cancelled, {"article_cancellation_v1": self.stop_receipt(cancelled)})
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_a_lineage_stop_never_releases_while_another_run_of_it_is_going(self):
        payload = self.started()
        parent = self.make_run(payload)
        child = self.make_run({**payload, "original_billing_source_run_id": parent.run_id}, status="failed")
        self.assertFalse(first_article.release_entitlement(organization=None, run=child, reason="revision_failed"))
        first_article.settle_owner_cancel(child)
        self.assertEqual(keys(self.org), ["attempt:1"])
        # The child is still covered by its parent's live attempt.
        self.assertEqual(first_article.lineage_entitlement(child).pk, int(payload["roo_points_ledger_id"]))
        # Once the last run of the lineage stops without a draft, the attempt is given back.
        parent.status = "failed"
        parent.save(update_fields=["status"])
        self.assertTrue(first_article.release_entitlement(organization=None, run=parent, reason="worker_lost"))
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_delivery_after_release_writes_nothing_and_still_counts_as_used(self):
        run = self.make_run(self.started())
        first_article.release_entitlement(organization=None, run=run, reason="worker_lost")
        run.status = "completed"
        run.save(update_fields=["status"])
        self.assertFalse(first_article.consume_entitlement(organization=None, run=run))
        first_article.settle_entitlement_for_run(run)
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])
        self.assertEqual(self.state()["status"], "used")
        error, _ = self.start(key="after-late-delivery")
        self.assertEqual(error.data["costPoints"], 12)

    def test_worker_stop_receipt_releases_without_a_refund_obligation(self):
        from .article_publication_evidence import cancellation_result
        run = self.make_run(self.started(), status="cancelled")
        result = cancellation_result(run, {"article_cancellation_v1": self.stop_receipt(run)})
        receipt = result["article_terminal_refund"]
        self.assertEqual((receipt["status"], receipt["entitlement_released"]), ("not_required", True))
        self.assertFalse(result["reconciliation_refund_pending"])
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_reconciliation_releases_once_after_a_bound_stop(self):
        from .reconciliation import _refund_failed_run
        run = self.make_run(self.started(), status="cancelled")
        run.result = {**run.result, "state_version": 5, "article_cancellation_v1": self.stop_receipt(run),
            "article_terminal_refund": {"status": "pending"}, "reconciliation_refund_pending": True}
        run.save(update_fields=["result"])
        for _ in range(2):
            _refund_failed_run(run, reason="run_reconciliation_failed")
        run.refresh_from_db()
        self.assertEqual(run.result["article_terminal_refund"]["status"], "not_required")
        self.assertTrue(run.result["article_terminal_refund"]["entitlement_released"])
        self.assertFalse(run.result["reconciliation_refund_pending"])
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def test_reconciliation_waits_for_the_worker_stop(self):
        from .reconciliation import _refund_failed_run
        run = self.make_run(self.started(), status="failed",
            result={"generation": 0, "state_version": 4, "article_terminal_refund": {"status": "pending"}})
        _refund_failed_run(run, reason="run_reconciliation_failed")
        run.refresh_from_db()
        self.assertEqual(run.result["article_terminal_refund"]["status"], "stop_required")
        self.assertEqual(keys(self.org), ["attempt:1"])

    def test_terminal_failure_hands_the_free_attempt_to_the_release_path(self):
        from integrations.services.article_generation import maybe_auto_refund_terminal_failure
        run = self.make_run(self.started(), status="cancelled")
        run.result = {**run.result, "state_version": 5, "article_cancellation_v1": self.stop_receipt(run)}
        run.save(update_fields=["result"])
        job = SimpleNamespace(job_id=run.run_id, domain=self.org.domain, request_meta={}, billing_status="")
        self.assertEqual(maybe_auto_refund_terminal_failure(job, error_code="INTERNAL_ERROR",
            error_message="Worker failed."), (False, 0))
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_rejected_dispatch_releases_the_attempt_and_credits_nothing(self):
        from .dispatch_outbox import refund_dispatch
        self.started()
        row = ContentFactoryDispatchOutbox.objects.get()
        with patch("integrations.services.article_generation.refund_content_factory_request_for_user") as credit:
            refund_dispatch(row, reason="dispatch_rejected")
            refund_dispatch(row, reason="dispatch_rejected")
        credit.assert_not_called()
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_queue_start_failure_releases_through_the_refund_helper(self):
        self.started()
        charged_user, ledger, article_request, _ = self.charged
        self.assertEqual((charged_user, ledger, article_request["roo_points_billing_status"]), (self.user, None, "entitled"))
        with patch("roo.services.PointsService.refund") as credit:
            self.assertIsNone(views._refund_roo_points_for_article_start(charged_user=charged_user, actor_id="",
                article_request=article_request, domain=self.org.domain, reason="queue_failed"))
        credit.assert_not_called()
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_paid_predicates_treat_the_free_article_as_unpaid_but_owed_a_release(self):
        from .billing import public_run_billing_receipt
        from .billing_lineage import paid_article_request, settlement_owed_request
        payload = self.started()
        self.assertFalse(paid_article_request(payload))
        self.assertTrue(settlement_owed_request(payload))
        self.assertFalse(settlement_owed_request({**payload, "original_billing_source_run_id": "parent"}))
        self.assertEqual(public_run_billing_receipt(payload, {}), {"billingStatus": "entitled", "refundStatus": "none"})

    def test_status_polls_keep_django_owned_alert_and_cancellation_fields(self):
        local = {"ops_alerts": [{"alert_reason": "free_cap_spike"}], "cancellation_reason": "owner_cancelled",
            "cancelled_by": "owner"}
        merged = views._merge_django_owned_article_result(local, {"status": "cancelled",
            "cancellation_reason": "worker_reason", "cancelled_by": "worker", "ops_alerts": []})
        self.assertEqual({key: merged[key] for key in local}, local)


@override_settings(**FREE)
class EntitledChildRunTests(EntitlementFixture, TestCase):
    """Revisions, restarts and comments of the free article run on its grant."""

    def reuse(self, run):
        payload = {}
        error = views._reuse_roo_points_authorization_for_article_job(run=run, payload=payload, domain=self.org.domain,
            failure_detail="Start a new article.")
        return error, payload

    def test_restart_of_a_live_attempt_runs_on_its_grant_and_its_delivery_is_the_free_article(self):
        original = self.started()
        run = self.make_run(original, status="failed", step="draft_section:intro")
        error, payload = self.reuse(run)
        self.assertIsNone(error)
        self.assertEqual((payload["roo_points_billing_status"], payload["roo_points_entitlement"],
            payload["roo_points_cost"], payload["roo_points_ledger_id"], payload["original_billing_source_run_id"]),
            ("entitled", "first_article", 0, original["roo_points_ledger_id"], run.run_id))
        child = self.make_run({**original, **payload, "client_request_id": "restart-1"}, status="completed",
            step="finalize")
        first_article.settle_entitlement_for_run(child)
        self.assertEqual(keys(self.org), ["attempt:1", "consumed"])
        self.assertEqual((self.state()["status"], self.state()["run_id"]), ("used", child.run_id))
        self.assertEqual(first_article.entitlement_payload(child)["entitlementStatus"], "consumed")
        # The failed original can no longer give the attempt back.
        self.assertFalse(first_article.release_entitlement(organization=None, run=run, reason="late_failure"))
        # A consumed attempt still covers comment revisions of the delivered article.
        self.assertIsNone(self.reuse(child)[0])

    def test_deleting_the_failed_original_after_its_restart_delivered_keeps_it_used(self):
        original = self.started()
        run = self.make_run(original, status="failed", step="research_topic")
        _error, payload = self.reuse(run)
        child = self.make_run({**original, **payload, "client_request_id": "restart-1"}, status="completed",
            step="finalize")
        first_article.settle_entitlement_for_run(child)
        views._cancel_local_article_run(run=run, organization=self.org,
            remote_data={"article_cancellation_v1": self.stop_receipt(run)})
        self.assertEqual(keys(self.org), ["attempt:1", "consumed"])
        self.assertEqual(self.state()["status"], "used")

    def test_the_failed_original_waits_for_its_running_restart(self):
        original = self.started()
        run = self.make_run(original, status="failed", step="research_topic")
        _error, payload = self.reuse(run)
        child = self.make_run({**original, **payload, "client_request_id": "restart-1"}, status="running")
        views._cancel_local_article_run(run=run, organization=self.org,
            remote_data={"article_cancellation_v1": self.stop_receipt(run)})
        self.assertEqual(keys(self.org), ["attempt:1"])
        self.assertEqual((self.state()["status"], self.state()["run_id"]), ("in_progress", child.run_id))
        # The restart then stops without a draft: as the last run it releases.
        views._cancel_local_article_run(run=child, organization=self.org,
            remote_data={"article_cancellation_v1": self.stop_receipt(child)})
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])

    def test_a_comment_on_the_free_draft_revises_without_the_balance_floor(self):
        from . import article_review_views
        original = self.started()
        run = self.make_run(original, status="completed", step="finalize")
        first_article.settle_entitlement_for_run(run)
        payload = {"commentIds": ["comment-1"]}
        context = SimpleNamespace(organization=self.org)
        with patch.object(views, "_setup_blocked_response_for_generation", return_value=None), \
                patch.object(views, "_revision_editorial_payload_from_run", return_value=({}, None)), \
                patch.object(views, "_require_roo_points_for_ai_agent") as gate:
            self.assertIsNone(article_review_views._revision_authorization(SimpleNamespace(user=self.user), context,
                run, payload))
        gate.assert_not_called()
        self.assertEqual((payload["roo_points_billing_status"], payload["roo_points_ledger_id"],
            payload["original_billing_source_run_id"]), ("entitled", original["roo_points_ledger_id"], run.run_id))
        self.assertEqual(keys(self.org), ["attempt:1", "consumed"])


    def test_restart_of_a_released_attempt_is_refused(self):
        run = self.make_run(self.started(), status="cancelled")
        first_article.release_entitlement(organization=None, run=run, reason="owner_cancelled")
        error, payload = self.reuse(run)
        self.assertEqual((error.status_code, error.data["code"]), (409, "roo_points_billing_required"))
        self.assertEqual(error.data["detail"], "This free draft was stopped. Start a new article instead.")
        self.assertEqual(payload, {})

    def test_revising_the_free_article_skips_the_balance_floor(self):
        original = self.started()
        run = self.make_run(original, status="awaiting_review", step="await_review")
        sent = {}

        def remote(**kwargs):
            sent.update(kwargs["payload"])
            return {"content_factory_status_code": 409, "error": "Synthetic stop after dispatch."}

        context = SimpleNamespace(organization=self.org, company=None, profile=None)
        with patch.object(views, "_setup_blocked_response_for_generation", return_value=None), \
                patch.object(views, "_call_content_factory_run_action", side_effect=remote), \
                patch.object(views, "_require_roo_points_for_ai_agent") as gate:
            response = views.control_article_run(user=self.user, context=context, run=run, action="revise",
                data={"feedback": "Make the intro shorter."})
        gate.assert_not_called()
        self.assertEqual(response.status_code, 409)
        self.assertEqual((sent["roo_points_billing_status"], sent["roo_points_ledger_id"],
            sent["original_billing_source_run_id"]), ("entitled", original["roo_points_ledger_id"], run.run_id))
        self.assertEqual(keys(self.org), ["attempt:1"])


@override_settings(**FREE)
class FreeFirstTopicBatchTests(EntitlementFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(views, "get_content_factory_research_cost_points", return_value=4))
        self.topic_debits = []
        self.enterContext(patch.object(views, "charge_content_factory_topic_generation_for_user",
            side_effect=self.topic_debit))

    def topic_debit(self, **kwargs):
        self.topic_debits.append(kwargs)
        raise InsufficientRooPointsError({"code": "roo_points_required", "requiredPoints": 4, "currentBalance": 0})

    def research(self, *, key, org=None, island="", first=True, expected=0, count=4):
        org = org or self.org
        payload = {"requested_topic_count": count, "client_request_id": key}
        if island:
            payload["content_island_slug"] = island
        user, request, error = views._charge_roo_points_for_content_island_topic_generation(
            SimpleNamespace(user=self.user, data={"expectedCostPoints": expected}),
            context=SimpleNamespace(organization=org, company=SimpleNamespace(pk=org.pk)), payload=payload,
            first_topic_batch=first)
        return payload, request, error

    def test_first_topic_batch_is_free_once_and_replays_keep_it(self):
        payload, _request, error = self.research(key="topics-1")
        self.assertIsNone(error)
        grant = free_rows(self.org).get()
        self.assertEqual(keys(self.org), ["topics"])
        self.assertEqual((payload["roo_points_billing_status"], payload["roo_points_action"], payload["roo_points_cost"],
            payload["roo_points_ledger_id"]), ("entitled", "topic_discovery", 0, str(grant.pk)))
        self.assertEqual(self.topic_debits, [])
        self.assertIsNone(self.research(key="topics-1")[2])
        self.assertEqual(keys(self.org), ["topics"])
        error = self.research(key="topics-2")[2]
        self.assertEqual((error.status_code, error.data["costPoints"]), (409, 4))

    def test_island_batch_stamps_the_island_action(self):
        payload, _request, error = self.research(key="island-1", island="pet-insurance")
        self.assertIsNone(error)
        self.assertEqual(payload["roo_points_action"], "content_island_topic_generation")

    def test_island_research_and_large_batches_keep_the_price(self):
        for label, kwargs in (("island_research", {"first": False}), ("eight_topics", {"count": 8})):
            with self.subTest(label):
                error = self.research(key=f"priced-{label}", **kwargs)[2]
                self.assertEqual((error.status_code, error.data["costPoints"]), (409, 4))
        self.assertFalse(free_rows().exists())

    def test_a_second_startup_on_the_same_account_pays_for_topics(self):
        self.research(key="topics-1")
        other = self.make_org("second-topics.example.test")
        self.assertFalse(first_article.topic_batch_free(other, self.user))
        error = self.research(key="topics-other", org=other)[2]
        self.assertEqual(error.data["costPoints"], 4)

    def test_a_failed_free_batch_gets_one_free_rerun(self):
        from integrations.services.article_generation import maybe_auto_refund_terminal_failure
        for island in ("pet-insurance", ""):
            org = self.make_org(f"topics-{island or 'plain'}.example.test")
            user = User.objects.create_user(email=f"owner@{org.domain}")
            self.user = user
            with self.subTest(island=island or None):
                payload, _request, error = self.research(key="topics-1", org=org, island=island)
                self.assertIsNone(error)
                run = ContentFactoryRun.objects.create(run_id=f"research-{org.pk}", workflow="auto_discovery",
                    domain=org.domain, organization=org, status="failed", run_request=payload, result={})
                # Content Factory's callback job: with and without the billing stamp in its copy.
                meta = dict(payload) if island else {}
                job = SimpleNamespace(job_id=run.run_id, domain=org.domain, request_meta=meta, billing_status="",
                    billing_amount=0, client_request_id=payload["client_request_id"])
                for _ in range(2):
                    self.assertEqual(maybe_auto_refund_terminal_failure(job, error_code="EMPTY_RESEARCH_RESULT",
                        error_message="Research failed.", refundable=True), (False, 0))
                self.assertEqual(keys(org), ["topics", "topics:released"])
                run.refresh_from_db()
                # A research run never gets an article refund receipt.
                self.assertNotIn("article_terminal_refund", run.result)
                self.assertTrue(first_article.topic_batch_free(org, user))
                self.assertIsNone(self.research(key="topics-rerun", org=org, island=island)[2])
                self.assertEqual(keys(org), ["topics", "topics:released", "topics:retry"])

    def test_a_batch_that_never_started_gets_one_free_rerun(self):
        from integrations.services.article_generation import refund_content_factory_topic_generation_for_user
        _payload, request, _error = self.research(key="topics-1")
        with patch("roo.services.PointsService.refund") as credit:
            self.assertIsNone(refund_content_factory_topic_generation_for_user(user=self.user, actor_id="",
                article_request=request, resolved_domain=self.org.domain, reason="queue_failed"))
        credit.assert_not_called()
        self.assertEqual(keys(self.org), ["topics", "topics:released"])
        self.assertIsNone(self.research(key="topics-rerun")[2])
        self.assertEqual(keys(self.org), ["topics", "topics:released", "topics:retry"])
        self.assertEqual(self.research(key="topics-third")[2].data["costPoints"], 4)

@override_settings(**FREE)
class FirstArticleSweepTests(EntitlementFixture, TestCase):
    """Every #1234 refund sweep releases a free original run once, and never credits."""

    def stale(self, run, **result):
        run.result = {**run.result, **result}
        run.save(update_fields=["result"])
        from datetime import timedelta
        from django.utils import timezone
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(hours=2))
        run.refresh_from_db()
        return run

    def assert_released_once(self, run, sweep):
        for _ in range(2):
            sweep()
        run.refresh_from_db()
        self.assertEqual(keys(self.org), ["attempt:1", "attempt:1:released"])
        receipt = run.result["article_terminal_refund"]
        self.assertEqual((receipt["status"], receipt["entitlement_released"]), ("not_required", True))
        self.assertFalse(receipt.get("paid_obligation", False))
        self.assertFalse(run.result["reconciliation_refund_pending"])
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def test_denied_sweep(self):
        from django.utils import timezone
        from . import reconciliation
        run = self.make_run(self.started(), status="denied", step="plan_article")
        run = self.stale(run, error_code="website_source_changed", state_version=5,
            article_cancellation_v1=self.stop_receipt(run))
        self.assert_released_once(run, lambda: reconciliation._queue_denied_article_refunds(limit=10,
            now=timezone.now()))

    def test_unrecoverable_sweep(self):
        from django.utils import timezone
        from . import reconciliation
        run = self.make_run(self.started(), status="failed", step="plan_article")
        run.resume_available = False
        run.save(update_fields=["resume_available"])
        run = self.stale(run, state_version=5, error_code="CORPUS_INCOMPLETE", requires_user_action=True,
            retryable=False, retry_available=False, article_cancellation_v1=self.stop_receipt(run),
            failure={"version": 1, "code": "CORPUS_INCOMPLETE", "step": "plan_article", "retryable": False,
                "requires_user_action": True, "retry_policy": "explicit_action"})
        self.assert_released_once(run, lambda: reconciliation._queue_unrecoverable_article_refunds(limit=10,
            now=timezone.now()))

    def test_dispatch_rejected_sweep(self):
        from django.utils import timezone
        from . import reconciliation
        payload = self.started()
        run = self.make_run(payload, status="failed", step="fetch_org_config")
        run = self.stale(run, error_code="dispatch_rejected", status="failed")
        record = {"run_id": run.run_id, "run_status": "failed", "error": "backend_rejected:editorial_run_conflict"}
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True}), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("rejected", record)):
            self.assert_released_once(run, lambda: reconciliation._queue_dispatch_rejected_article_refunds(limit=10,
                now=timezone.now()))

    def test_pending_settlement_sweep(self):
        from . import reconciliation
        run = self.make_run(self.started(), status="failed", step="research_topic")
        run = self.stale(run, state_version=5, article_cancellation_v1=self.stop_receipt(run),
            article_terminal_refund={"status": "pending"}, reconciliation_refund_pending=True)
        self.assert_released_once(run, lambda: reconciliation._settle_sweep_refunds(limit=10))



@override_settings(**FREE)
class FirstArticleOpsAlertTests(EntitlementFixture, TestCase):
    """The entitlement's two ops alerts reach the founder-journey sweep (IF-15)."""

    def setUp(self):
        self.queue_alert = first_article._queue_alert
        super().setUp()
        # The fixture stubs the alert; here it records through the real helper.
        self.alerts.side_effect = self.queue_alert

    def receipts(self, org=None):
        config = OrganizationContentConfig.objects.get(organization=org or self.org)
        receipts = (config.pillar_strategy or {}).get("ops_alerts", {})
        return {receipt["alert_reason"]: receipt for receipt in receipts.values()}

    def test_a_third_attempt_queues_one_pending_receipt(self):
        from .founder_journey_alerts import founder_journey_alert_id
        for number in (1, 2):
            payload = self.started(key=f"attempt-{number}")
            first_article.release_entitlement_for_request(organization=self.org, request=payload, reason="test")
        for key in ("third-request", "third-request-again"):
            error, _ = self.start(key=key)
            self.assertEqual((error.status_code, error.data["costPoints"]), (409, 12))
        receipts = self.receipts()
        self.assertEqual(list(receipts), ["first_article_attempts_exhausted"])
        receipt = receipts["first_article_attempts_exhausted"]
        self.assertEqual((receipt["state"], receipt["attempts"]), ("pending", 0))
        self.assertEqual(receipt["alert_id"], founder_journey_alert_id(organization_id=self.org.pk,
            alert_reason="first_article_attempts_exhausted", run_id=None, company_id=None, claimant_user_id=None))

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD=1)
    def test_a_spike_queues_a_pending_receipt_after_commit(self):
        from django.core.cache import cache
        cache.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.started()
        self.assertEqual(self.receipts()["free_cap_spike"]["state"], "pending")

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD=1)
    def test_a_repeat_spike_from_one_org_in_a_later_hour_is_its_own_alert(self):
        from django.core.cache import cache
        payload = self.started()
        runs = []
        for _hour in (1, 2):
            # A new hour: the hourly guard is fresh, and the same org trips it again.
            cache.clear()
            run = self.make_run({**payload, "original_billing_source_run_id": "parent"})
            with self.captureOnCommitCallbacks(execute=True):
                first_article.note_entitled_child_run(run)
            runs.append(run)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        spikes = [receipt for receipt in config.pillar_strategy["ops_alerts"].values()
                  if receipt["alert_reason"] == "free_cap_spike"]
        self.assertEqual(sorted(receipt["run_id"] for receipt in spikes), sorted(run.run_id for run in runs))
        self.assertEqual({receipt["state"] for receipt in spikes}, {"pending"})
        for run in runs:
            run.refresh_from_db()
            self.assertEqual(run.result["ops_alerts"]["free_cap_spike"]["state"], "pending")

    def test_a_receipt_write_failure_is_logged_and_never_reaches_the_founder(self):
        with patch("content_factory.founder_journey_alerts.queue_founder_journey_alert",
                side_effect=RuntimeError("database unavailable")), \
                self.assertLogs("content_factory.first_article_entitlement", "ERROR") as logs:
            self.assertIsNone(self.queue_alert(organization=self.org, alert_reason="free_cap_spike"))
        self.assertIn("founder_journey_alert_queue_failed alert_reason=free_cap_spike", logs.output[0])
