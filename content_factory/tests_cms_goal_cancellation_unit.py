"""Only original-key CF stop observations can release unused native money."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import cms_goal_cancellation as cancellation
from . import tests_cms_goal_refunds_unit as fixtures
from .dispatch_models import ContentFactoryDispatchOutbox
from .website_contract import WebsiteAuthorityError

stop_unused = cancellation.stop_unused


@override_settings(**fixtures.CmsGoalRefundTests._overridden_settings)
class CmsGoalCancellationTests(SimpleTestCase):
    configure = fixtures.CmsGoalRefundTests.configure
    reserve = fixtures.CmsGoalRefundTests.reserve
    hold = fixtures.CmsGoalRefundTests.hold
    prepare_generation = fixtures.CmsGoalRefundTests.prepare_generation
    consume = fixtures.CmsGoalRefundTests.consume
    earn_draft = fixtures.CmsGoalRefundTests.earn_draft

    def setUp(self):
        fixtures.CmsGoalRefundTests.setUp(self)
        self.goal_operation.state = "cancelled"
        self.remote = self.stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_remote_config",
            return_value={"enabled": True, "base_url": "https://cf.synthetic.test"}))
        self.lookup = self.stack.enter_context(patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key",
            return_value=("started", {"run_id": "original-run"})))
        self.post = self.stack.enter_context(patch("integrations.http_client.post", return_value=SimpleNamespace(
            status_code=200, json=lambda: {"status": "cancelled", "run_id": "original-run"})))
        self.unlocked = self.stack.enter_context(patch("content_factory.website_connections.require_unlocked_remote_call"))

    def stop(self): return stop_unused(self.goal_operation, now=self.now)

    def test_original_key_lookup_and_exact_cancel_acknowledgement_stop_work_with_flags_off(self):
        with override_settings(ARTICLES_CMS_GOALS_ENABLED=False, ARTICLES_CMS_CHANNEL_ENABLED=False): proof = self.stop()
        self.assertEqual(cancellation.validate(self.outbox, proof)["run_id"], "original-run")
        self.assertEqual(self.lookup.call_args.args[1], self.key)
        self.assertEqual(self.post.call_args.args[0], "https://cf.synthetic.test/api/runs/original-run/cancel")
        self.assertEqual(proof["status"], "cancelled")
        self.assertNotIn("application-password", repr(proof))
        self.assertEqual(self.outbox.state, "pending")  # Only the fenced coordinator persists effects.

    def test_explicit_no_work_key_observations_need_no_cancel_post(self):
        for outcome in ("not_received", "received_not_started", "rejected"):
            self.lookup.return_value = outcome, {}
            proof = self.stop()
            self.assertEqual((proof["status"], proof["run_id"]), (outcome, None))
        self.post.assert_not_called()

    def test_unknown_dispatch_or_missing_config_stays_pending_without_credit_or_cancel(self):
        for result in (("unknown", {}), ("started", {}), ("started", {"run_id": "../../foreign"})):
            self.lookup.return_value = result
            with self.assertRaises(WebsiteAuthorityError) as error: self.stop()
            self.assertTrue(error.exception.retryable)
        self.post.assert_not_called()
        self.remote.return_value["enabled"] = False
        with self.assertRaises(WebsiteAuthorityError): self.stop()
        self.credit_call.assert_not_called()

    def test_generic_success_absence_wrong_run_and_pending_cleanup_do_not_prove_worker_stopped(self):
        for code, body in ((404, {}), (204, {}), (200, {}), (200, {"status": "running", "run_id": "original-run"}),
                (200, {"status": "cancelled", "run_id": "foreign-run"}),
                (200, {"status": "cancelled", "run_id": "original-run", "cleanup_pending": True})):
            self.post.return_value = SimpleNamespace(status_code=code, json=lambda: body)
            with self.subTest(code=code, body=body), self.assertRaises(WebsiteAuthorityError): self.stop()
        self.credit_call.assert_not_called()

    def test_valid_private_stop_observation_replays_without_remote_requests(self):
        proof = self.stop()
        self.outbox.payload[cancellation.REFERENCE] = proof
        self.lookup.reset_mock(); self.post.reset_mock()
        self.assertEqual(self.stop(), proof)
        self.lookup.assert_not_called(); self.post.assert_not_called()
        self.outbox.payload["request"]["topic"] = "Changed private source"
        with self.assertRaises(WebsiteAuthorityError): self.stop()

    def test_local_cancelled_status_or_worker_boolean_cannot_supply_a_stop_observation(self):
        self.outbox.state = "cancelled"
        self.outbox.payload[cancellation.REFERENCE] = {"cancelled": True}
        with self.assertRaises(WebsiteAuthorityError): self.stop()
        self.lookup.assert_not_called(); self.post.assert_not_called()

    def test_an_accepted_draft_or_active_goal_is_never_cancelled_to_credit_unused_work(self):
        self.earn_draft()
        with self.assertRaises(WebsiteAuthorityError): self.stop()
        self.goal_operation.state = "running"
        with self.assertRaises(WebsiteAuthorityError): self.stop()
        self.lookup.assert_not_called(); self.post.assert_not_called()

    def test_no_original_outbox_means_no_dispatched_work_without_a_provider_read(self):
        with patch.object(ContentFactoryDispatchOutbox.objects, "filter", return_value=SimpleNamespace(first=lambda: None)):
            self.assertIsNone(self.stop())
        self.lookup.assert_not_called(); self.post.assert_not_called()
