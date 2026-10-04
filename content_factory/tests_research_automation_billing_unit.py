"""Scheduled and manual research use the same paid, keyed dispatch contract."""
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from content_factory import notification_channel_views as api, service_views as service, vibe_marketing_views as marketing
from content_factory.models import AutomationRunStatus
from integrations.services import article_generation as billing, research_automations as research


class ResearchAutomationBillingTests(SimpleTestCase):
    def setUp(self):
        self.user = Obj(pk=7, id=7)
        self.org = Obj(pk=9, domain="fixture.test")
        self.automation = Obj(status="active", pause_if_unanswered=False, organization=self.org, organization_id=9, user=self.user, notification_channel=Obj(user=self.user))
        self.run = Obj(pk="run-1", slot_index=0, id="run-1", automation_id="automation-1", automation=self.automation,
            status=AutomationRunStatus.SCHEDULED, last_error="", save=Mock(),
            idempotency_key="research-automation:automation-1:2026-10-04:0", scheduled_for_at=timezone.now())
        self.payload = {"domain": "fixture.test", "requested_topic_count": 3, "requested_by_slack_user_id": "mlai_user:7"}

    def harness(self, stack, *, remote=None, charge_error=None):
        stack.enter_context(patch.object(research.transaction, "atomic", return_value=nullcontext()))
        runs = stack.enter_context(patch.object(research.AutomationRun, "objects"))
        runs.select_related.return_value.get.return_value = self.run
        runs.filter.return_value.exclude.return_value.exists.return_value = False
        stack.enter_context(patch.object(research.Organization.objects, "select_for_update"))
        runs.select_for_update.return_value.select_related.return_value.get.return_value = self.run
        stack.enter_context(patch.object(research.ResearchAutomation, "objects"))
        stack.enter_context(patch.object(research, "_discovery_payload_for_run", return_value=dict(self.payload)))
        stack.enter_context(patch("integrations.services.github_installations.resolve_user_for_actor_id", return_value=self.user))
        charge = stack.enter_context(patch.object(research, "charge_content_factory_topic_generation_for_user", side_effect=charge_error, return_value=(self.user, Obj(pk=13), 3)))
        stack.enter_context(patch.object(research, "_content_factory_balance_for_user", return_value=7))
        stack.enter_context(patch.object(marketing, "_get_config", return_value=Obj(github_repo="")))
        queue = stack.enter_context(patch.object(marketing, "_queue_content_factory_run", return_value=remote or Obj(run_id="remote-1", status="queued", run_request={}, error="")))
        track = stack.enter_context(patch.object(research, "_store_job_tracking_record"))
        refund = stack.enter_context(patch.object(research, "refund_content_factory_topic_generation_for_user"))
        return charge, queue, track, refund

    def test_scheduled_research_charges_three_once_and_uses_keyed_queue_before_setup(self):
        with ExitStack() as stack:
            charge, queue, track, refund = self.harness(stack)
            result = research.dispatch_automation_run("run-1")
        self.assertEqual(result["status"], "queued")
        charge.assert_called_once()
        payload = queue.call_args.kwargs["payload"]
        self.assertEqual((payload["roo_points_cost"], payload["roo_points_required"], payload["roo_points_action"]), (3, 3, "content_island_topic_generation"))
        self.assertTrue(payload["client_request_id"].startswith("automation-research:"))
        self.assertEqual(track.call_args.kwargs["billing_amount"], 3)
        self.assertEqual(track.call_args.kwargs["billing_ledger_id"], 13)
        self.assertEqual(track.call_args.kwargs["billing_status"], "charged")
        self.assertEqual(queue.call_args.kwargs["billing_refund_context"]["kind"], "content_island_topic_generation")
        refund.assert_not_called()

    def test_repeated_run_is_skipped_before_debit(self):
        self.run.status = AutomationRunStatus.QUEUED
        with ExitStack() as stack:
            charge, queue, _, _ = self.harness(stack)
            result = research.dispatch_automation_run("run-1")
        self.assertEqual(result["status"], "skipped")
        charge.assert_not_called()
        queue.assert_not_called()

    def test_insufficient_points_does_not_dispatch(self):
        with ExitStack() as stack:
            _, queue, _, refund = self.harness(stack, charge_error=billing.InsufficientRooPointsError({"message": "Need3"}))
            result = research.dispatch_automation_run("run-1")
        self.assertEqual(result["error"], "insufficient_roo_points")
        queue.assert_not_called()
        refund.assert_not_called()

    def test_ambiguous_keyed_dispatch_keeps_charge_and_canonical_recovery_context(self):
        remote = Obj(run_id="dispatch-key", status="blocked", run_request={"dispatch_pending_resolution": True}, error="Checking dispatch")
        with ExitStack() as stack:
            _, queue, track, refund = self.harness(stack, remote=remote)
            result = research.dispatch_automation_run("run-1")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(track.call_args.kwargs["billing_status"], "charged")
        refund.assert_not_called()

    def test_failed_queue_reuses_canonical_refund_instead_of_refunding_twice(self):
        with ExitStack() as stack:
            _, _, track, refund = self.harness(stack, remote=Obj(run_id="dispatch-key", status="blocked", run_request={}, error="Unavailable"))
            result = research.dispatch_automation_run("run-1")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(track.call_args.kwargs["billing_status"], "refunded")
        refund.assert_not_called()

    def test_run_now_changed_quote_rejects_before_creating_run(self):
        request = Obj(data={"companyId": "startup-1", "expectedCostPoints": 0}, user=self.user)
        with patch.object(api, "_resolve_context_or_response", return_value=(Obj(organization=self.org), None)), \
             patch.object(api, "start_manual_automation_run") as start:
            response = api.VibeMarketingResearchAutomationRunNowView().post(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["costPoints"], 3)
        start.assert_not_called()

    def test_manual_run_reuses_explicit_scoped_key_after_terminal_result(self):
        automation = Obj(id="automation-1", timezone="Australia/Melbourne")
        with patch.object(research.ResearchAutomation, "objects") as automations, \
             patch.object(research.NotificationChannel, "objects") as channels, \
             patch.object(research.AutomationRun, "objects") as runs, \
             patch.object(research, "dispatch_automation_run") as dispatch:
            automations.filter.return_value.order_by.return_value.first.return_value = automation
            channels.filter.return_value.exists.return_value = True
            runs.filter.return_value.first.return_value = Obj(id="terminal-1", status="completed")
            result = research.start_manual_automation_run(self.org, requested_by_user_id=7, request_id="retry-1")
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["automation_run_id"], "terminal-1")
        lookup = runs.filter.call_args.kwargs
        self.assertIs(lookup["automation"], automation)
        self.assertTrue(lookup["idempotency_key"].startswith("manual-research:"))
        dispatch.assert_not_called()

    def test_terminal_research_refund_uses_recorded_three_not_article_price(self):
        job = Obj(client_request_id="research-key", billing_status="charged", billing_amount=3,
            request_meta={"roo_points_action": "content_island_topic_generation", "requested_topic_count": 3,
                          "requested_by_slack_user_id": "mlai_user:7"}, domain="fixture.test", slack_user_id="", save=Mock())
        with patch.object(billing, "_get_existing_billed_source_job", return_value=None), \
             patch.object(billing, "_get_content_factory_user_for_job", return_value=self.user), \
             patch.object(billing, "refund_content_factory_topic_generation_for_user") as refund, \
             patch.object(billing, "_refund_content_factory_request") as article_refund:
            result = billing.maybe_auto_refund_terminal_failure(job, error_code="EMPTY_RESEARCH_RESULT", error_message="No topics")
            repeated = billing.maybe_auto_refund_terminal_failure(job, error_code="EMPTY_RESEARCH_RESULT", error_message="No topics")
        self.assertEqual(result, (True, 3))
        self.assertEqual(repeated, (False, 0))
        refund.assert_called_once()
        article_refund.assert_not_called()

    def test_article_confirmation_does_not_reuse_previous_research_quote(self):
        original = {"roo_points_action": "content_island_topic_generation", "expectedCostPoints": 3,
                    "client_request_id": "research-key"}
        job = Obj(job_id="research-1", client_request_id="research-key", request_meta=original,
                  billing_status="charged", save=Mock())
        def charge(actor, payload, domain, **kwargs):
            self.assertNotIn("expectedCostPoints", payload)
            return self.user, Obj(pk=14), 6
        with patch.object(billing, "_charge_content_factory_request", side_effect=charge):
            billing._charge_deferred_discovery_job_if_needed(source_job=job, slack_user_id="mlai_user:7",
                domain="fixture.test", confirmed_keyword="Culture")
        self.assertEqual(job.billing_amount, 6)
        self.assertEqual(job.request_meta["roo_points_action"], "article_generation")
        self.assertEqual(original["expectedCostPoints"], 3)

    def test_poll_resolves_only_scoped_pending_research_and_surfaces_confirmed_absence(self):
        from workflow_runs.models import ContentFactoryRun
        self.run.status = AutomationRunStatus.QUEUED
        self.run.content_factory_run_id = 'dispatch-key'
        failed = Obj(run_id='dispatch-key', status='failed', error='Queue did not accept the run.')
        with patch.object(ContentFactoryRun, 'objects') as rows, \
             patch.object(marketing, '_run_pending_remote_dispatch', return_value=True), \
             patch.object(marketing, '_resolve_dispatch_token_run', return_value=failed) as resolve:
            local = Obj()
            rows.filter.return_value.first.return_value = local
            research.reconcile_automation_research_dispatch(self.run)
        rows.filter.assert_called_once_with(run_id='dispatch-key', domain='fixture.test', workflow='auto_discovery')
        resolve.assert_called_once_with(local)
        self.assertEqual(self.run.status, AutomationRunStatus.FAILED)
        self.assertIn('Queue did not accept', self.run.last_error)

    def test_empty_research_callback_refunds_and_stops_waiting_for_topic(self):
        from content_factory.models import ContentFactoryJob, ScheduledDiscoveryDispatch
        job = Obj(request_meta={'roo_points_action': 'content_island_topic_generation'},
                  billing_status='charged', save=Mock())
        with patch.object(ContentFactoryJob, 'objects') as jobs, \
             patch.object(ScheduledDiscoveryDispatch, 'objects') as scheduled, \
             patch.object(service.ContentFactoryCallbackView, '_callback_requested_by_slack_user_id', return_value=''), \
             patch('integrations.services.daily_discovery.is_scheduled_daily_job', return_value=False), \
             patch('integrations.services.notification_adapters.normalize_notification_context', return_value={}), \
             patch.object(billing, 'maybe_auto_refund_terminal_failure', return_value=(True, 3)) as refund:
            jobs.update_or_create.return_value = (job, False)
            scheduled.filter.return_value.first.return_value = None
            response = service.ContentFactoryCallbackView()._handle_topic_selection(
                {'job_id': 'research-1', 'domain': 'fixture.test', 'selection': {'options': []}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['awaiting_confirmation'])
        self.assertEqual(job.status, 'error')
        refund.assert_called_once()


@override_settings(ROO_API_KEY="synthetic-service-key")
class WorkerArticleAdmissionTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.params = {"article_admission": "1", "domain": "fixture.test", "github_repo": "fixture/site", "requested_by_slack_user_id": "mlai_user:7"}
        self.user = Obj(id=7, pk=7)
        self.config = Obj(github_repo="fixture/site", organization=Obj(domain="fixture.test"), website_connection_id=None)
        self.context = Obj(organization=self.config.organization, profile=Obj(user=self.user))

    def call(self, params=None, key=True):
        request = self.factory.get("/api/content-factory/org/config", params or self.params, **({"HTTP_X_API_KEY": "synthetic-service-key"} if key else {}))
        return service.ContentFactoryOrgConfigView.as_view()(request)

    def test_service_authentication_required_before_any_admission(self):
        with patch.object(service, "_worker_article_admission_response") as admit:
            self.assertEqual(self.call(key=False).status_code, 403)
        admit.assert_not_called()

    def test_scope_and_actor_required_without_generic_config_lookup(self):
        self.assertEqual(self.call({"article_admission": "1"}).status_code, 400)
        with patch("integrations.services.github_installations.resolve_user_for_actor_id", return_value=None):
            self.assertEqual(self.call().status_code, 403)

    def test_scoped_fresh_capability_success_and_closed_failure(self):
        for ready in (True, False):
            with self.subTest(ready=ready), \
                 patch("integrations.services.github_installations.resolve_user_for_actor_id", return_value=self.user), \
                 patch("core.actor_ids.actor_ids_for_user", return_value=["mlai_user:7"]), \
                 patch("content_factory.activation.founder_context_for_domain", return_value=self.context) as owned, \
                 patch.object(marketing, "_get_config", return_value=self.config), \
                 patch.object(marketing, "_article_capabilities_for_context", return_value={"version": 1, "canGenerateArticle": ready, "reasonCode": "verification_required", "reason": "Verify setup"}) as capability:
                response = self.call()
            self.assertEqual(response.status_code, 200 if ready else 409)
            owned.assert_called_once_with(self.user, "fixture.test")
            self.assertTrue(capability.call_args.kwargs["force"])
            self.assertEqual(response.data["github_repo"], "fixture/site")
            self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_switched_repo_or_different_owner_rejects_without_verification(self):
        for cfg in (None, Obj(github_repo="fixture/other", website_connection_id=None)):
            with patch("integrations.services.github_installations.resolve_user_for_actor_id", return_value=self.user), \
                 patch("core.actor_ids.actor_ids_for_user", return_value=["mlai_user:7"]), \
                 patch("content_factory.activation.founder_context_for_domain", return_value=self.context,
                       side_effect=PermissionError("Wrong founder") if cfg is None else None), \
                 patch.object(marketing, "_get_config", return_value=cfg), \
                 patch.object(marketing, "_article_capabilities_for_context") as capability:
                self.assertEqual(self.call().status_code, 403 if cfg is None else 409)
                capability.assert_not_called()
