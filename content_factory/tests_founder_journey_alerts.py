"""Founder-journey ops alerts on synthetic database rows (P0.1, IF-15)."""
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep
from . import founder_journey_alerts as alerts
from .editorial_catalog import merge_strategy, public_strategy
from .models import OrganizationContentConfig
from .service_views import _merge_django_owned_run_result
from .services import island_refresh_scheduler
from .website_models import WebsiteConnection

CHANNEL = "C0000000000"
SLACK = "integrations.services.slack.SlackService.get_client"


def acknowledged(**kwargs):
    return {"ok": True, "ts": f"1700000000.{kwargs['client_msg_id'][:6]}"}


class FounderJourneyFixture(TestCase):
    def setUp(self):
        self.now = timezone.now()
        rollout = override_settings(
            ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=True,
            ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE=(self.now - timedelta(days=60)).date().isoformat(),
            ARTICLES_FOUNDER_JOURNEY_ALERTS_MAX_PER_TICK=20,
            CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=CHANNEL,
            DEFAULT_BACKEND_URL="https://api.example.test",
        )
        rollout.enable()
        self.addCleanup(rollout.disable)
        self.user = get_user_model().objects.create_user(
            email="secret.founder@example.test", first_name="Secretfirst", last_name="Secretlast")
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.org = Organization.objects.create(domain="journey.example.test", name="Synthetic journey")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile, organization=self.org, domain=self.org.domain, name="Synthetic")

    def make_run(self, *, workflow="confirmed_topic", status="cancelled", error="", result=None, run_request=None,
                 step_message="", age=timedelta(hours=1), touched=None):
        run = ContentFactoryRun.objects.create(
            run_id=f"run-{uuid4().hex[:12]}", workflow=workflow, domain=self.org.domain, organization=self.org,
            status=status, error=error, result=result or {}, run_request=run_request or {})
        if step_message:
            ContentFactoryRunStep.objects.create(run=run, step_key="write", status="cancelled", message=step_message, attempts=2)
        ContentFactoryRun.objects.filter(pk=run.pk).update(
            created_at=self.now - age, updated_at=touched or (self.now - age))
        run.refresh_from_db()
        return run

    def age_company(self, hours=25):
        VibeRaisingCompany.objects.filter(pk=self.company.pk).update(created_at=self.now - timedelta(hours=hours))

    def sweep(self, now=None):
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            outcome = alerts.run_founder_journey_alert_sweep(now=now or self.now)
        return outcome, client.return_value.chat_postMessage.call_args_list

    def receipts(self, run):
        run.refresh_from_db()
        return run.result.get("ops_alerts", {})

    def org_receipts(self):
        return OrganizationContentConfig.objects.get(organization=self.org).pillar_strategy.get("ops_alerts", {})


