"""Roo-points authorization reuse for article revision/restart.

Articles initiated by the Roo Slack bot are billed through the bot's own flow and never carry the web
`roo_points_*` authorization fields on the run, so the web revision/restart flow must not block them on
a reuse check the bot path never populates. Web-initiated runs still require their stamped authorization.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from content_factory.billing import get_content_factory_article_cost_points
from content_factory.vibe_marketing_views import _reuse_roo_points_authorization_for_article_job

PAID_DOMAIN = "vyavos.com"   # not in FREE_CONTENT_FACTORY_DOMAINS
FREE_DOMAIN = "mlai.au"


def _run(run_request: dict):
    return SimpleNamespace(run_id="run-abc", run_request=run_request)


class ReuseRooPointsAuthorizationTests(SimpleTestCase):
    def test_slackbot_run_is_authorized_without_web_billing_fields(self):
        # The diagnosed case: a roo_slackbot article on a paid domain with NO roo_points_* fields.
        payload: dict = {}
        result = _reuse_roo_points_authorization_for_article_job(
            run=_run({"request_source": "roo_slackbot", "domain": PAID_DOMAIN}),
            payload=payload,
            domain=PAID_DOMAIN,
            failure_detail="nope",
        )
        self.assertIsNone(result)  # allowed
        self.assertTrue(payload["roo_points_authorized"])
        self.assertEqual(payload["roo_points_billing_status"], "reused")
        self.assertEqual(payload["roo_points_billing_source"], "roo_slackbot")
        self.assertEqual(payload["original_billing_source_run_id"], "run-abc")
        self.assertEqual(payload["roo_points_cost"], get_content_factory_article_cost_points(PAID_DOMAIN))

    def test_web_run_without_billing_fields_is_still_blocked(self):
        # A web-initiated run must still carry its stamped authorization — no free pass.
        payload: dict = {}
        result = _reuse_roo_points_authorization_for_article_job(
            run=_run({"request_source": "vibe_web", "domain": PAID_DOMAIN}),
            payload=payload,
            domain=PAID_DOMAIN,
            failure_detail="blocked",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["code"], "roo_points_billing_required")
        self.assertEqual(result.data["detail"], "blocked")

    def test_web_run_with_valid_authorization_is_reused(self):
        # Existing happy path: a properly-stamped web run reuses its authorization.
        payload: dict = {}
        result = _reuse_roo_points_authorization_for_article_job(
            run=_run({
                "roo_points_authorized": True,
                "roo_points_action": "article_generation",
                "roo_points_cost": get_content_factory_article_cost_points(PAID_DOMAIN),
                "roo_points_billing_status": "charged",
                "roo_points_ledger_id": "ledger-123",
            }),
            payload=payload,
            domain=PAID_DOMAIN,
            failure_detail="nope",
        )
        self.assertIsNone(result)
        self.assertEqual(payload["roo_points_billing_status"], "reused")
        self.assertEqual(payload["roo_points_ledger_id"], "ledger-123")

    def test_free_domain_is_authorized(self):
        payload: dict = {}
        result = _reuse_roo_points_authorization_for_article_job(
            run=_run({}), payload=payload, domain=FREE_DOMAIN, failure_detail="nope",
        )
        self.assertIsNone(result)
        self.assertEqual(payload["roo_points_billing_status"], "free")
