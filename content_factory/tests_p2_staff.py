"""Persisted staff access, recovery idempotency, incident dedupe and load gates."""
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from community_chat.admin_access import admin_sections
from community_chat.models import CommunityChatDevice
from community_chat.staff_console_views import StartupListView, StartupDetailView, StartupActionView, AlertListView, AlertAckView
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig
from . import staff_console as console, staff_alert_rules as rules
from .founder_journey_alerts import queue_founder_journey_alert
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org, make_stamped_run, fake_provider, missing_provider, fake_member_context


@override_settings(**ALL_FLAGS_ON)
class StaffTests(TestCase):
    def setUp(self):
        cache.clear()
        self.built = make_p2_org()
        self.org = self.built.organization
        self.staff = User.objects.create_superuser(email=uuid4().hex+"@staff.example.test")
        self.device = CommunityChatDevice.objects.create(user=self.staff, public_key="a"*64, status="verified")
        self.factory = APIRequestFactory()

    def call(self, view, *, user=None, verified=True, data=None, **kwargs):
        request = self.factory.post("/", data, format="json") if data is not None else self.factory.get("/")
        if user is not False:
            force_authenticate(request, user=user or self.staff)
        if verified:
            request.community_chat_public_key = self.device.public_key
            request.community_chat_installation_id = self.device.installation_id
        return view.as_view()(request, **kwargs)

    def new_run(self, **changes):
        run = make_stamped_run(self.org, **changes)
        ContentFactoryRun.objects.filter(pk=run.pk).update(domain=self.org.domain)
        run.domain = self.org.domain
        return run

    def action(self, run, action, key=None, **extra):
        return console.perform_action(self.org.pk, {"action": action, "runId": run.run_id,
            "idempotencyKey": key or uuid4().hex, **extra}, by_user=self.staff)

    def test_all_routes_refuse_founder_member_signedout_staff_without_verified_admin(self):
        views = [(StartupListView, {}), (StartupDetailView, {"organization_id": self.org.pk}),
                 (StartupActionView, {"organization_id": self.org.pk}), (AlertListView, {}), (AlertAckView, {"alert_id": "unknown"})]
        for view, args in views:
            for user, verified in ((False, False), (self.built.owner, True), (self.staff, False)):
                with self.subTest(view=view, user=user):
                    response = self.call(view, user=user, verified=verified, data={} if view in (StartupActionView, AlertAckView) else None, **args)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.data["reason"], "not_authorised")
        self.staff.is_staff = False
        self.staff.save(update_fields=["is_staff"])
        self.assertEqual(self.call(StartupListView).status_code, 403)
        with fake_member_context(members=[self.built.owner.pk]):
            self.assertEqual(self.call(StartupListView, user=self.built.owner).status_code, 403)

    def test_section_and_flag_gate(self):
        request = self.factory.get("/")
        request.user = self.staff
        self.assertIn("startups", admin_sections(request))
        request.user = self.built.owner
        self.assertNotIn("startups", admin_sections(request))
        with override_settings(MY_STARTUP_STAFF_CONSOLE_ENABLED=False):
            request.user = self.staff
            self.assertNotIn("startups", admin_sections(request))
            self.assertEqual(self.call(StartupListView).status_code, 403)

    def test_verified_staff_routes_and_detail_never_return_config_secrets(self):
        run = self.new_run(result={"error_code": "NOT_A_CODE", "self_heal_v1": {"attempts": 2}})
        self.built.config.github_token_encrypted = "fixture-secret"
        self.built.config.save(update_fields=["github_token_encrypted"])
        self.assertEqual(self.call(StartupListView).status_code, 200)
        response = self.call(StartupDetailView, organization_id=self.org.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["runId"], run.run_id)
        self.assertEqual(response.data["copyId"], "working")
        self.assertEqual(response.data["owner"]["email"], self.built.owner.email)
        self.assertNotIn("fixture-secret", str(response.data))

    def test_detail_doctor_uses_saved_scoped_sha_and_cost_receipts(self):
        from .website_models import WebsiteConnection, WebsiteConnectionOperation
        from .website_goal import scope_for, new_goal
        site = WebsiteConnection.objects.create(organization=self.org, github_repo="synthetic/site", branch="main",
            verified_sha="a"*40, repository_id=123)
        self.built.config.website_connection = site
        self.built.config.save(update_fields=["website_connection"])
        scope = scope_for(site, self.built.company.pk)
        goal = new_goal("synthetic-goal", scope, now=timezone.now())
        goal["budget_used"] = {"model_cost": .25, "model_calls": 2}
        WebsiteConnectionOperation.objects.create(connection=site, generation=site.generation,
            action="prepare", idempotency_key="staff-doctor", payload={"company_id": str(self.built.company.pk)},
            receipt={"goal": goal})
        doctor = console.startup_detail(self.org.pk)["website"]["doctor"]
        self.assertEqual(doctor["verifiedSourceSha"], "a"*40)
        self.assertEqual(doctor["budget"]["modelUsd"], .25)
        self.assertEqual(doctor["companyId"], str(self.built.company.pk))
        self.assertEqual(doctor["connectionId"], str(site.pk))

    def test_every_startup_paginated_with_at_most_six_queries(self):
        Organization.objects.bulk_create([Organization(name=f"Startup {i}", domain=f"staff-{i}.example.test") for i in range(103)])
        with CaptureQueriesContext(connection) as queries:
            page = console.startup_list({})
        self.assertLessEqual(len(queries), 6)
        self.assertEqual(len(page["results"]), 50)
        second = console.startup_list({"cursor": page["next"]})
        third = console.startup_list({"cursor": second["next"]})
        ids = [r["organizationId"] for p in (page, second, third) for r in p["results"]]
        self.assertEqual(len(ids), 104)
        self.assertEqual(len(set(ids)), 104)
        self.assertIsNone(third["next"])

    def test_overdue_list_filter_ack_and_ids_only_details(self):
        run = self.new_run(result={"error_code": "NOT_A_CODE", "last_transitions": [{"at": timezone.now().isoformat(), "from": "working", "to": "your_turn"}]})
        alert_id = queue_founder_journey_alert(organization=self.org, alert_reason="unregistered_failure_code", run=run)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        receipts = config.pillar_strategy["ops_alerts"]
        details = receipts[alert_id]["details"]
        self.assertEqual(details["run_id"], run.run_id)
        self.assertNotIn("@", str(details))
        self.assertNotIn("https://", str(details))
        details["ack_due_at"] = (timezone.now()-timedelta(hours=1)).isoformat()
        config.save(update_fields=["pillar_strategy"])
        page = console.startup_list({"alerts": "overdue"})
        self.assertEqual(page["overdueAlerts"], 1)
        self.assertEqual(page["results"][0]["organizationId"], self.org.pk)
        self.assertEqual(self.call(AlertListView).data["results"][0]["startupName"], self.org.name)
        first = self.call(AlertAckView, data={}, alert_id=alert_id)
        second = self.call(AlertAckView, data={}, alert_id=alert_id)
        self.assertEqual(first.data, second.data)
        self.assertEqual(console.startup_list({"alerts": "overdue"})["results"], [])

    def test_unknown_code_rule_dedupes(self):
        self.new_run(result={"error_code": "NOT_A_CODE"})
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)

    def test_codeless_rule_and_ingest_share_one_identity(self):
        run = self.new_run(result={"error_code": "TERMINAL_CODE_MISSING"})
        queue_founder_journey_alert(organization=self.org, alert_reason="codeless_terminal", run=run)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        self.assertEqual(len(config.pillar_strategy["ops_alerts"]), 1)

    def test_codeless_raw_terminal_fires_once(self):
        self.new_run(result={})
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)

    def test_our_team_causes_each_fire_and_dedupe(self):
        cases = [({"strict_pass_v1": {"claims": 2}, "draft_ready_v1": {"version": 1}}, "claim_findings_delivered"),
                 ({"error_code": "RUN_BUDGET_EXHAUSTED"}, "free_budget_exhausted"),
                 ({"error_code": "TERMINAL_CODE_MISSING", "preview_repair_v1": {"outcome": "exhausted", "attempts": 6}}, "repair_loop_exhausted"),
                 ({"ladder": {"exhausted_at": timezone.now().isoformat()}}, None)]
        for result, cause in cases:
            run = self.new_run(result=result)
            rules.run_alert_rule_sweep()
            config = OrganizationContentConfig.objects.get(organization=self.org)
            found = {a["alert_reason"] for a in config.pillar_strategy["ops_alerts"].values() if a.get("run_id") == run.run_id}
            self.assertIn("our_team_item", found)
            if cause:
                self.assertIn(cause, found)
            self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)

    def test_setup_attempts_spike_fires_once_per_day(self):
        for _ in range(7):
            self.new_run(workflow="article_system_setup", status="completed")
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)

    def test_queue_age_rule_fires_once(self):
        run = self.new_run(status="queued")
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)

    def test_provider_rate_rule_global_fires_once_and_ack(self):
        self.new_run(result={"error_code": "REVIEW_PROVIDER_UNAVAILABLE"})
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)
        receipt = rules.global_alerts()[0]
        self.assertEqual(receipt["alert_reason"], "provider_error_rate")
        self.assertIsNone(receipt["run_id"])
        console.acknowledge_alert(receipt["alert_id"], by_user=self.staff)
        self.assertFalse(rules.open_alert(rules.global_alerts()[0]))

    def test_throttle_spike_fires_once(self):
        from .staff_signals import count
        count("founder_throttled", amount=6)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)
        self.assertEqual(rules.global_alerts()[0]["alert_reason"], "throttle_spike")

    def test_refresh_spike_above_seven_day_baseline_fires_once(self):
        from .staff_signals import count
        now = timezone.now()
        count("refresh_failed", now=now-timedelta(days=2), amount=167)
        count("refresh_failed", now=now, amount=4)
        self.assertEqual(rules.run_alert_rule_sweep(now=now)["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep(now=now)["queued"], 0)
        self.assertEqual(rules.global_alerts()[0]["alert_reason"], "refresh_failure_spike")

    def test_github_callback_spike_fires_once(self):
        from .staff_signals import count
        count("github_callback_failed", amount=6)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 1)
        self.assertEqual(rules.run_alert_rule_sweep()["queued"], 0)
        self.assertEqual(rules.global_alerts()[0]["alert_reason"], "github_callback_failures")

    def test_overdue_log_once_per_melbourne_day(self):
        with self.assertLogs(rules.logger, level="WARNING") as logs:
            rules.run_alert_rule_sweep()
        self.assertEqual(sum("staff_alert_overdue" in x for x in logs.output), 1)
        with patch.object(rules.logger, "warning") as log:
            rules.run_alert_rule_sweep()
        log.assert_not_called()

    def test_rules_off_do_no_queries_or_effects(self):
        with override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=False), self.assertNumQueries(0):
            self.assertEqual(rules.run_alert_rule_sweep(), {"status": "disabled"})

    def test_sweep_five_hundred_idle_orgs_under_two_seconds_and_batched_queries(self):
        orgs = Organization.objects.bulk_create([Organization(name="Load", domain=f"load-{i}.test") for i in range(499)])
        OrganizationContentConfig.objects.bulk_create([OrganizationContentConfig(organization=o) for o in orgs])
        start = monotonic()
        with CaptureQueriesContext(connection) as queries:
            result = rules.run_alert_rule_sweep(limit=500)
        self.assertEqual(result["organizations"], 500)
        self.assertLess(monotonic()-start, 2)
        self.assertLessEqual(len(queries), 4)

    def test_missing_provider_refuses_each_action_and_audits_without_touching_stall_clock(self):
        run = self.new_run()
        before = run.updated_at
        for action, module in (("retry_now", "failure_policy_engine"), ("switch_to_draft", "channel_switch"),
                               ("release_or_refund", "no_delivery_refunds"), ("resolve_and_notify", "founder_transitions")):
            with missing_provider("content_factory."+module):
                status, body, _ = self.action(run, action)
                self.assertEqual((status, body["status"], body["detail"]), (200, "refused", "Not available yet."))
        run.refresh_from_db()
        self.assertEqual(run.updated_at, before)
        self.assertEqual(len(run.result["staff_actions_v1"]), 4)

    def test_retry_now_changes_only_backoff_time_not_caps_and_replays(self):
        @dataclass(frozen=True)
        class Decision:
            action: str = "retry"
            at: object = None
            cap: object = None
        apply = Mock(return_value={"action": "retry", "attempts": 1})
        with fake_provider("content_factory.failure_policy_engine", decide=lambda *a, **kw: Decision(at=timezone.now()+timedelta(hours=1)), apply=apply):
            run = self.new_run()
            one = self.action(run, "retry_now", key="same")
            two = self.action(run, "retry_now", key="same")
        self.assertEqual(one[:2], two[:2])
        self.assertTrue(two[2])
        apply.assert_called_once()
        self.assertLess(apply.call_args.args[1].at-timezone.now(), timedelta(seconds=1))
        self.assertIsNone(apply.call_args.args[1].cap)
        status, body, _ = console.perform_action(self.org.pk, {"action": "retry_now", "runId": run.run_id, "idempotencyKey": "same", "note": "different"}, by_user=self.staff)
        self.assertEqual(status, 409)

    def test_actions_scope_tenant_bounds_notes_history_and_http_replay(self):
        run = self.new_run(result={"staff_actions_v1": [{"action": "old"}]*25})
        with fake_provider("content_factory.failure_policy_engine", decide=lambda *a, **k: {}, apply=lambda *a, **k: {}):
            payload = {"action": "retry_now", "runId": run.run_id, "idempotencyKey": "repeat", "note": "x"*600}
            self.assertEqual(self.call(StartupActionView, data=payload, organization_id=self.org.pk).status_code, 200)
            replay = self.call(StartupActionView, data=payload, organization_id=self.org.pk)
            self.assertEqual(replay["Idempotent-Replayed"], "true")
        run.refresh_from_db()
        self.assertEqual(len(run.result["staff_actions_v1"]), 20)
        self.assertEqual(len(run.result["staff_actions_v1"][-1]["note"]), 500)
        other = make_p2_org().organization
        self.assertEqual(self.call(StartupActionView, data=payload, organization_id=other.pk).status_code, 404)

    def test_switch_delivered_calls_channel_provider_once(self):
        run = self.new_run(result={"draft_ready_v1": {"version": 1}})
        switch = Mock(return_value={"channel": "copy_ready"})
        with fake_provider("content_factory.channel_switch", switch_to_copy_ready=switch):
            self.assertEqual(self.action(run, "switch_to_draft", key="switch")[1]["status"], "done")
            self.action(run, "switch_to_draft", key="switch")
        switch.assert_called_once()

    def test_switch_unwritten_calls_portable_start_and_carry_provider(self):
        run = self.new_run()
        carry = Mock(return_value={"state": "carried"})
        with fake_provider("content_factory.channel_switch", switch_to_copy_ready=Mock()), fake_provider("content_factory.no_delivery_refunds", carry_charge=carry), patch.object(console, "_start_copy_ready", return_value={"status": "done", "detail": "Queued", "receipt": {}}) as start:
            self.action(run, "switch_to_draft", key="portable")
        self.assertEqual(start.call_args.kwargs["key"], f"staff-switch:{self.org.pk}:1")
        self.assertIs(start.call_args.kwargs["carry"], carry)

    def test_resolve_acknowledges_and_resolves_then_notifies_once(self):
        run = self.new_run()
        key = queue_founder_journey_alert(organization=self.org, alert_reason="our_team_item", run=run)
        notify = Mock(return_value=1)
        with fake_provider("content_factory.founder_transitions", notify_team_resolved=notify):
            self.action(run, "resolve_and_notify", key="resolve")
            self.action(run, "resolve_and_notify", key="resolve")
        notify.assert_called_once()
        receipt = OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy["ops_alerts"][key]
        self.assertTrue(receipt["acknowledged_at"])
        self.assertTrue(receipt["resolved_at"])

    def paid(self):
        from django.db import transaction
        from roo.services import PointsService
        from integrations.services.article_generation import _ledger_reference_id
        from .dispatch_outbox import reserve_dispatch
        PointsService.credit_purchased_topup(user=self.built.owner, delta=30,
            description="Synthetic funding", idempotency_key="fund-"+uuid4().hex)
        key = uuid4().hex
        spend, _ = PointsService.spend(user=self.built.owner, delta=12, source="CONTENT_FACTORY",
            description="Synthetic article", created_by_slack_id="fixture", idempotency_key="content_factory:charge:"+key,
            reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
        request = {"client_request_id": key, "domain": self.org.domain, "topic": "Synthetic topic",
            "roo_points_ledger_id": spend.pk, "roo_points_cost": 12, "roo_points_billing_user_id": self.built.owner.pk,
            "roo_points_billing_status": "charged"}
        with transaction.atomic():
            reserve_dispatch(organization=self.org, payload=request, billing_user_id=self.built.owner.pk)
        return self.new_run(run_request=request, result={"error_code": "RUN_BUDGET_EXHAUSTED"}), spend

    def test_real_staff_refund_writes_exactly_one_original_credit(self):
        run, spend = self.paid()
        # Policy has finished; the staff action must use the existing no-delivery decision.
        with override_settings(ARTICLES_SELF_HEAL_POLICY_ENABLED=False):
            for key in ("one", "one", "two"):
                self.assertEqual(self.action(run, "release_or_refund", key=key)[1]["receipt"]["state"], "refunded")
        self.assertEqual(spend.service_refunds.count(), 1)
        from roo.models import Ledger
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)

    def test_real_portable_reservation_carries_before_dispatch_once_with_no_new_debit(self):
        run, spend = self.paid()
        from . import vibe_marketing_views as views
        from .dispatch_models import ContentFactoryDispatchOutbox
        from roo.models import Ledger
        with fake_provider("content_factory.channel_switch", switch_to_copy_ready=Mock()), patch.object(views, "analytics_config_for_content_factory", return_value={}), patch.object(views.http_client, "post") as post:
            first = self.action(run, "switch_to_draft", key="first")
            second = self.action(run, "switch_to_draft", key="other-staff-key")
        self.assertEqual(first[1]["status"], "done", first)
        self.assertEqual(second[1]["status"], "already_done")
        post.assert_not_called()
        child = ContentFactoryRun.objects.get(run_id=f"staff-switch:{self.org.pk}:1")
        row = ContentFactoryDispatchOutbox.objects.get(client_request_id=child.run_id)
        self.assertEqual(child.result["points_v1"]["state"], "carried")
        self.assertEqual(row.payload["request"]["roo_points_ledger_id"], spend.pk)
        self.assertEqual(row.payload["request"]["delivery_mode"], "content_only")
        self.assertEqual(row.payload["request"]["github_repo"], "")
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)