class FounderJourneyAlertTests(FounderJourneyFixture):
    def test_unenrolled_org_gets_exactly_one_alert_each_and_a_replay_sends_nothing(self):
        self.age_company()
        cancelled = self.make_run(workflow="confirmed_topic", status="cancelled", step_message="Cancelled.")
        autofill = self.make_run(workflow="startup_autofill", status="blocked",
                                 result={"error_code": "PROFILE_DRAFT_UNAVAILABLE"})
        self.assertFalse(WebsiteConnection.objects.filter(organization=self.org).exists())

        outcome, calls = self.sweep()

        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["sent"], 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(self.receipts(cancelled)), {"run_terminal"})
        self.assertEqual(set(self.receipts(autofill)), {"startup_autofill_blocked"})
        company_alert = alerts.founder_journey_alert_id(
            organization_id=self.org.pk, alert_reason="no_draft_24h", company_id=self.company.pk)
        self.assertEqual(self.org_receipts()[company_alert]["state"], "sent")
        sent_ids = {call.kwargs["client_msg_id"] for call in calls}
        self.assertIn(company_alert, sent_ids)
        self.assertIn(self.receipts(cancelled)["run_terminal"]["alert_id"], sent_ids)
        autofill_text = next(call.kwargs["text"] for call in calls
                             if call.kwargs["client_msg_id"] == self.receipts(autofill)["startup_autofill_blocked"]["alert_id"])
        self.assertIn("PROFILE_DRAFT_UNAVAILABLE", autofill_text)
        self.assertIn(f"https://api.example.test/admin/workflow_runs/contentfactoryrun/{autofill.pk}/change/", autofill_text)
        self.assertIn(f"Org {self.org.pk} (journey.example.test)", autofill_text)
        for call in calls:
            self.assertEqual(call.kwargs["channel"], CHANNEL)

        replay, replay_calls = self.sweep(now=self.now + timedelta(minutes=10))
        self.assertEqual(replay_calls, [])
        self.assertEqual((replay["queued"], replay["sent"]), (0, 0))

    def test_owner_cancels_are_not_alerted_but_an_unspecified_cancel_is(self):
        owner = self.make_run(result={"cancellation_reason": "owner_cancelled", "cancelled_by": "owner"})
        deleted = self.make_run(result={"cancellation_reason": "draft_deleted", "cancelled_by": "owner"})
        discarded = self.make_run(result={"cancellation_reason": "article_discarded", "cancelled_by": "owner"})
        unspecified = self.make_run(result={"cancellation_reason": "unspecified", "cancelled_by": "system"},
                                    step_message="Cancelled by user.")

        _, calls = self.sweep()

        for run in (owner, deleted, discarded):
            self.assertEqual(self.receipts(run), {})
        self.assertEqual(set(self.receipts(unspecified)), {"run_terminal"})
        self.assertEqual(len(calls), 1)

    def test_legacy_codeless_owner_cancel_is_not_alerted(self):
        legacy = self.make_run(step_message="Cancelled by user.")
        with_error = self.make_run(error="Website connection changed.", step_message="Cancelled by user.")

        _, calls = self.sweep()

        self.assertEqual(self.receipts(legacy), {})
        self.assertEqual(set(self.receipts(with_error)), {"run_terminal"})
        self.assertEqual(len(calls), 1)

    def test_discarding_a_finished_draft_is_a_legacy_owner_cancel(self):
        discarded = self.make_run(workflow="confirmed_topic", status="cancelled",
                                  result={"status": "cancelled", "cancelled": True, "cleanup": {}, "warnings": []})
        ContentFactoryRunStep.objects.create(run=discarded, step_key="write", status="completed", message="Done.")
        interrupted = self.make_run(workflow="confirmed_topic", status="cancelled", result={"cancelled": True})
        ContentFactoryRunStep.objects.create(run=interrupted, step_key="write", status="running", message="Writing.")
        no_steps = self.make_run(workflow="confirmed_topic", status="cancelled", result={"cancelled": True})

        _, calls = self.sweep()

        self.assertEqual(self.receipts(discarded), {})
        self.assertEqual(set(self.receipts(interrupted)), {"run_terminal"})
        self.assertEqual(set(self.receipts(no_steps)), {"run_terminal"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(alerts.is_owner_cancel(discarded))
        self.assertFalse(alerts.is_owner_cancel(interrupted))

    def test_island_refresh_and_scheduler_runs_never_alert(self):
        island = self.make_run(workflow="island_refresh", status="denied")
        scheduled = self.make_run(workflow="topic_discovery", status="failed",
                                  run_request={"request_source": "mlai_backend_scheduler"})
        scheduled_block = self.make_run(workflow="topic_discovery", status="blocked",
                                        run_request={"request_source": "mlai_backend_scheduler"})

        _, calls = self.sweep()

        self.assertEqual(calls, [])
        for run in (island, scheduled, scheduled_block):
            self.assertEqual(self.receipts(run), {})

    def test_failed_denied_and_blocked_runs_in_any_workflow_alert(self):
        failed = self.make_run(workflow="article_system_setup", status="failed",
                               result={"failure": {"code": "DIRECTORY_STYLE_REVIEW_FAILED"}})
        denied = self.make_run(workflow="repo_scan", status="denied")
        blocked = self.make_run(workflow="article_system_setup", status="blocked")

        _, calls = self.sweep()

        self.assertEqual(set(self.receipts(failed)), {"run_terminal"})
        self.assertEqual(set(self.receipts(denied)), {"run_terminal"})
        self.assertEqual(set(self.receipts(blocked)), {"run_blocked"})
        failed_text = next(call.kwargs["text"] for call in calls if failed.run_id in call.kwargs["text"])
        self.assertIn("Code DIRECTORY_STYLE_REVIEW_FAILED.", failed_text)
        self.assertIn("Workflow article_system_setup.", failed_text)

    def test_runs_before_since_and_completed_runs_are_ignored(self):
        old = self.make_run(status="failed", age=timedelta(days=90))
        done = self.make_run(status="completed")

        _, calls = self.sweep()

        self.assertEqual(calls, [])
        self.assertEqual(self.receipts(old), {})
        self.assertEqual(self.receipts(done), {})

    def test_your_turn_for_72_hours_alerts_once_and_receipts_do_not_touch_the_run(self):
        stale = self.now - timedelta(hours=73)
        waiting = self.make_run(workflow="confirmed_topic", status="awaiting_approval", age=timedelta(hours=80), touched=stale)
        fresh = self.make_run(workflow="confirmed_topic", status="awaiting_delivery_mode", age=timedelta(hours=80),
                              touched=self.now - timedelta(hours=10))
        needs_action = self.make_run(workflow="article_system_setup", status="blocked", age=timedelta(hours=80), touched=stale,
                                     result={"failure": {"action": {"id": "reconnect_github"}}})

        _, calls = self.sweep()

        self.assertEqual(set(self.receipts(waiting)), {"your_turn_72h"})
        self.assertEqual(self.receipts(fresh), {})
        self.assertEqual(set(self.receipts(needs_action)), {"run_blocked", "your_turn_72h"})
        waiting.refresh_from_db()
        self.assertEqual(waiting.updated_at, stale)
        self.assertEqual(len(calls), 3)

    def test_company_with_a_delivered_draft_or_younger_than_a_day_gets_no_draft_alert(self):
        self.age_company(hours=5)
        self.sweep()
        self.assertFalse(OrganizationContentConfig.objects.filter(organization=self.org).exists())

        self.age_company(hours=30)
        self.make_run(workflow="confirmed_topic", status="awaiting_approval")
        _, calls = self.sweep()
        self.assertEqual(calls, [])

    def test_empty_channel_logs_the_alert_and_marks_it_unconfigured(self):
        run = self.make_run(status="failed")
        with override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID=""), patch(SLACK) as client, \
                self.assertLogs("content_factory.founder_journey_alerts", level="WARNING") as logs:
            outcome = alerts.run_founder_journey_alert_sweep(now=self.now)
        client.assert_not_called()
        receipt = self.receipts(run)["run_terminal"]
        self.assertEqual(receipt["state"], "unconfigured")
        self.assertEqual(outcome["unconfigured"], 1)
        record = next(item for item in logs.records if item.getMessage().startswith("founder_journey_alert "))
        self.assertEqual(record.alert_id, receipt["alert_id"])
        self.assertEqual(record.alert_reason, "run_terminal")
        self.assertEqual(record.org_id, self.org.pk)
        self.assertIn(f"run_id={run.run_id}", record.getMessage())

        _, calls = self.sweep(now=self.now + timedelta(minutes=10))
        self.assertEqual(calls, [])

    def test_invalid_channel_is_treated_as_unconfigured(self):
        run = self.make_run(status="failed")
        with override_settings(CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID="#ops-alerts"), patch(SLACK) as client:
            alerts.run_founder_journey_alert_sweep(now=self.now)
        client.assert_not_called()
        self.assertEqual(self.receipts(run)["run_terminal"]["state"], "unconfigured")

    def test_three_failed_deliveries_mark_the_receipt_failed(self):
        run = self.make_run(status="failed")
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = RuntimeError("slack down")
            for minutes in (0, 1, 6, 12, 18, 30):
                alerts.run_founder_journey_alert_sweep(now=self.now + timedelta(minutes=minutes))
        self.assertEqual(client.return_value.chat_postMessage.call_count, 3)
        receipt = self.receipts(run)["run_terminal"]
        self.assertEqual((receipt["state"], receipt["attempts"]), ("failed", 3))
        self.assertIsNone(receipt["sent_at"])

    def test_an_interrupted_final_attempt_is_never_sent_a_fourth_time(self):
        run = self.make_run(status="failed")
        alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal", run=run)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        strategy = config.pillar_strategy
        strategy["ops_alerts"][alert_id].update(attempts=3, next_attempt_at=(self.now - timedelta(minutes=1)).isoformat())
        OrganizationContentConfig.objects.filter(pk=config.pk).update(pillar_strategy=strategy)
        _, calls = self.sweep()
        self.assertEqual(calls, [])
        self.assertEqual(self.org_receipts()[alert_id]["state"], "failed")
        self.assertEqual(self.receipts(run)["run_terminal"]["state"], "failed")

    def test_a_status_poll_that_replaces_the_run_result_never_causes_a_second_alert(self):
        from . import vibe_marketing_views as views
        autofill = self.make_run(workflow="startup_autofill", status="blocked",
                                 result={"error_code": "PROFILE_DRAFT_UNAVAILABLE"})
        discovery = self.make_run(workflow="auto_discovery", status="failed", error="Discovery failed.")
        article = self.make_run(workflow="confirmed_topic", status="failed", error="Writer failed.")

        _, first = self.sweep()
        self.assertEqual(len(first), 3)
        for run, status in ((autofill, "blocked"), (discovery, "failed"), (article, "failed")):
            run.refresh_from_db()
            views._sync_local_run_from_remote(run, {"status": status, "error": run.error,
                                                    "result": {"error_code": "PROFILE_DRAFT_UNAVAILABLE", "note": "poll"}})
        autofill.refresh_from_db()
        self.assertEqual(autofill.result.get("note"), "poll")

        _, second = self.sweep(now=self.now + timedelta(minutes=10))
        self.assertEqual(second, [])
        _, third = self.sweep(now=self.now + timedelta(minutes=20))
        self.assertEqual(third, [])
        self.assertTrue(all(receipt["state"] == "sent" for receipt in self.org_receipts().values()))
        self.assertEqual(len(self.org_receipts()), 3)

    def test_a_poll_before_delivery_still_delivers_exactly_once(self):
        from . import vibe_marketing_views as views
        run = self.make_run(workflow="startup_autofill", status="blocked")
        alert_id = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="startup_autofill_blocked", run=run)
        run.refresh_from_db()
        views._sync_local_run_from_remote(run, {"status": "blocked", "result": {"note": "poll"}})
        run.refresh_from_db()
        self.assertNotIn("ops_alerts", run.result)

        _, calls = self.sweep()
        self.assertEqual([call.kwargs["client_msg_id"] for call in calls], [alert_id])
        self.assertIn(f"Run {run.run_id}. Workflow startup_autofill.", calls[0].kwargs["text"])
        self.assertEqual(self.receipts(run)["startup_autofill_blocked"]["state"], "sent")
        _, replay = self.sweep(now=self.now + timedelta(minutes=10))
        self.assertEqual(replay, [])

    def test_a_receipt_only_the_run_holds_is_adopted_not_resent(self):
        run = self.make_run(status="failed")
        alert_id = alerts.founder_journey_alert_id(organization_id=self.org.pk, alert_reason="run_terminal", run_id=run.run_id)
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={"ops_alerts": {"run_terminal": {
            "alert_id": alert_id, "alert_reason": "run_terminal", "state": "sent", "attempts": 1, "sent_at": "x"}}})
        run.refresh_from_db()
        self.assertEqual(alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal", run=run), alert_id)
        self.assertEqual(self.org_receipts()[alert_id]["state"], "sent")
        _, calls = self.sweep()
        self.assertEqual(calls, [])

    def test_a_retry_after_one_failure_is_sent_with_the_same_client_msg_id(self):
        run = self.make_run(status="failed")
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = [RuntimeError("timeout"), {"ok": True, "ts": "1.2"}]
            alerts.run_founder_journey_alert_sweep(now=self.now)
            alerts.run_founder_journey_alert_sweep(now=self.now + timedelta(minutes=6))
        ids = {call.kwargs["client_msg_id"] for call in client.return_value.chat_postMessage.call_args_list}
        receipt = self.receipts(run)["run_terminal"]
        self.assertEqual(ids, {receipt["alert_id"]})
        self.assertEqual((receipt["state"], receipt["attempts"], receipt["message_id"]), ("sent", 2, "1.2"))
        self.assertTrue(receipt["sent_at"])

    def test_max_per_tick_bounds_new_alerts(self):
        runs = [self.make_run(status="failed") for _ in range(3)]
        with patch(SLACK) as client:
            client.return_value.chat_postMessage.side_effect = acknowledged
            first = alerts.run_founder_journey_alert_sweep(now=self.now, limit=2)
            second = alerts.run_founder_journey_alert_sweep(now=self.now, limit=2)
        self.assertEqual((first["queued"], first["sent"]), (2, 2))
        self.assertEqual((second["queued"], second["sent"]), (1, 1))
        self.assertTrue(all(self.receipts(run)["run_terminal"]["state"] == "sent" for run in runs))

    def test_flag_off_means_no_sweep(self):
        run = self.make_run(status="failed")
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False), patch(SLACK) as client:
            self.assertEqual(alerts.run_founder_journey_alert_sweep(now=self.now), {"status": "disabled"})
        client.assert_not_called()
        self.assertEqual(self.receipts(run), {})

    def test_invalid_since_fails_the_runner_loudly(self):
        with override_settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_SINCE="last tuesday"):
            outcome = alerts.run_founder_journey_alert_sweep(now=self.now)
        self.assertEqual(outcome["status"], "failed")

    def test_alert_text_and_receipts_hold_no_email_name_or_article_text(self):
        self.age_company()
        run = self.make_run(status="failed", error="secret.founder@example.test could not publish",
                            result={"article": "SECRET ARTICLE BODY", "markdown": "SECRET ARTICLE BODY", "error_code": "EDITORIAL_REJECTED"})
        alerts.queue_founder_journey_alert(
            organization=self.org, alert_reason="first_article_attempts_exhausted", company_id=self.company.pk,
            details={"founder_email": self.user.email, "founder_name": "Secretfirst", "article_title": "Secret",
                     "note": "Secretfirst Secretlast wrote this", "contact": self.user.email, "attempts": 2})

        _, calls = self.sweep()

        stored = str(self.receipts(run)) + str(self.org_receipts())
        texts = " ".join(call.kwargs["text"] for call in calls)
        self.assertEqual(len(calls), 3)
        for private in ("secret.founder", "Secretfirst", "Secretlast", "SECRET ARTICLE BODY", "@"):
            self.assertNotIn(private, texts)
            self.assertNotIn(private, stored)
        self.assertIn("attempts=2", texts)
        self.assertIn("Code EDITORIAL_REJECTED.", texts)


