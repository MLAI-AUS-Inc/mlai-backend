"""Private reporting canaries over synthetic provider fixtures and real ORM/API paths."""
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from founder_tools.models import VibeRaisingProfile, VibeRaisingCompany
from integrations.models import ExternalServiceConnection, ExternalFinancialRecord
from integrations.tests_startup_updates import StartupUpdateApiTestCase
from startup_updates.models import (
    StartupProfile, StartupMetricObservation, StartupManualDocument, StartupEvent,
    MonthlyUpdateDraft, UserStartupBinding, GmailMessageArtifact, GmailThreadArtifact,
)
from startup_updates.revisions import capture_snapshot, save_revision, approve_and_publish, frozen_memo, RevisionConflict
from startup_updates.source_evidence import stage_source, complete_source
from startup_updates.evidence_contract import reporting_period


class ReportingCohortCanaries(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="canary@example.invalid", password="test-only")
        self.founder = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.now = datetime(2026, 9, 10, 10, tzinfo=timezone.utc)

    def startup(self, name, sources, *, month=date(2026, 8, 1), domain=None, currency="EUR"):
        org = Organization.objects.create(name=name, domain=domain or f"{name}.invalid")
        StartupProfile.objects.create(organization=org, default_currency=currency, reporting_timezone="Europe/Berlin")
        company = VibeRaisingCompany.objects.create(profile=self.founder, organization=org, name=name, domain=domain, location="Berlin")
        run = ContentFactoryRun.objects.create(run_id=name, organization=org, workflow="startup_monthly_update", domain=org.domain,
            run_request={"input_sources": sources, "draft_months": [month.isoformat()], "source_evidence_refreshed": {"stripe_complete": "stripe" in sources}})
        return org, company, run

    def metric(self, org, provider, amount, *, key="revenue", month=date(2026, 8, 1), currency="EUR"):
        metadata = {"source_metric": "xero_profit_and_loss_revenue", "report_hash": "authoritative", "report_start_date": month.isoformat(), "report_end_date": "2026-08-31", "accounting_basis": "accrual"} if provider == "xero" else {"definition_version": 2, "basis": "paid_stripe_invoice_sales_excluding_tax"}
        if provider == "xero":
            import calendar
            from integrations.tests_connectors import _xero_profit_and_loss_report
            from startup_updates.evidence_contract import content_hash
            payload = _xero_profit_and_loss_report(total_income=str(amount or 0), total_expenses=str(amount or 0) if key == "monthlyCosts" else "0", net_profit=str(amount or 0))
            metadata.update(report_payload=payload, report_hash=content_hash(payload), report_end_date=date(month.year, month.month, calendar.monthrange(month.year, month.month)[1]).isoformat(),
                source_metric={"revenue": "xero_profit_and_loss_revenue", "monthlyCosts": "xero_profit_and_loss_monthly_costs", "netProfitLoss": "xero_profit_and_loss_net"}.get(key))
        return StartupMetricObservation.objects.create(organization=org, metric_key=key, metric_name=key, period_month=month,
            value_number=amount, value_text=f"{currency} {amount}" if amount is not None else "", unit=currency, source_provider=provider, source_metadata=metadata)

    def capture(self, org, run):
        month = date.fromisoformat(run.run_request["draft_months"][0])
        with patch("startup_updates.revisions.reporting_period", side_effect=lambda month, zone, **kwargs: reporting_period(month, zone, as_of=kwargs.get("as_of", self.now))):
            return capture_snapshot(org, month, run=run)

    def test_delayed_snapshot_keeps_original_source_cutoff_and_timezone(self):
        org, _, run = self.startup("delayed", ["manual_documents"], month=date(2026, 9, 1))
        run.run_request.update(reporting_timezone="Australia/Melbourne", backfill_window_end="2026-09-03T12:00:00+10:00")
        self.now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        snapshot = self.capture(org, run)
        self.assertTrue(snapshot.payload["period"]["is_partial"])
        self.assertEqual(snapshot.payload["period"]["cutoff"], "2026-09-03T12:00:00.000001+10:00")
        self.assertEqual(snapshot.payload["period"]["timezone"], "Australia/Melbourne")
        edited = capture_snapshot(org, snapshot.month, base_snapshot=snapshot, manual_metrics={"revenue": "12"})
        self.assertEqual(edited.payload["period"], snapshot.payload["period"])
        run.run_request.update(draft_months=["2026-08-01"], backfill_window_end="2026-08-31T23:59:59.999999+10:00")
        self.assertFalse(self.capture(org, run).payload["period"]["is_partial"])

    def test_invalid_source_cutoff_is_rejected(self):
        from rest_framework.exceptions import ValidationError
        org, _, run = self.startup("invalid-cutoff", ["manual_documents"])
        for value in ("invalid", "2026-09-03T12:00:00"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                run.run_request["backfill_window_end"] = value
                self.capture(org, run)

    def test_stripe_only_and_xero_plus_stripe_never_add_sources(self):
        for name, sources, expected in [("stripe-only", ["stripe"], "100.0000"), ("combined", ["xero", "stripe"], "250.0000")]:
            with self.subTest(name=name):
                org, _, run = self.startup(name, sources)
                self.metric(org, "financial", 100)
                if "xero" in sources:
                    self.metric(org, "xero", 250)
                snapshot = self.capture(org, run)
                revenue = next(item for item in snapshot.payload["metrics"] if item["key"] == "revenue")
                self.assertEqual(Decimal(revenue["value"]), Decimal(expected))
                self.assertEqual(snapshot.payload["charts"]["performance"][-1]["income"], float(expected))
                draft = MonthlyUpdateDraft.objects.create(organization=org, month=snapshot.month)
                revision = save_revision(draft, {"highlights": ["Revenue was {{metric:revenue}}."]}, snapshot=snapshot)
                self.assertIn(revenue["display_value"], revision.structured_memo["highlights"][0])
                self.assertEqual(revision.structured_memo["kpi_snapshot"][0]["snapshot_id"], snapshot.pk)

    def test_mtd_balance_sheet_for_month_end_cannot_supply_runway(self):
        from integrations.tests_connectors import _xero_profit_and_loss_report, _xero_balance_sheet_report
        from startup_updates.services import publish_xero_metric_observations
        from integrations.services.xero_scopes import XERO_REPORT_SCOPE
        org, _, run = self.startup("balance-cutoff", ["xero"], month=date(2026, 9, 1))
        ExternalServiceConnection.objects.create(organization=org, user=self.user, provider="xero", scopes=[XERO_REPORT_SCOPE])
        def report(connection, name, *, params):
            if name == "BalanceSheet":
                return _xero_balance_sheet_report(total_bank="9000", as_of="30 Sep 2026")
            return _xero_profit_and_loss_report(total_income="100", total_expenses="200", net_profit="-100")
        with patch("integrations.services.external_connectors.fetch_xero_base_currency", return_value="EUR"), patch("integrations.services.external_connectors.fetch_xero_accounting_report", side_effect=report):
            result = publish_xero_metric_observations(organization=org, run=run, start_date=date(2026,9,1), end_date=date(2026,9,10))
        self.assertFalse(StartupMetricObservation.objects.filter(organization=org, metric_key="runway").exists())
        self.assertTrue(StartupMetricObservation.objects.filter(organization=org, metric_key="revenue", period_month=date(2026,9,1), value_number=100).exists())
        self.assertTrue(any("does not match the source cutoff" in warning for warning in result["warnings"]))

    def test_domainless_narrative_startup_keeps_uploaded_only_evidence(self):
        org, company, run = self.startup("domainless", ["manual_documents"])
        text = "Background. " * 4000 + "On 23 August 2026, we shipped our first offline release."
        document = StartupManualDocument.objects.create(organization=org, company=company, created_by=self.user,
            original_filename="progress.txt", storage_path="synthetic/progress.txt", extracted_text=text, extraction_status="processed")
        run.run_request["manual_document_ids"] = [str(document.pk)]
        run.save()
        snapshot = self.capture(org, run)
        self.assertEqual(snapshot.payload["manual_sources"]["documents"][0]["text"], text)
        self.assertIsNone(snapshot.payload["charts"])
        self.assertTrue(all(item["value"] is None for item in snapshot.payload["metrics"]))
        self.assertEqual(snapshot.payload["startup"]["name"], "domainless")
        self.assertEqual(snapshot.payload["period"]["timezone"], "Europe/Berlin")
        self.assertIsNone(company.domain)
        document.extracted_text = "Edited in September: the release was cancelled."
        document.save()
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.payload["manual_sources"]["documents"][0]["text"], text)
        self.assertNotEqual(self.capture(org, run).content_hash, snapshot.content_hash)

    def test_september_partial_unknown_and_true_zero_remain_distinct(self):
        org, _, run = self.startup("september", ["xero"], month=date(2026, 9, 1))
        self.metric(org, "xero", 0, key="monthlyCosts", month=date(2026, 9, 1))
        snapshot = self.capture(org, run)
        metrics = {item["key"]: item for item in snapshot.payload["metrics"]}
        self.assertTrue(snapshot.payload["period"]["is_partial"])
        self.assertIsNone(metrics["revenue"]["value"])
        self.assertEqual(Decimal(metrics["monthlyCosts"]["value"]), 0)
        self.assertIsNone(snapshot.payload["charts"]["performance"][-1]["income"])
        self.assertEqual(snapshot.payload["charts"]["performance"][-1]["expenses"], 0)

    def test_chart_history_omits_legacy_costs_and_freezes_report_evidence(self):
        from startup_updates.services import build_monthly_financial_snapshot
        from startup_updates.evidence_contract import content_hash
        org, _, run = self.startup("chart-history", ["xero"])
        old = self.metric(org, "xero", 40, key="monthlyCosts", month=date(2026,7,1))
        old.source_metadata = {}
        old.save()
        self.metric(org, "xero", 100)
        snapshot = self.capture(org, run)
        history = snapshot.payload["charts"]
        july = next(point for point in history["performance"] if point["month"] == "2026-07-01")
        self.assertIsNone(july["expenses"])
        august = history["performance"][-1]
        report_hash = august["metric_evidence"]["income"]["report_hash"]
        self.assertEqual(content_hash(history["source_reports"][report_hash]), report_hash)
        # Later mutations cannot change the frozen report or its chart value.
        metric = StartupMetricObservation.objects.get(pk=august["metric_evidence"]["income"]["observation_id"])
        metric.source_metadata = {}
        metric.value_number = 999
        metric.save()
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.payload["charts"]["performance"][-1]["income"], 100)
        self.assertEqual(content_hash(snapshot.payload["charts"]["source_reports"][report_hash]), report_hash)
        self.assertIsNone(build_monthly_financial_snapshot(organization=org, target_month=date(2026,8,1)))

    def test_source_reuse_and_exact_revision_approval_keep_old_publication(self):
        org, _, run = self.startup("revision", ["manual_documents"])
        source = stage_source(run, "notion", "page", {"cleaned_text": "February pilot signed."})
        complete_source(run, "notion", "page", {"source_fingerprint": source["source_fingerprint"], "events": []})
        run.save()
        later_run = ContentFactoryRun.objects.create(run_id="revision-retry", organization=org, workflow=run.workflow, domain=org.domain, run_request=run.run_request)
        cached = stage_source(later_run, "notion", "page", {"cleaned_text": "February pilot signed."})
        self.assertEqual(cached["cached_extraction"]["events"], [])
        snapshot = self.capture(org, run)
        draft = MonthlyUpdateDraft.objects.create(organization=org, month=snapshot.month)
        revision_a = save_revision(draft, {"highlights": ["Revision A."]}, snapshot=snapshot)
        approve_and_publish(draft, actor=self.user, revision_id=revision_a.pk, revision_hash=revision_a.content_hash, audience_visibility=["just_me"])
        revision_b = save_revision(draft, {"highlights": ["Revision B."]}, snapshot=snapshot, expected_revision=revision_a.pk)
        with self.assertRaises(RevisionConflict):
            approve_and_publish(draft, actor=self.user, revision_id=revision_a.pk, revision_hash=revision_a.content_hash, audience_visibility=["just_me"])
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision_id, revision_b.pk)
        self.assertEqual(frozen_memo(draft, published=True)["highlights"], ["Revision A."])


    def test_worker_checkpoint_cannot_erase_evidence_or_change_founder_inputs(self):
        from content_factory.service_views import _sync_content_factory_run_snapshot
        _, _, run = self.startup("checkpoint", ["manual_documents"])
        run.run_request["evidence_snapshots"] = {"2026-08-01": {"snapshot_id": 123, "expected_revision": 45}}
        run.result = {"source_evidence": {"notion:page": {"fingerprint": "pinned", "output": {"events": []}}},
            "update_candidates": [{"event_id": 1, "founder_status": "approved"}]}
        run.save()
        original_request, original_evidence = run.run_request, run.result["source_evidence"]
        _sync_content_factory_run_snapshot(run_id=run.run_id, data={"workflow": run.workflow, "status": "running", "domain": run.domain,
            "run_request": {"input_sources": ["gmail"]}, "result": {"source_evidence": {}, "pending_drafts": [{"month": "2026-08-01"}]}}, step_states={})
        run.refresh_from_db()
        self.assertEqual(run.run_request, original_request)
        self.assertEqual(run.result["source_evidence"], original_evidence)
        self.assertEqual(run.result["update_candidates"][0]["founder_status"], "approved")
        self.assertEqual(run.result["pending_drafts"][0]["month"], "2026-08-01")

    def test_currency_mismatch_stays_unknown_and_failed_review_survives_edit(self):
        from rest_framework.exceptions import ValidationError
        org, _, run = self.startup("mismatch", ["xero"])
        self.metric(org, "xero", 500, currency="AUD")
        snapshot = self.capture(org, run)
        revenue = next(item for item in snapshot.payload["metrics"] if item["key"] == "revenue")
        self.assertIsNone(revenue["value"])
        draft = MonthlyUpdateDraft.objects.create(organization=org, month=snapshot.month)
        revision = save_revision(draft, {"highlights": ["Unsupported claim."]}, snapshot=snapshot)
        revision.validation = {"groundedness_status": "needs_review", "groundedness_notes": "Source missing."}
        type(revision).objects.filter(pk=revision.pk).update(validation=revision.validation)
        edited = save_revision(draft, {"highlights": ["Still unsupported."]}, snapshot=snapshot, expected_revision=revision.pk)
        with self.assertRaises(ValidationError):
            approve_and_publish(draft, actor=self.user, revision_id=edited.pk, revision_hash=edited.content_hash, audience_visibility=["just_me"])