@override_settings(**ALL_FLAGS_ON)
class StaffConcurrencyTests(TransactionTestCase):
    def test_two_staff_requests_with_one_key_apply_once(self):
        if connection.vendor != "postgresql":
            self.skipTest("Real row-lock concurrency requires PostgreSQL.")
        from threading import Event, Thread
        from django.db import close_old_connections
        cache.clear()
        built = make_p2_org()
        staff = User.objects.create_superuser(email="concurrent-staff@example.test")
        run = make_stamped_run(built.organization)
        entered, release = Event(), Event()
        calls, outcomes, errors = [], [], []
        def apply(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(10)
            return {"action": "retry"}
        data = {"action": "retry_now", "runId": run.run_id, "idempotencyKey": "same-key"}
        def invoke():
            close_old_connections()
            try:
                outcomes.append(console.perform_action(built.organization.pk, data, by_user=staff)[0])
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()
        with fake_provider("content_factory.failure_policy_engine", decide=lambda *a, **k: {}, apply=apply):
            first = Thread(target=invoke)
            first.start()
            self.assertTrue(entered.wait(10))
            second = Thread(target=invoke)
            second.start()
            second.join(10)
            release.set()
            first.join(10)
        self.assertFalse(errors)
        self.assertEqual(sorted(outcomes), [200, 409])
        self.assertEqual(calls, [1])
        run.refresh_from_db()
        self.assertEqual(len(run.result["staff_actions_v1"]), 1)


@override_settings(ARTICLES_STAFF_ALERT_RULES_ENABLED=True)
class GithubSignalTests(TestCase):
    def test_early_native_and_later_failures_count_once_success_and_legacy_do_not(self):
        from types import SimpleNamespace
        from .staff_signals import total
        def module(flow):
            return SimpleNamespace(CONNECTED="connected", begin_callback=lambda request: flow)
        cache.clear()
        self.assertIsNone(rules.begin_github_callback(module(None), object()))
        success = SimpleNamespace(response=None, redirect=lambda reason: reason)
        self.assertEqual(rules.begin_github_callback(module(success), object()).redirect("connected"), "connected")
        self.assertEqual(total("github_callback_failed", window=timedelta(hours=1)), 0)
        early = SimpleNamespace(response=object(), redirect=lambda reason: reason)
        rules.begin_github_callback(module(early), object())
        early.redirect("cancelled")
        self.assertEqual(total("github_callback_failed", window=timedelta(hours=1)), 1)
        late = SimpleNamespace(response=None, redirect=lambda reason: reason)
        rules.begin_github_callback(module(late), object())
        late.redirect("no_repo")
        late.redirect("authorization_failed")
        self.assertEqual(total("github_callback_failed", window=timedelta(hours=1)), 2)