class QueueFounderJourneyAlertTests(FounderJourneyFixture):
    def test_queue_is_idempotent_for_org_and_run_scoped_alerts(self):
        first = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="free_cap_spike")
        receipt = dict(self.org_receipts()[first])
        second = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="free_cap_spike")
        self.assertEqual(first, second)
        self.assertEqual(self.org_receipts(), {first: receipt})
        self.assertEqual(receipt["state"], "pending")

        run = self.make_run(status="failed")
        run_first = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="first_article_attempts_exhausted", run=run)
        run_second = alerts.queue_founder_journey_alert(organization=self.org, alert_reason="first_article_attempts_exhausted", run=run)
        self.assertEqual(run_first, run_second)
        self.assertEqual(list(self.receipts(run)), ["first_article_attempts_exhausted"])
        # The caller's in-memory run keeps the receipt, so a later save cannot erase it.
        self.assertIn("first_article_attempts_exhausted", run.result["ops_alerts"])

    def test_alert_id_matches_the_contract_slot_rules(self):
        from uuid import NAMESPACE_URL, uuid5
        run = self.make_run(status="failed")
        self.assertEqual(
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal", run=run, company_id=self.company.pk),
            str(uuid5(NAMESPACE_URL, f"mlai:founder-journey:{self.org.pk}:{run.run_id}:run_terminal")))
        self.assertEqual(
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="no_draft_24h", company_id=self.company.pk),
            str(uuid5(NAMESPACE_URL, f"mlai:founder-journey:{self.org.pk}:{self.company.pk}:no_draft_24h")))
        self.assertEqual(
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="domain_claim_disputed",
                                               details={"claimant_user_id": 41}),
            str(uuid5(NAMESPACE_URL, f"mlai:founder-journey:{self.org.pk}:claimant-user-41:domain_claim_disputed")))
        self.assertEqual(
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="free_cap_spike"),
            str(uuid5(NAMESPACE_URL, f"mlai:founder-journey:{self.org.pk}:-:free_cap_spike")))

    def test_two_claimants_get_two_domain_alerts_and_a_replay_gets_none(self):
        other = get_user_model().objects.create_user(email="other.claimant@example.test")
        OrganizationContentConfig.objects.filter(organization=self.org).delete()
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="domain_claim_disputed",
                                           details={"claimant_user_id": self.user.pk})
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="domain_claim_disputed",
                                           details={"claimant_user_id": other.pk})
        _, calls = self.sweep()
        self.assertEqual(len(calls), 2)
        self.assertIn(f"claimant_user_id={other.pk}", " ".join(call.kwargs["text"] for call in calls))

        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="domain_claim_disputed",
                                           details={"claimant_user_id": self.user.pk})
        _, replay = self.sweep(now=self.now + timedelta(minutes=10))
        self.assertEqual(replay, [])
        self.assertEqual(len(self.org_receipts()), 2)

    def test_unknown_reason_or_missing_org_is_rejected(self):
        with self.assertRaises(ValueError):
            alerts.queue_founder_journey_alert(organization=self.org, alert_reason="reason_code")
        with self.assertRaises(ValueError):
            alerts.queue_founder_journey_alert(organization=None, alert_reason="free_cap_spike")

    def test_company_alert_links_to_the_company_admin(self):
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="first_article_attempts_exhausted",
                                           company_id=self.company.pk)
        _, calls = self.sweep()
        self.assertIn(f"https://api.example.test/admin/founder_tools/viberaisingcompany/{self.company.pk}/change/",
                      calls[0].kwargs["text"])


