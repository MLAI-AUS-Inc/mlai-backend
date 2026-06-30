"""Persist the Roo-points billing link from a ContentFactoryJob onto its ContentFactoryRun.

A paid article's run is synced from content-factory without the web `roo_points_*` fields, but the charge
is recorded on the matching ContentFactoryJob. `_merge_job_billing_into_run_request` mirrors that charge
onto the run's run_request at materialization, so the revision flow can verify the payment.
"""
from types import SimpleNamespace

from django.test import TestCase

from content_factory.billing import get_content_factory_article_cost_points
from content_factory.vibe_marketing_views import (
    _merge_job_billing_into_run_request,
    _persist_web_article_billing_to_job,
)

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


class PersistWebArticleBillingToJobTests(TestCase):
    """The founder-tools web path charges Roo points but only materializes a ContentFactoryRun;
    content-factory then overwrites run_request, stripping the web roo_points_* fields. The charge
    must be stamped onto the durable ContentFactoryJob so the revision flow can verify payment."""

    def _charge_ledger(self, ref):
        from roo.models import Ledger
        return Ledger.objects.create(
            source="CONTENT_FACTORY", kind="SPEND", delta=-6,
            reference_id=ref, idempotency_key=f"content_factory:charge:{ref}",
        )

    def _run(self, run_id):
        from workflow_runs.models import ContentFactoryRun
        return ContentFactoryRun.objects.create(
            run_id=run_id, workflow="article_generation", domain=PAID_DOMAIN,
            slack_user_id="mlai_user:648", status="approval_required", run_request={},
        )

    def _web_payload(self, led, crid="vibe-article-job:web"):
        return {
            "domain": PAID_DOMAIN,
            "client_request_id": crid,
            "roo_points_authorized": True,
            "roo_points_action": "article_generation",
            "roo_points_cost": 6,
            "roo_points_billing_status": "charged",
            "roo_points_ledger_id": str(led.id),
        }

    def test_creates_billing_job_when_callback_run_exists_without_job(self):
        from content_factory.models import ContentFactoryJob
        led = self._charge_ledger("vibe-article-job:web1")
        run = self._run("web-run-1")  # no ContentFactoryJob yet (web path never created one)

        _persist_web_article_billing_to_job(run, self._web_payload(led, "vibe-article-job:web1"))

        job = ContentFactoryJob.objects.get(job_id="web-run-1")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_ledger_id, led.id)
        self.assertEqual(job.client_request_id, "vibe-article-job:web1")
        self.assertEqual(job.billing_amount, 6)

    def test_stamps_existing_callback_created_job(self):
        from content_factory.models import ContentFactoryJob
        led = self._charge_ledger("vibe-article-job:web2")
        # A content-factory callback created the job first, with no billing (the orphan scenario).
        ContentFactoryJob.objects.create(
            job_id="web-run-2", slack_user_id="u", domain=PAID_DOMAIN,
            request_meta={"publish_stage": "article_review_ready"},
        )
        run = self._run("web-run-2")

        _persist_web_article_billing_to_job(run, self._web_payload(led, "vibe-article-job:web2"))

        job = ContentFactoryJob.objects.get(job_id="web-run-2")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_ledger_id, led.id)
        self.assertEqual(job.client_request_id, "vibe-article-job:web2")

    def test_does_not_downgrade_already_charged_job(self):
        from content_factory.models import ContentFactoryJob
        real = self._charge_ledger("vibe-article-job:real")
        ContentFactoryJob.objects.create(
            job_id="web-run-3", slack_user_id="u", domain=PAID_DOMAIN,
            billing_status="charged", billing_amount=6, billing_ledger=real,
            client_request_id="vibe-article-job:real",
        )
        run = self._run("web-run-3")
        other = self._charge_ledger("vibe-article-job:other")

        _persist_web_article_billing_to_job(run, self._web_payload(other, "vibe-article-job:real"))

        job = ContentFactoryJob.objects.get(job_id="web-run-3")
        self.assertEqual(job.billing_ledger_id, real.id)  # untouched

    def test_no_charge_in_payload_is_a_noop(self):
        from content_factory.models import ContentFactoryJob
        run = self._run("web-run-4")
        _persist_web_article_billing_to_job(run, {"domain": PAID_DOMAIN})  # no roo_points_*
        self.assertFalse(ContentFactoryJob.objects.filter(job_id="web-run-4").exists())

    def test_revision_lineage_resolves_after_stamp(self):
        """End-to-end: after stamping, the revision lineage lookup finds the charge."""
        from content_factory.vibe_marketing_views import _reusable_content_factory_charge_for_run
        led = self._charge_ledger("vibe-article-job:web5")
        run = self._run("web-run-5")
        self.assertIsNone(_reusable_content_factory_charge_for_run(run))  # orphaned before

        _persist_web_article_billing_to_job(run, self._web_payload(led, "vibe-article-job:web5"))

        self.assertEqual(_reusable_content_factory_charge_for_run(run), led.id)
