"""Original native outbox recovery never rebuilds or charges a new article."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import cms_goal_dispatch as dispatch
from . import cms_goal_authority as authority
from . import tests_cms_goal_controller_unit as fixtures
from .website_contract import WebsiteAuthorityError


@override_settings(**fixtures.CmsGoalControllerTests._overridden_settings)
class CmsGoalDispatchTests(SimpleTestCase):
    configure = fixtures.CmsGoalControllerTests.configure
    reserve = fixtures.CmsGoalControllerTests.reserve
    hold = fixtures.CmsGoalControllerTests.hold
    prepare_generation = fixtures.CmsGoalControllerTests.prepare_generation
    consume = fixtures.CmsGoalControllerTests.consume
    paid_run = fixtures.CmsGoalControllerTests.paid_run

    def setUp(self):
        fixtures.CmsGoalControllerTests.setUp(self)
        self.goal_operation.payload["generation_data"] = {"topic": "Current approved topic", "target_keyword": "Current approved topic"}
        self.lease = "server-lease"
        self.claim()

    def claim(self):
        self.goal_operation.state = "running"
        self.goal_operation.payload["lease_id"] = self.lease
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=180)

    def advance(self): return dispatch.advance(self.goal_operation, lease_id=self.lease, now=self.now)

    def test_existing_original_run_does_not_call_canonical_start_or_charge_again(self):
        self.paid_run(); self.claim()
        with patch("content_factory.vibe_marketing_views._start_article_generation_for_channel") as start:
            self.assertIs(self.advance(), self.run)
        start.assert_not_called(); self.spend.assert_called_once()

    def test_missing_mirror_recovers_from_original_outbox_without_rebuilding_paid_request(self):
        self.paid_run(); self.claim()
        self.outbox.payload["request"]["analytics_article_id"] = "original-analytics-id"
        # Analytics metadata is immutable in the original artifact identity.
        original = self.outbox.payload["request"]
        artifact = next(row for row in self.saved if row.action == "cms-artifact")
        from .channel_artifacts import _request_identity
        artifact.payload["request_sha256"] = _request_identity(original)
        with patch.object(dispatch, "original_run", return_value=None), \
                patch("content_factory.dispatch_binding.reserve_portable_dispatch_intent", return_value=self.run) as mirror, \
                patch("content_factory.vibe_marketing_views._start_article_generation_for_channel") as start:
            self.assertIs(self.advance(), self.run)
        self.assertEqual(mirror.call_args.kwargs["payload"], original)
        self.assertEqual(mirror.call_args.kwargs["payload"]["analytics_article_id"], "original-analytics-id")
        start.assert_not_called(); self.spend.assert_called_once()

    def test_first_start_uses_only_server_typed_hold_and_current_bounded_goal_budget(self):
        def canonical(**kwargs):
            self.assertIsInstance(kwargs["cms_goal_scope"], authority.CmsGoalDispatch)
            self.assertEqual(kwargs["cms_goal_scope"].lease_id, self.lease)
            self.assertEqual(kwargs["data"]["delivery_mode"], "content_only")
            self.assertEqual(kwargs["website_contract"]["goal_budget"]["goal_id"], str(self.goal_operation.pk))
            self.paid_run(); self.claim()
            return SimpleNamespace(status_code=202, data={"run_id": self.run.run_id})
        with patch("content_factory.vibe_marketing_views._start_article_generation_for_channel", side_effect=canonical) as start:
            self.assertIs(self.advance(), self.run)
            self.assertIs(self.advance(), self.run)
        start.assert_called_once(); self.spend.assert_called_once()

    def test_lost_error_reply_after_original_transaction_uses_same_committed_outbox_on_next_lease(self):
        def uncertain(**kwargs):
            self.paid_run(); self.claim()
            return SimpleNamespace(status_code=503, data={"code": "website_prepare_dependency_transient"})
        with patch("content_factory.vibe_marketing_views._start_article_generation_for_channel", side_effect=uncertain) as start:
            self.assertIs(self.advance(), self.run)
            self.assertIs(self.advance(), self.run)
        start.assert_called_once(); self.spend.assert_called_once()

    def test_stale_lease_or_foreign_original_goal_metadata_cannot_resume_a_saved_outbox(self):
        self.paid_run(); self.claim()
        with self.assertRaises(WebsiteAuthorityError): dispatch.advance(self.goal_operation, lease_id="old-lease", now=self.now)
        self.outbox.payload["request"]["cms_goal_id"] = "foreign"
        with self.assertRaises(WebsiteAuthorityError): self.advance()

    def test_missing_budget_or_changed_original_price_record_cannot_create_new_work(self):
        self.goal_operation.receipt.pop("native_budget")
        with patch("content_factory.vibe_marketing_views._start_article_generation_for_channel") as start, self.assertRaises(WebsiteAuthorityError):
            self.advance()
        start.assert_not_called(); self.spend.assert_called_once()