class PrivateStrategyKeyTests(FounderJourneyFixture):
    def test_merge_strategy_keeps_and_public_strategy_hides_backend_receipts(self):
        existing = {"pillars": [{"slug": "a"}], "ops_alerts": {"id": {"state": "sent"}},
                    "previous_owner": {"user_id": 7, "company_id": 3}}
        merged = merge_strategy(existing, {"pillars": [{"slug": "b"}], "ops_alerts": {}, "previous_owner": None})
        self.assertEqual(merged["ops_alerts"], existing["ops_alerts"])
        self.assertEqual(merged["previous_owner"], existing["previous_owner"])
        self.assertEqual(merged["pillars"], [{"slug": "b"}])
        self.assertNotIn("ops_alerts", merge_strategy({}, {"ops_alerts": {"forged": {}}}))
        visible = public_strategy(merged)
        self.assertNotIn("ops_alerts", visible)
        self.assertNotIn("previous_owner", visible)
        self.assertEqual(visible["pillars"], [{"slug": "b"}])

    def test_a_config_holding_only_private_keys_is_not_island_refresh_eligible(self):
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="free_cap_spike")
        config = OrganizationContentConfig.objects.get(organization=self.org)
        config.pillar_strategy = {**config.pillar_strategy, "previous_owner": {"user_id": 1}}
        config.save(update_fields=["pillar_strategy"])
        self.assertNotIn(config.pk, {item.pk for item in island_refresh_scheduler._eligible_configs()})

        config.pillar_strategy = {**config.pillar_strategy, "topics": ["kept eligible"]}
        config.save(update_fields=["pillar_strategy"])
        self.assertIn(config.pk, {item.pk for item in island_refresh_scheduler._eligible_configs()})

    def test_worker_snapshot_keeps_backend_receipts_and_cancel_reasons(self):
        existing = {"ops_alerts": {"run_terminal": {"state": "sent"}}, "cancellation_reason": "owner_cancelled",
                    "cancelled_by": "owner", "status": "cancelled"}
        for incoming in ({"status": "cancelled"},
                         {"status": "cancelled", "ops_alerts": {}, "cancellation_reason": "", "cancelled_by": None},
                         {"status": "cancelled", "ops_alerts": {"run_terminal": {"state": "pending"}},
                          "cancellation_reason": "unspecified", "cancelled_by": "system"}):
            merged = _merge_django_owned_run_result(existing, incoming)
            self.assertEqual(merged["ops_alerts"], existing["ops_alerts"])
            self.assertEqual((merged["cancellation_reason"], merged["cancelled_by"]), ("owner_cancelled", "owner"))
        worker_reason = _merge_django_owned_run_result({}, {"cancellation_reason": "worker_heartbeat_lost", "cancelled_by": "system"})
        self.assertEqual(worker_reason["cancellation_reason"], "worker_heartbeat_lost")

    @override_settings(ROO_API_KEY="synthetic-test-key", INTERNAL_API_KEY="synthetic-test-key")
    def test_a_cf_run_put_snapshot_keeps_receipts_and_cancel_reasons(self):
        from tests.website_fixtures import CallbackFixtureClient
        run = self.make_run(workflow="repo_scan", status="failed",
                            result={"status": "failed", "cancellation_reason": "unspecified", "cancelled_by": "system"})
        alerts.queue_founder_journey_alert(organization=self.org, alert_reason="run_terminal", run=run)
        client = CallbackFixtureClient()
        client.credentials(HTTP_X_API_KEY="synthetic-test-key")
        response = client.put(f"/api/content-factory/runs/{run.run_id}/", {
            "run_id": run.run_id, "workflow": run.workflow, "domain": run.domain, "status": "failed",
            "current_step": "scan", "result": {"status": "failed", "error_code": "REPOSITORY_SCAN_FAILED"}, "step_states": {},
        }, format="json")
        self.assertIn(response.status_code, {200, 201}, response.content)
        run.refresh_from_db()
        self.assertEqual(run.result["error_code"], "REPOSITORY_SCAN_FAILED")
        self.assertIn("run_terminal", run.result["ops_alerts"])
        self.assertEqual((run.result["cancellation_reason"], run.result["cancelled_by"]), ("unspecified", "system"))


class FailureContractTests(TestCase):
    def test_alert_module_adds_no_scanned_code_fields(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("check_failure_contract", root / "scripts/check_failure_contract.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        for relative in ("content_factory/founder_journey_alerts.py", "content_factory/management/commands/founder_funnel_report.py"):
            self.assertEqual(checker.python_emissions((root / relative).read_text(), relative), [])

    def test_the_sweep_is_a_scheduled_runner(self):
        source = (Path(__file__).resolve().parents[1] / "core/management/commands/run_scheduled_discovery.py").read_text()
        self.assertIn('("founder_journey_alerts", run_founder_journey_alert_sweep)', source)
