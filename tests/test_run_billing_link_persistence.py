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
    _persist_run_billing_to_job,
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


class PersistRunBillingToJobTests(TestCase):
    """The inverse: persist a web run's run_request charge onto its (sync-proof) ContentFactoryJob.

    The web vibe-marketing flow stamps the charge on run_request only; a later content-factory sync
    strips those fields. Mirroring onto the job makes the payment link survive so the revision verifier
    (`_reusable_content_factory_charge_for_run`) can reuse it.
    """

    def _charge_ledger(self, ref, delta=-6):
        from roo.models import Ledger
        return Ledger.objects.create(
            source="CONTENT_FACTORY", kind="SPEND", delta=delta,
            reference_id=ref, idempotency_key=f"content_factory:charge:{ref}",
        )

    def _run(self, run_id, run_request, slack_user_id="actor-1"):
        from workflow_runs.models import ContentFactoryRun
        return ContentFactoryRun.objects.create(
            run_id=run_id, workflow="article_generation", domain=PAID_DOMAIN,
            slack_user_id=slack_user_id, run_request=run_request,
        )

    def _charged_run_request(self, ledger_id, crid="vibe-article:1:abc"):
        return {
            "domain": PAID_DOMAIN,
            "client_request_id": crid,
            "roo_points_authorized": True,
            "roo_points_action": "article_generation",
            "roo_points_cost": 6,
            "roo_points_billing_status": "charged",
            "roo_points_ledger_id": str(ledger_id),
        }

    def test_creates_job_and_stamps_billing_when_none_exists(self):
        from content_factory.models import ContentFactoryJob
        led = self._charge_ledger("vibe-article:1:abc")
        run = self._run("run-w1", self._charged_run_request(led.id))

        _persist_run_billing_to_job(run)

        job = ContentFactoryJob.objects.get(job_id="run-w1")  # created lazily
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_ledger_id, led.id)
        self.assertEqual(job.billing_amount, 6)
        self.assertEqual(job.client_request_id, "vibe-article:1:abc")
        self.assertEqual(job.slack_user_id, "actor-1")
        self.assertEqual(job.domain, PAID_DOMAIN)

    def test_stamps_existing_unbilled_job(self):
        from content_factory.models import ContentFactoryJob
        led = self._charge_ledger("vibe-article:1:def")
        ContentFactoryJob.objects.create(job_id="run-w2", slack_user_id="u", domain=PAID_DOMAIN)
        run = self._run("run-w2", self._charged_run_request(led.id, crid="vibe-article:1:def"))

        _persist_run_billing_to_job(run)

        job = ContentFactoryJob.objects.get(job_id="run-w2")
        self.assertEqual(job.billing_status, "charged")
        self.assertEqual(job.billing_ledger_id, led.id)

    def test_does_not_overwrite_existing_job_charge(self):
        from content_factory.models import ContentFactoryJob
        original = self._charge_ledger("vibe-article:1:orig")
        newer = self._charge_ledger("vibe-article:1:new")
        ContentFactoryJob.objects.create(
            job_id="run-w3", slack_user_id="u", domain=PAID_DOMAIN,
            billing_status="charged", billing_amount=6, billing_ledger=original,
        )
        run = self._run("run-w3", self._charged_run_request(newer.id))

        _persist_run_billing_to_job(run)

        job = ContentFactoryJob.objects.get(job_id="run-w3")
        self.assertEqual(job.billing_ledger_id, original.id)  # untouched

    def test_unpaid_run_does_not_create_a_job(self):
        from content_factory.models import ContentFactoryJob
        run = self._run("run-w4", {"domain": PAID_DOMAIN, "topic": "X"})  # no roo_points_*
        _persist_run_billing_to_job(run)
        self.assertFalse(ContentFactoryJob.objects.filter(job_id="run-w4").exists())

    def test_free_or_gated_status_is_not_persisted(self):
        from content_factory.models import ContentFactoryJob
        rr = self._charged_run_request(0)
        rr["roo_points_billing_status"] = "free"
        rr["roo_points_ledger_id"] = ""
        run = self._run("run-w5", rr)
        _persist_run_billing_to_job(run)
        self.assertFalse(ContentFactoryJob.objects.filter(job_id="run-w5").exists())

    def test_round_trips_for_revision_verifier(self):
        """End-to-end: web charge -> job -> verifier reuses it (the orphaned-charge fix)."""
        from workflow_runs.models import ContentFactoryRun
        from content_factory.vibe_marketing_views import (
            _reusable_content_factory_charge_for_run,
            _reuse_roo_points_authorization_for_article_job,
        )
        led = self._charge_ledger("vibe-article:1:e2e")
        run = self._run("run-w6", self._charged_run_request(led.id, crid="vibe-article:1:e2e"))
        _persist_run_billing_to_job(run)

        # Simulate the content-factory sync that strips run_request's web roo_points_* fields.
        synced = ContentFactoryRun.objects.get(run_id="run-w6")
        synced.run_request = {"domain": PAID_DOMAIN, "topic": "X"}
        synced.save(update_fields=["run_request"])

        self.assertEqual(_reusable_content_factory_charge_for_run(synced), led.id)
        self.assertIsNone(
            _reuse_roo_points_authorization_for_article_job(
                run=synced, payload={}, domain=PAID_DOMAIN, failure_detail="X",
            )
        )