class GmailWindowCanaries(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        StartupProfile.objects.create(organization=self.organization)
        self.binding = UserStartupBinding.objects.create(user=self.user, organization=self.organization, google_connection=self.google_connection)
        self.run = ContentFactoryRun.objects.create(run_id="february-canary", organization=self.organization, domain=self.organization.domain, workflow="startup_monthly_update",
            run_request={"organization_id": self.organization.pk, "binding_id": self.binding.pk, "google_connection_id": self.google_connection.pk,
                "input_sources": ["gmail"], "draft_months": ["2026-02-01"], "backfill_window_start": "2026-02-01T00:00:00+00:00", "backfill_window_end": "2026-02-28T23:59:59+00:00"})

    def test_all_forty_five_threads_remain_eligible_after_later_reply(self):
        from startup_updates.api_views import _get_prioritized_run_thread_ids
        for index in range(45):
            GmailMessageArtifact.objects.create(organization=self.organization, google_connection=self.google_connection,
                gmail_message_id=f"feb-{index}", gmail_thread_id=f"thread-{index}", internal_date=datetime(2026, 2, 12, tzinfo=timezone.utc), relevance_label="relevant", needs_thread_context=True)
        ids = _get_prioritized_run_thread_ids(run=self.run, organization=self.organization, google_connection=self.google_connection)
        self.assertEqual(len(ids), 45)
        GmailThreadArtifact.objects.create(organization=self.organization, google_connection=self.google_connection,
            gmail_thread_id="thread-44", hydration_status="hydrated", extraction_status="processed", latest_message_internal_date=datetime(2026, 9, 5, tzinfo=timezone.utc),
            message_payloads=[{"message_id": "feb-44", "internal_date": "2026-02-12T00:00:00+00:00", "cleaned_text": "The February launch shipped."},
                {"message_id": "sep-reply", "internal_date": "2026-09-05T00:00:00+00:00", "cleaned_text": "September-only claim."}])
        with self._with_key(), patch("startup_updates.api_views.ensure_thread_attachments_hydrated", return_value=[]):
            response = self.client.get(reverse("startup_updates_extraction_batch", args=[self.run.run_id]), **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertNotIn("September-only", response.data["threads"][0]["cleaned_text"])
        self.assertEqual(response.data["threads"][0]["source_message_ids"], ["feb-44"])
