"""Owned research cancellation with synthetic persistence and no network."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import island_research_cancel as cancel
from . import vibe_marketing_views as views


def research_run():
    return SimpleNamespace(pk=1, run_id="research-one", workflow="island_refresh", status="running",
        current_step="research_sources", run_request={"island_research_brief": {"subject": "Balcony herbs"}},
        result={}, steps=Mock(), save=Mock(), domain="example.test")


class IslandResearchCancelTests(SimpleTestCase):
    def control(self, run, remote, *, owned=True, locked=None):
        context = SimpleNamespace(organization=SimpleNamespace(domain=run.domain))
        request = SimpleNamespace(data={"companyId": "company-one"}, user=SimpleNamespace(pk=7))
        with patch.object(views, "_resolve_context_or_response", return_value=(context, None)), \
                patch.object(views, "get_object_or_404", return_value=run), \
                patch.object(views, "_run_belongs_to_context", return_value=owned), \
                patch.object(views, "founder_actor_id_for_user", return_value="web:7"), \
                patch.object(views, "_call_content_factory_run_action", return_value=remote) as worker, \
                patch.object(views, "_serialize_run", side_effect=lambda row, **kw: {
                    "runId": row.run_id, "status": row.status, "result": row.result}), \
                patch.object(cancel.transaction, "atomic", return_value=nullcontext()), \
                patch.object(cancel.ContentFactoryRun.objects, "select_for_update") as query, \
                patch.object(cancel, "refund_empty_or_failed_research") as refund:
            query.return_value.get.return_value = locked or run
            response = views.VibeMarketingRunControlView.post.__wrapped__(
                views.VibeMarketingRunControlView(), request, run.run_id, "cancel")
        return response, worker, refund

    def test_confirmed_cancellation_preserves_brief_and_completed_steps_and_replays(self):
        run = research_run()
        brief = deepcopy(run.run_request)
        receipt = {"run_id": run.run_id, "status": "cancelled"}
        response, worker, refund = self.control(run, receipt)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "cancelled")
        self.assertEqual(run.run_request, brief)
        run.steps.exclude.assert_called_once_with(status="completed")
        self.assertEqual(run.steps.exclude.return_value.update.call_args.kwargs["status"], "cancelled")
        self.assertFalse(run.resume_available)
        worker.assert_called_once()
        refund.assert_called_once_with(run)
        replay, worker, _ = self.control(run, receipt)
        self.assertEqual(replay.data, response.data)
        worker.assert_not_called()

    def test_unknown_or_failed_worker_cancellation_never_stops_or_refunds_locally(self):
        for receipt in ({}, {"status": "running", "run_id": "research-one"},
                        {"status": "cancelled", "run_id": "other"},
                        {"error": "Worker unavailable", "content_factory_status_code": 503},
                        {"error": "Worker refused", "content_factory_status_code": 409}):
            run = research_run()
            with self.subTest(receipt=receipt):
                response, _, refund = self.control(run, receipt)
                self.assertIn(response.status_code, (409, 502))
                self.assertEqual(run.status, "running")
                run.save.assert_not_called()
                refund.assert_not_called()

    def test_local_completion_wins_racing_cancel_and_retains_results(self):
        run = research_run()
        completed = research_run()
        completed.status = "completed"
        completed.result = {"suggested_islands": [{"id": "measured"}]}
        response, _, _ = self.control(run, {"status": "cancelled", "run_id": run.run_id}, locked=completed)
        self.assertEqual(response.data["status"], "completed")
        self.assertEqual(response.data["result"], completed.result)
        completed.save.assert_not_called()

    def test_worker_completion_is_synced_instead_of_cancelling(self):
        run = research_run()
        completed = research_run()
        completed.status = "completed"
        completed.result = {"suggested_islands": [{"id": "measured"}]}
        receipt = {"status": "completed", "run_id": run.run_id, "result": completed.result}
        with patch.object(views, "_sync_local_run_from_remote", return_value=completed) as sync:
            response, _, _ = self.control(run, receipt)
        sync.assert_called_once_with(run, receipt)
        self.assertEqual(response.data["status"], "completed")

    def test_other_companies_and_scheduled_refreshes_cannot_use_research_cancellation(self):
        run = research_run()
        response, worker, refund = self.control(run, {}, owned=False)
        self.assertEqual(response.status_code, 404)
        worker.assert_not_called(); refund.assert_not_called()
        run.run_request = {}
        response, worker, refund = self.control(run, {})
        self.assertEqual(response.status_code, 400)
        worker.assert_not_called(); refund.assert_not_called()

    def test_unresolved_dispatch_and_terminal_denial_do_not_dispatch_cancellation(self):
        run = research_run()
        run.run_request["dispatch_pending_resolution"] = True
        response, worker, refund = self.control(run, {})
        self.assertEqual(response.status_code, 409)
        worker.assert_not_called(); refund.assert_not_called()
        del run.run_request["dispatch_pending_resolution"]
        run.status = "denied"
        response, worker, refund = self.control(run, {})
        self.assertEqual(response.data["status"], "denied")
        worker.assert_not_called()
        run.save.assert_not_called()
