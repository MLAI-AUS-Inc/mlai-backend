"""Roo-points authorization reuse for article revision/restart.

A paid article's run frequently has no web `roo_points_*` stamp: the article-generation service records
the charge on the ContentFactoryJob / Roo Ledger (keyed by client_request_id), and content-factory syncs
the run back without the billing fields. The revision/restart flow must REUSE that real charge (found via
the run's billing lineage) rather than rejecting it — without ever inventing authorization.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase
from unittest.mock import patch

from content_factory import vibe_marketing_views as views
from content_factory.billing import get_content_factory_article_cost_points
from content_factory.vibe_marketing_views import (
    _reusable_content_factory_charge_for_run,
    _reuse_roo_points_authorization_for_article_job,
)

PAID_DOMAIN = "vyavos.com"   # not in FREE_CONTENT_FACTORY_DOMAINS
MLAI_DOMAIN = "mlai.au"


def _run(run_request: dict):
    return SimpleNamespace(run_id="run-abc", run_request=run_request)


def _valid_web_auth():
    return {
        "roo_points_authorized": True,
        "roo_points_action": "article_generation",
        "roo_points_cost": get_content_factory_article_cost_points(PAID_DOMAIN),
        "roo_points_billing_status": "charged",
        "roo_points_ledger_id": "777",
    }


class ReuseAuthorizationDecisionTests(SimpleTestCase):
    """Verifier decision logic (lineage lookup mocked — no DB)."""

    def test_verified_web_charge_reuses_without_a_lineage_walk(self):
        payload: dict = {}
        charge = SimpleNamespace(reference_id='web-article-original')
        with patch('roo.models.Ledger.objects.filter') as ledger, \
             patch.object(views, "_reusable_content_factory_charge_for_run") as lineage:
            ledger.side_effect = [SimpleNamespace(first=lambda: charge), SimpleNamespace(exists=lambda: False)]
            result = _reuse_roo_points_authorization_for_article_job(
                run=_run(_valid_web_auth()), payload=payload, domain=PAID_DOMAIN, failure_detail="x",
            )
            self.assertEqual(ledger.call_args_list[0].kwargs, {'pk': 777, 'kind': 'SPEND', 'source': 'CONTENT_FACTORY'})
            self.assertEqual(ledger.call_args_list[1].kwargs,
                {'kind': 'REFUND', 'source': 'CONTENT_FACTORY', 'reference_id': charge.reference_id})
        self.assertIsNone(result)
        lineage.assert_not_called()  # Verified charge -> no lineage walk needed.
        self.assertEqual(payload["roo_points_billing_status"], "reused")
        self.assertEqual(payload["roo_points_ledger_id"], "777")

    def test_stamp_without_a_real_charge_cannot_authorize_reuse(self):
        payload = {}
        with patch('roo.models.Ledger.objects.filter') as ledger, \
             patch.object(views, '_reusable_content_factory_charge_for_run', return_value=None):
            ledger.return_value.first.return_value = None
            result = _reuse_roo_points_authorization_for_article_job(run=_run(_valid_web_auth()),
                payload=payload, domain=PAID_DOMAIN, failure_detail='No payment')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data['code'], 'roo_points_billing_required')
        self.assertEqual(payload, {})

    def test_unstamped_run_reuses_real_lineage_charge(self):
        payload: dict = {}
        with patch.object(views, "_reusable_content_factory_charge_for_run", return_value=2127), \
             patch("roo.models.Ledger.objects.get", return_value=SimpleNamespace(delta_microroo=-6_000_000)):
            result = _reuse_roo_points_authorization_for_article_job(
                run=_run({"domain": PAID_DOMAIN}), payload=payload, domain=PAID_DOMAIN, failure_detail="x",
            )
        self.assertIsNone(result)  # reused from the lineage charge
        self.assertEqual(payload["roo_points_billing_status"], "reused")
        self.assertEqual(payload["roo_points_ledger_id"], "2127")
        self.assertEqual(payload["roo_points_cost"], 6)
        self.assertEqual(payload["roo_points_required"], 6)

    def test_unstamped_run_without_any_charge_is_blocked(self):
        payload: dict = {}
        with patch.object(views, "_reusable_content_factory_charge_for_run", return_value=None):
            result = _reuse_roo_points_authorization_for_article_job(
                run=_run({"domain": PAID_DOMAIN}), payload=payload, domain=PAID_DOMAIN, failure_detail="blocked",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["code"], "roo_points_billing_required")
        self.assertEqual(result.data["detail"], "blocked")

    def test_mlai_article_without_a_charge_is_blocked(self):
        payload: dict = {}
        with patch.object(views, "_reusable_content_factory_charge_for_run", return_value=None):
            result = _reuse_roo_points_authorization_for_article_job(
                run=_run({}), payload=payload, domain=MLAI_DOMAIN, failure_detail="Article payment required",
            )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["code"], "roo_points_billing_required")
        self.assertEqual(payload, {})


class ReusableChargeLineageTests(TestCase):
    """The lineage lookup itself, against real ContentFactoryJob / Ledger rows."""

    def _make_charge(self, reference_id):
        from roo.models import Ledger
        return Ledger.objects.create(
            source="CONTENT_FACTORY", kind="SPEND", delta=-6,
            reference_id=reference_id, idempotency_key=f"content_factory:charge:{reference_id}",
        )

    def test_finds_charge_via_job_billing(self):
        from content_factory.models import ContentFactoryJob
        led = self._make_charge("vibe-article-job:abc")
        ContentFactoryJob.objects.create(
            job_id="run-job-1", slack_user_id="u", domain=PAID_DOMAIN,
            billing_status="charged", billing_ledger=led,
        )
        run = SimpleNamespace(run_id="run-job-1", run_request={"domain": PAID_DOMAIN})
        self.assertEqual(_reusable_content_factory_charge_for_run(run), led.id)

    def test_finds_charge_via_ledger_client_request_id(self):
        # Job has no billing, but the run carries the client_request_id of a real charge.
        from content_factory.models import ContentFactoryJob
        led = self._make_charge("vibe-article-job:xyz")
        ContentFactoryJob.objects.create(job_id="run-job-2", slack_user_id="u", domain=PAID_DOMAIN)
        run = SimpleNamespace(run_id="run-job-2", run_request={"domain": PAID_DOMAIN, "client_request_id": "vibe-article-job:xyz"})
        self.assertEqual(_reusable_content_factory_charge_for_run(run), led.id)

    def test_refunded_charge_is_not_reusable(self):
        from roo.models import Ledger
        self._make_charge("vibe-article-job:ref")
        Ledger.objects.create(
            source="CONTENT_FACTORY", kind="REFUND", delta=6,
            reference_id="vibe-article-job:ref", idempotency_key="content_factory:refund:vibe-article-job:ref",
        )
        run = SimpleNamespace(run_id="missing", run_request={"client_request_id": "vibe-article-job:ref"})
        self.assertIsNone(_reusable_content_factory_charge_for_run(run))

    def test_no_charge_anywhere_returns_none(self):
        run = SimpleNamespace(run_id="nope", run_request={"client_request_id": "vibe-article-job:none"})
        self.assertIsNone(_reusable_content_factory_charge_for_run(run))
