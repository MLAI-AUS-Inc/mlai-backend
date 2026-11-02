"""Persisted P2 dispatch, settlement and duplicate-write acceptance tests."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from founder_tools.views import FounderToolsProfileView
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from . import no_delivery_refunds as refunds, dispatch_outbox, service_views
from .dispatch_models import ContentFactoryDispatchOutbox
from .editorial_run_state import EditorialRunConflict
from .first_article_entitlement import reserve_entitlement, ENTITLEMENT
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org, fake_member_context, fake_provider, missing_provider


@override_settings(ARTICLES_REGISTRY_REFUNDS_ENABLED=True)
class RefundTests(TestCase):
    def setUp(self):
        self.built = make_p2_org()
        self.org, self.user = self.built.organization, self.built.owner
        PointsService.credit_purchased_topup(user=self.user, delta=100,
            description="Fixture funding", idempotency_key="fund-"+uuid4().hex)

    def paid(self, **changes):
        from integrations.services.article_generation import _ledger_reference_id
        key = uuid4().hex
        spend, _ = PointsService.spend(user=self.user, delta=12, source="CONTENT_FACTORY",
            description="Fixture article", created_by_slack_id="fixture",
            idempotency_key="content_factory:charge:"+key, reference_type="CONTENT_FACTORY",
            reference_id=_ledger_reference_id(key))
        request = {"client_request_id": key, "domain": self.org.domain,
            "roo_points_ledger_id": spend.pk, "roo_points_cost": 12,
            "roo_points_billing_user_id": self.user.pk, "roo_points_billing_status": "charged"}
        with transaction.atomic():
            dispatch_outbox.reserve_dispatch(organization=self.org, payload=request, billing_user_id=self.user.pk)
        run = ContentFactoryRun.objects.create(run_id="run-"+uuid4().hex, organization=self.org,
            domain=self.org.domain, workflow="article_generation", status="failed", run_request=request,
            result={"error_code": "RUN_BUDGET_EXHAUSTED"}, **changes)
        return run, spend

    def test_all_registry_refund_classes_and_codeless_cases_use_exact_original_debit(self):
        for cause in ("RUN_BUDGET_EXHAUSTED", "EDITORIAL_REJECTED", "REVIEW_PROVIDER_UNAVAILABLE",
                      "WEBSITE_SOURCE_CHANGED", "CONNECTION_AUTHORIZATION_FAILED", "TERMINAL_CODE_MISSING", "UNKNOWN", None):
            with self.subTest(cause=cause):
                run, spend = self.paid()
                run.result = {"error_code": cause}
                run.save(update_fields=["result"])
                before = run.updated_at
                outcome = refunds.apply_refund_decision(run, refunds.refund_decision(run))
                self.assertEqual(outcome["state"], "refunded")
                self.assertEqual(outcome["original_key"], spend.idempotency_key)
                self.assertEqual(spend.service_refunds.count(), 1)
                refunds.apply_refund_decision(run, {})
                self.assertEqual(spend.service_refunds.count(), 1)
                run.refresh_from_db()
                self.assertEqual(run.updated_at, before)

    def test_two_terminal_runs_share_one_refund_and_no_new_debit(self):
        run, spend = self.paid()
        child = ContentFactoryRun.objects.create(run_id="child-"+uuid4().hex, organization=self.org,
            domain=self.org.domain, workflow="article_generation", status="failed")
        refunds.carry_charge(source_run=run, new_run=child)
        child.result = {"error_code": "EDITORIAL_REJECTED"}
        child.save(update_fields=["result"])
        for item in (run, child):
            self.assertEqual(refunds.apply_refund_decision(item, refunds.refund_decision(item))["state"], "refunded")
        self.assertEqual(spend.service_refunds.count(), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)

    def test_failed_publish_continuation_cannot_refund_a_delivered_original_paid_draft(self):
        source, spend = self.paid()
        source.status = "completed"
        source.result = {"draft_ready_v1": {"version": 1}}
        source.save(update_fields=["status", "result"])
        child = ContentFactoryRun.objects.create(run_id="publish-child", organization=self.org,
            domain=self.org.domain, workflow="direct_generate", status="failed", result={"error_code": "RUN_BUDGET_EXHAUSTED"})
        refunds.carry_charge(source_run=source, new_run=child)
        self.assertEqual(refunds.apply_refund_decision(child, {})["state"], "captured")
        self.assertFalse(spend.service_refunds.exists())
        child.refresh_from_db()
        self.assertFalse(refunds._direct_delivery(child))
        self.assertTrue(refunds._delivery(child))

    def test_unrelated_delivered_run_cannot_claim_another_original_charge_delivery(self):
        source, spend = self.paid()
        unrelated, _ = self.paid()
        unrelated.result = {"draft_ready_v1": {"version": 1}}
        unrelated.save(update_fields=["result"])
        source.run_request["original_billing_source_run_id"] = unrelated.run_id
        source.save(update_fields=["run_request"])
        # The original direct debit remains valid, but its sender-provided delivery source is not.
        self.assertEqual(refunds.apply_refund_decision(source, {})["state"], "refunded")
        self.assertEqual(spend.service_refunds.count(), 1)

    def test_topic_research_uses_its_original_charge_and_preserves_job_billing(self):
        from integrations.services.article_generation import _ledger_reference_id
        from .models import ContentFactoryJob
        key = uuid4().hex
        spend, _ = PointsService.spend(user=self.user, delta=12, source="CONTENT_FACTORY", description="Research",
            created_by_slack_id="fixture", idempotency_key="content_factory:topic_generation:charge:"+key,
            reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
        request = {"client_request_id": key, "roo_points_action": "content_island_topic_generation",
                   "roo_points_cost": 12, "roo_points_ledger_id": spend.pk, "roo_points_billing_user_id": self.user.pk}
        job = ContentFactoryJob.objects.create(job_id="topic-research", client_request_id=key, domain=self.org.domain,
            request_meta=request, billing_ledger=spend, billing_status="charged", billing_amount=12)
        run = ContentFactoryRun.objects.create(run_id="topic-research", organization=self.org, domain=self.org.domain,
            workflow="auto_discovery", status="failed", run_request=request)
        self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "refunded")
        job.refresh_from_db()
        self.assertEqual(job.billing_status, "refunded")
        self.assertEqual(spend.service_refunds.count(), 1)

    def test_legacy_refund_is_adopted_and_never_duplicated(self):
        run, spend = self.paid()
        original, _ = PointsService.refund(user=self.user, delta=12, source="CONTENT_FACTORY",
            description="Legacy refund", created_by_slack_id="fixture", idempotency_key="legacy-refund-"+uuid4().hex,
            original_spend_key=spend.idempotency_key)
        result = refunds.apply_refund_decision(run, {})
        self.assertEqual(result["refund_ledger_id"], original.pk)
        self.assertEqual(spend.service_refunds.count(), 1)

    def test_no_original_debit_never_creates_credit(self):
        run = ContentFactoryRun.objects.create(run_id="no-debit", organization=self.org, domain=self.org.domain,
            status="failed", workflow="article_generation", run_request={"roo_points_cost": 12})
        self.assertEqual(refunds.refund_decision(run)["action"], "none")
        self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "none")
        self.assertFalse(Ledger.objects.filter(kind="REFUND").exists())

    def test_delivered_draft_is_captured_even_after_authorization_failure(self):
        run, spend = self.paid()
        run.result = {"draft_ready_v1": {"version": 1}, "error_code": "CONNECTION_AUTHORIZATION_FAILED"}
        run.save(update_fields=["result"])
        self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "captured")
        self.assertEqual(refunds.points_outcome(run), {"state": "charged", "points": 12})
        self.assertFalse(spend.service_refunds.exists())

    def test_capture_changes_to_kept_after_owner_cancels_delivered_draft(self):
        run, spend = self.paid()
        run.result = {"draft_ready_v1": {"version": 1}}
        run.save(update_fields=["result"])
        self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "captured")
        run.status = "cancelled"
        run.result["cancellation_reason"] = "owner_cancelled"
        run.save(update_fields=["status", "result"])
        self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "kept")
        self.assertFalse(spend.service_refunds.exists())

    def test_late_owner_cancel_keeps_charge_and_cancel_preview_agrees(self):
        run, spend = self.paid()
        ContentFactoryRunStep.objects.create(run=run, step_key="write_article", status="running", started_at=timezone.now())
        self.assertEqual(refunds.cancel_outcome(run), {"state": "kept", "points": 12, "freeArticle": False})
        run.status, run.result = "cancelled", {"cancellation_reason": "owner_cancelled"}
        run.save(update_fields=["status", "result"])
        self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "kept")
        self.assertFalse(spend.service_refunds.exists())

    def test_retry_intent_and_unknown_publication_keep_reservation(self):
        run, spend = self.paid()
        for result in ({"self_heal_v1": {"mode": "policy", "next_at": timezone.now().isoformat()},
                        "error_code": "REVIEW_PROVIDER_UNAVAILABLE"},
                       {"publication_evidence_v1": {"version": 0}}):
            run.result = result
            run.save(update_fields=["result"])
            self.assertEqual(refunds.apply_refund_decision(run, {}), {})
            self.assertFalse(spend.service_refunds.exists())

    def test_forged_decision_cannot_credit_another_amount_or_key(self):
        run, spend = self.paid()
        outcome = refunds.apply_refund_decision(run, {"action": "refund", "points": 999, "original_key": "foreign"})
        self.assertEqual((outcome["points"], outcome["original_key"]), (12, spend.idempotency_key))

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False,
                       ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=0)
    def test_free_entitlement_releases_once_from_original_attempt(self):
        key = "free-"+uuid4().hex
        reservation = reserve_entitlement(organization=self.org, user=self.user, client_request_id=key,
            data={"expectedCostPoints": 0, "delivery_mode": "content_only", "delivery_mode_confirmed": True},
            config=self.built.config)
        self.assertIsNotNone(reservation)
        run = ContentFactoryRun.objects.create(run_id="free-run", organization=self.org, domain=self.org.domain,
            workflow="article_generation", status="failed", run_request={"client_request_id": key,
                "roo_points_billing_status": "entitled", "roo_points_entitlement": ENTITLEMENT,
                "roo_points_ledger_id": str(reservation.pk)}, result={"error_code": "RUN_BUDGET_EXHAUSTED"})
        for _ in range(2):
            self.assertEqual(refunds.apply_refund_decision(run, {})["state"], "released")
        self.assertEqual(Ledger.objects.filter(idempotency_key__endswith=":released").count(), 1)
        self.assertFalse(Ledger.objects.filter(kind="REFUND").exists())

    @override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False,
                       ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=0)
    def test_real_reserved_or_consumed_entitlement_can_be_carried(self):
        key = "carry-free-"+uuid4().hex
        reservation = reserve_entitlement(organization=self.org, user=self.user, client_request_id=key,
            data={"expectedCostPoints": 0, "delivery_mode": "content_only", "delivery_mode_confirmed": True},
            config=self.built.config)
        request = {"client_request_id": key, "roo_points_billing_status": "entitled",
                   "roo_points_entitlement": ENTITLEMENT, "roo_points_ledger_id": str(reservation.pk)}
        source = ContentFactoryRun.objects.create(run_id="real-free", organization=self.org, domain=self.org.domain,
            workflow="article_generation", run_request=request)
        child = ContentFactoryRun.objects.create(run_id="real-child", organization=self.org, domain=self.org.domain,
            workflow="article_generation")
        self.assertEqual(refunds.carry_charge(source_run=source, new_run=child)["state"], "carried")
        from .first_article_entitlement import consume_entitlement
        consume_entitlement(organization=self.org, run=source)
        self.assertEqual(refunds.carry_charge(source_run=source, new_run=child)["state"], "carried")

    def test_declared_entitlement_without_original_reservation_cannot_be_carried(self):
        source = ContentFactoryRun.objects.create(run_id="forged-free", organization=self.org,
            domain=self.org.domain, workflow="article_generation", run_request={"roo_points_billing_status": "entitled",
            "roo_points_entitlement": ENTITLEMENT, "roo_points_ledger_id": "999999999"})
        child = ContentFactoryRun.objects.create(run_id="forged-child", organization=self.org, domain=self.org.domain,
            workflow="article_generation")
        with self.assertRaises(ValueError):
            refunds.carry_charge(source_run=source, new_run=child)
        child.refresh_from_db()
        self.assertEqual(child.run_request, {})
        self.assertNotIn("points_v1", child.result)

    def test_sweep_is_bounded_and_batches_no_debit_receipts(self):
        import time
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        ContentFactoryRun.objects.bulk_create([ContentFactoryRun(run_id=f"empty-{i}", organization=self.org,
            domain=self.org.domain, workflow="article_generation", status="failed") for i in range(500)])
        start = time.monotonic()
        with CaptureQueriesContext(connection) as queries:
            result = refunds.run_refund_sweep(limit=500)
        self.assertEqual(result["processed"], 500)
        self.assertLessEqual(len(queries), 8)
        self.assertLess(time.monotonic()-start, 2)
        self.assertEqual(refunds.run_refund_sweep(limit=500)["processed"], 0)

    @override_settings(ARTICLES_REGISTRY_REFUNDS_ENABLED=False)
    def test_flag_off_has_no_effect(self):
        run, spend = self.paid()
        self.assertEqual(refunds.refund_decision(run)["why"], "flag_off")
        self.assertEqual(refunds.apply_refund_decision(run, {}), {})
        self.assertIsNone(refunds.points_outcome(run))
        self.assertFalse(spend.service_refunds.exists())


class DispatchAndSnapshotTests(TestCase):
    def setUp(self):
        self.built = make_p2_org()

    def test_new_stamp_does_not_conflict_with_pre_release_reservation_digest(self):
        payload = {"domain": self.built.organization.domain, "client_request_id": "same", "topic": "Example"}
        with transaction.atomic():
            first = dispatch_outbox.reserve_dispatch(organization=self.built.organization, payload=payload)
        old = deepcopy(first.payload)
        old["request"].pop("self_heal_policy_v1")
        first.payload = old
        first.save(update_fields=["payload"])
        with transaction.atomic():
            second = dispatch_outbox.reserve_dispatch(organization=self.built.organization, payload=dict(payload))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)

    def test_snapshots_cannot_create_or_replace_backend_owned_keys(self):
        from .p2_foundation import P2_BACKEND_REQUEST_KEYS, P2_BACKEND_RESULT_KEYS
        for keys, merge in ((P2_BACKEND_RESULT_KEYS, service_views._merge_django_owned_run_result),
                            (P2_BACKEND_REQUEST_KEYS, service_views._merge_django_owned_run_request)):
            saved = {key: {"owner": "backend"} for key in keys}
            for incoming in ({}, {key: {"owner": "forged"} for key in keys}):
                self.assertEqual(merge(saved, incoming), saved)
            self.assertEqual(merge({}, saved), {})

    @override_settings(ARTICLES_NO_CODELESS_TERMINAL_ENABLED=True)
    def test_terminal_guard_and_owner_cancel_exception(self):
        for state in ("failed", "blocked", "cancelled", "denied", "stopped"):
            data = {"run_id": "guard", "status": state, "result": {}}
            service_views._guard_p2_terminal_snapshot(data)
            self.assertEqual(data["result"]["error_code"], "TERMINAL_CODE_MISSING")
            self.assertEqual(data["result"]["failure"]["code"], "TERMINAL_CODE_MISSING")
        data = {"status": "cancelled", "result": {"cancellation_reason": "owner_cancelled"}}
        service_views._guard_p2_terminal_snapshot(data)
        self.assertNotIn("error_code", data["result"])

    @override_settings(ARTICLES_DISPATCH_RELIABILITY_ENABLED=True)
    def test_conflict_attach_needs_same_org_and_owner_or_member(self):
        org = self.built.organization
        member = User.objects.create_user(email="member@example.test")
        run = ContentFactoryRun.objects.create(run_id="active", organization=org, domain=org.domain,
            status="running", workflow="article_generation", run_request={"roo_points_requested_by_user_id": self.built.owner.pk})
        with missing_provider("founder_tools.startup_team"):
            self.assertEqual(service_views._attach_p2_editorial_conflict(run, {"domain": org.domain}, ack=True).status_code, 200)
            self.assertIsNone(service_views._attach_p2_editorial_conflict(run, {"domain": "foreign.test"}, ack=True))
            run.run_request["roo_points_requested_by_user_id"] = member.pk
            self.assertIsNone(service_views._attach_p2_editorial_conflict(run, {"domain": org.domain}, ack=True))
        with fake_member_context(members={member.pk}, owner_ids={self.built.owner.pk}):
            self.assertEqual(service_views._attach_p2_editorial_conflict(run, {"domain": org.domain}, ack=True).status_code, 200)

    @override_settings(ARTICLES_NO_CODELESS_TERMINAL_ENABLED=True)
    def test_real_ingest_guards_codeless_terminal_and_dedupes_org_alert(self):
        from .models import OrganizationContentConfig
        org = self.built.organization
        run = ContentFactoryRun.objects.create(run_id="ingest-guard", organization=org, domain=org.domain,
            workflow="article_generation", status="running", run_request={"client_request_id": "ingest-guard"})
        data = {"workflow": run.workflow, "domain": org.domain, "status": "failed", "run_request": run.run_request,
                "result": {"cancellation_reason": "owner_cancelled", "points_v1": {"state": "refunded"}}}
        for _ in range(2):
            run, _ = service_views._sync_content_factory_run_snapshot(run_id=run.run_id, data=deepcopy(data), step_states={})
        self.assertEqual(run.result["error_code"], "TERMINAL_CODE_MISSING")
        self.assertNotIn("points_v1", run.result)
        self.assertNotIn("cancellation_reason", run.result)
        config = OrganizationContentConfig.objects.get(organization=org)
        rows = list(config.pillar_strategy["ops_alerts"].values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["alert_reason"], "codeless_terminal")

    def test_repin_snapshots_keep_only_backend_approved_source_chain(self):
        history = {"version": 1, "repins": 1, "first_from_sha": "a"*40, "last_to_sha": "b"*40,
                   "history": [{"from_sha": "a"*40, "to_sha": "b"*40, "repin_key": "run:1",
                                "reason": "resume_latest", "at": timezone.now().isoformat()}]}
        def validated(value):
            return value if value == history else None
        with fake_provider("content_factory.source_repin", validated_history=validated):
            for incoming in ("a"*40, "b"*40):
                result = service_views._merge_django_owned_run_request({"expected_source_sha": "b"*40},
                    {"expected_source_sha": incoming}, backend_result={"re_pin_v1": history})
                self.assertEqual(result["expected_source_sha"], "b"*40)
            with self.assertRaises(EditorialRunConflict):
                service_views._merge_django_owned_run_request({"expected_source_sha": "b"*40},
                    {"expected_source_sha": "c"*40}, backend_result={"re_pin_v1": history})
        with missing_provider("content_factory.source_repin"), self.assertRaises(EditorialRunConflict):
            service_views._merge_django_owned_run_request({"expected_source_sha": "a"*40},
                {"expected_source_sha": "b"*40}, backend_result={"re_pin_v1": history})

    @override_settings(ARTICLES_DISPATCH_RELIABILITY_ENABLED=True)
    def test_lost_post_adopts_started_run_with_same_key_within_two_minutes(self):
        from . import vibe_marketing_views as views
        org, user = self.built.organization, self.built.owner
        payload = {"client_request_id": "lost-post", "domain": org.domain, "github_repo": "",
                   "roo_points_requested_by_user_id": user.pk, "delivery_mode": "content_only",
                   "delivery_mode_confirmed": True}
        with transaction.atomic():
            row = dispatch_outbox.reserve_dispatch(organization=org, payload=payload)
        now = row.next_attempt_at
        def create(**kwargs):
            return ContentFactoryRun.objects.create(run_id="cf-already-started", domain=org.domain,
                organization=org, workflow="article_generation", status="running", run_request=payload)
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.invalid"}), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", side_effect=[("unknown", {}),
                    ("started", {"run_id": "cf-already-started"})]), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views, "_refresh_article_editorial_payload", return_value=None), \
                patch("integrations.services.article_generation.require_article_activation"), \
                patch("integrations.http_client.post", side_effect=TimeoutError("lost response")) as post, \
                patch.object(views, "_create_local_run", side_effect=create), \
                patch("content_factory.dispatch_binding.bind_dispatch_token_run"):
            dispatch_outbox.process_dispatch_outbox(now=now)
            row.refresh_from_db()
            self.assertLessEqual(row.next_attempt_at-now, timedelta(seconds=120))
            dispatch_outbox.process_dispatch_outbox(now=row.next_attempt_at)
        row.refresh_from_db()
        self.assertEqual(row.state, "delivered")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(ContentFactoryRun.objects.filter(run_id="cf-already-started").count(), 1)

    def test_pr_count_seam_missing_and_fake_provider(self):
        run = ContentFactoryRun.objects.create(run_id="pr", organization=self.built.organization,
            domain=self.built.organization.domain, workflow="article_generation", result={"pr_url": "https://github.com/fixture/site/pull/1"})
        with missing_provider("content_factory.failure_policy_engine"):
            service_views._record_p2_pull_request(run, None)
        calls = []
        with fake_provider("content_factory.failure_policy_engine", record_pull_request=lambda org, **kw: calls.append(kw)):
            service_views._record_p2_pull_request(run, None)
            service_views._record_p2_pull_request(run, run.result["pr_url"])
        self.assertEqual(len(calls), 1)


class ProfileIdempotencyTests(TestCase):
    @override_settings(MY_STARTUP_IDEMPOTENT_PROFILE_SAVE_ENABLED=True)
    def test_duplicate_profile_saves_apply_once_and_conflicting_body_is_refused(self):
        built = make_p2_org()
        factory = APIRequestFactory()
        view = FounderToolsProfileView.as_view()
        def save(body):
            request = factory.post("/profile/", body, format="json", HTTP_IDEMPOTENCY_KEY="profile-"+str(built.owner.pk))
            force_authenticate(request, built.owner)
            return view(request)
        first = save({})
        second = save({})
        third = save({"organization_name": "Changed"})
        self.assertEqual((first.status_code, second.status_code, third.status_code), (200, 200, 409))
        self.assertEqual(second["Idempotent-Replayed"], "true")


class ConcurrencyTests(TransactionTestCase):
    """PostgreSQL proves row locking and actual duplicate request exclusion."""
    @override_settings(**ALL_FLAGS_ON)
    def test_actual_repin_accepts_before_and_after_snapshots_and_refuses_foreign_sha(self):
        from .tests.test_p2_authority_repin import SourceRepinTests
        fixture = SourceRepinTests("test_success_and_replay_leave_timestamp_content_and_ledger_unchanged")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertEqual(fixture.apply()["status"], "repinned")
        fixture.run.refresh_from_db()
        for sha in ("a"*40, "b"*40):
            run, _ = service_views._sync_content_factory_run_snapshot(run_id=fixture.run.run_id,
                data={"workflow": fixture.run.workflow, "domain": fixture.run.domain,
                      "github_repo": fixture.run.github_repo, "status": "running",
                      "run_request": {**fixture.request, "expected_source_sha": sha}, "result": {}}, step_states={})
            self.assertEqual(run.run_request["expected_source_sha"], "b"*40)
            self.assertEqual(run.result["re_pin_v1"]["last_to_sha"], "b"*40)
        with self.assertRaises(EditorialRunConflict):
            service_views._sync_content_factory_run_snapshot(run_id=fixture.run.run_id,
                data={"workflow": fixture.run.workflow, "domain": fixture.run.domain,
                      "github_repo": fixture.run.github_repo, "status": "running",
                      "run_request": {**fixture.request, "expected_source_sha": "c"*40}, "result": {}}, step_states={})

    @override_settings(ARTICLES_REGISTRY_REFUNDS_ENABLED=True)
    def test_two_terminal_threads_refund_one_original_charge(self):
        from django.db import connection, close_old_connections
        if connection.vendor != "postgresql":
            self.skipTest("Requires disposable PostgreSQL")
        from threading import Barrier, Thread
        fixture = RefundTests(methodName="test_no_original_debit_never_creates_credit")
        fixture.setUp()
        run, spend = fixture.paid()
        child = ContentFactoryRun.objects.create(run_id="thread-child", organization=fixture.org,
            domain=fixture.org.domain, workflow="article_generation", status="failed")
        refunds.carry_charge(source_run=run, new_run=child)
        gate, failures, states = Barrier(2), [], []
        def settle(pk):
            close_old_connections()
            try:
                current = ContentFactoryRun.objects.get(pk=pk)
                gate.wait(timeout=10)
                states.append(refunds.apply_refund_decision(current, {})["state"])
            except Exception as exc:
                failures.append(repr(exc))
            finally:
                close_old_connections()
        threads = [Thread(target=settle, args=(item.pk,)) for item in (run, child)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(states, ["refunded", "refunded"])
        self.assertEqual(spend.service_refunds.count(), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)

    @override_settings(MY_STARTUP_IDEMPOTENT_PROFILE_SAVE_ENABLED=True)
    def test_concurrent_profile_save_has_one_writer(self):
        from django.db import connection, close_old_connections
        if connection.vendor != "postgresql":
            self.skipTest("Requires disposable PostgreSQL")
        from threading import Event, Thread
        built = make_p2_org()
        entered, release, responses, failures = Event(), Event(), [], []
        original = FounderToolsProfileView._save_profile
        def slow_save(view, request):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("Timed out waiting for duplicate request")
            return original(view, request)
        def save():
            close_old_connections()
            try:
                user = User.objects.get(pk=built.owner.pk)
                request = APIRequestFactory().post("/profile/", {}, format="json", HTTP_IDEMPOTENCY_KEY="thread-profile")
                force_authenticate(request, user)
                response = FounderToolsProfileView.as_view()(request)
                responses.append(response.status_code)
            except Exception as exc:
                failures.append(repr(exc))
            finally:
                close_old_connections()
        with patch.object(FounderToolsProfileView, "_save_profile", slow_save):
            first = Thread(target=save)
            first.start()
            self.assertTrue(entered.wait(timeout=10))
            duplicate = Thread(target=save)
            duplicate.start()
            duplicate.join(timeout=10)
            self.assertFalse(duplicate.is_alive())
            release.set()
            first.join(timeout=10)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(responses), [200, 409])
