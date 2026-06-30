"""Persist the Roo-points billing link from a ContentFactoryJob onto its ContentFactoryRun.

A paid article's run is synced from content-factory without the web `roo_points_*` fields, but the charge
is recorded on the matching ContentFactoryJob. `_merge_job_billing_into_run_request` mirrors that charge
onto the run's run_request at materialization, so the revision flow can verify the payment.
"""
from types import SimpleNamespace

from django.test import TestCase

from content_factory.billing import get_content_factory_article_cost_points
from content_factory.vibe_marketing_views import _merge_job_billing_into_run_request

PAID_DOMAIN = "vyavos.com"


class MergeJobBillingTests(TestCase):
    def _charge_ledger(self, ref):
        from roo.models import Ledger
        return Ledger.objects.create(
            source="CONTENT_FACTORY", kind="SPEND", delta=-6,
            reference_id=ref, idempotency_key=f"content_factory:charge:{ref}",
        )

    def _run(self, run_id, run_request):
        from workflow_runs.models import ContentFactoryRun
        return ContentFactoryRun.objects.create(
            run_id=run_id, workflow="article_generation", domain=PAID_DOMAIN, run_request=run_request,
        )

    def test_mirrors_charged_job_billing_onto_run(self):
        from content_factory.models import ContentFactoryJob
        led = self._charge_ledger("vibe-article-job:abc")
        ContentFactoryJob.objects.create(
            job_id="run-1", slack_user_id="u", domain=PAID_DOMAIN,
            billing_status="charged", billing_amount=6, billing_ledger=led,
            client_request_id="vibe-article-job:abc",
        )
        run = self._run("run-1", {"domain": PAID_DOMAIN, "topic": "X"})

        _merge_job_billing_into_run_request(run)
        run.refresh_from_db()
        rr = run.run_request
        self.assertTrue(rr["roo_points_authorized"])
        self.assertEqual(rr["roo_points_billing_status"], "charged")
        self.assertEqual(str(rr["roo_points_ledger_id"]), str(led.id))
        self.assertEqual(rr["roo_points_cost"], get_content_factory_article_cost_points(PAID_DOMAIN))
        self.assertEqual(rr["client_request_id"], "vibe-article-job:abc")
        self.assertEqual(rr["topic"], "X")  # existing run_request data preserved

    def test_does_not_overwrite_existing_authorization(self):
        from content_factory.models import ContentFactoryJob
        led = self._charge_ledger("vibe-article-job:def")
        ContentFactoryJob.objects.create(
            job_id="run-2", slack_user_id="u", domain=PAID_DOMAIN,
            billing_status="charged", billing_amount=6, billing_ledger=led,
        )
        run = self._run("run-2", {"roo_points_authorized": True, "roo_points_ledger_id": "999"})

        _merge_job_billing_into_run_request(run)
        run.refresh_from_db()
        self.assertEqual(run.run_request["roo_points_ledger_id"], "999")  # untouched

    def test_no_job_billing_leaves_run_unchanged(self):
        from content_factory.models import ContentFactoryJob
        ContentFactoryJob.objects.create(job_id="run-3", slack_user_id="u", domain=PAID_DOMAIN)  # no billing
        run = self._run("run-3", {"domain": PAID_DOMAIN})

        _merge_job_billing_into_run_request(run)
        run.refresh_from_db()
        self.assertNotIn("roo_points_authorized", run.run_request)

    def test_missing_job_is_a_noop(self):
        run = self._run("run-4", {"domain": PAID_DOMAIN})
        _merge_job_billing_into_run_request(run)  # no job exists
        run.refresh_from_db()
        self.assertNotIn("roo_points_authorized", run.run_request)
