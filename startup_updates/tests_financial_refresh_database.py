"""Synthetic persisted refresh, ownership and save tests; approved schema only."""
import calendar
import copy
from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from startup_updates import financial_refresh as refresh
from startup_updates.evidence_contract import content_hash
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft, StartupProfile, UserStartupBinding, StartupDataDeletionRequest
from startup_updates.revisions import RevisionConflict, save_revision
from startup_updates.tests_financial_history_unit import evidence_payload, report_payload
from vibe_raising.financial_refresh_views import FinancialRefreshView, FinancialRefreshStatusView
from vibe_raising.views import VibeRaisingMonthlyUpdateView
from workflow_runs.models import ContentFactoryRun


class FinancialRefreshDatabaseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="finance-founder@example.invalid", password="synthetic")
        self.other = get_user_model().objects.create_user(email="finance-other@example.invalid", password="synthetic")
        self.organization = Organization.objects.create(name="Finance synthetic", domain="finance.example.invalid")
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.company = VibeRaisingCompany.objects.create(profile=self.profile, organization=self.organization,
            name="Finance synthetic", domain=self.organization.domain)
        self.settings = StartupProfile.objects.create(organization=self.organization, default_currency="AUD", reporting_timezone="Australia/Melbourne")
        UserStartupBinding.objects.create(user=self.user, organization=self.organization, role="founder")
        self.xero = ExternalServiceConnection.objects.create(user=self.user, organization=self.organization, provider="xero",
            access_token="synthetic-only", external_account_id="tenant-synthetic", scopes=["accounting.reports.read"])
        self.stripe = ExternalServiceConnection.objects.create(user=self.user, organization=self.organization, provider="stripe",
            access_token="synthetic-only", external_account_id="acct_synthetic")
        self.month = date(2026, 8, 1)
        self.creation_key = str(uuid4())
        self.data = {"requestId": str(uuid4()), "creationKey": self.creation_key,
            "targetMonth": self.month.isoformat(), "inputSources": ["stripe", "xero"]}
        self.factory = APIRequestFactory()
        self.submit = patch("startup_updates.financial_refresh.submit_refresh", return_value=True)
        self.submit.start()
        self.addCleanup(self.submit.stop)

    def start(self, **changes):
        result = refresh.start_refresh(company=self.company, user=self.user, data={**self.data, **changes})
        return ContentFactoryRun.objects.get(run_id=result["refreshId"])

    def provider(self, provider, step, connection, request, progress):
        if provider == "stripe":
            return {"count": 2, "hash": "synthetic-stripe-page", "cursor": "in_synthetic", "more": False}
        if step == "currency":
            return {"currency": "AUD"}
        month = date.fromisoformat(step)
        return {"month": step, "end": month.replace(day=calendar.monthrange(month.year, month.month)[1]).isoformat(),
            "report": report_payload(1000, 400)}

    def complete(self, run, provider=None):
        with patch("startup_updates.financial_refresh.fetch_stage", side_effect=provider or self.provider) as fetch:
            for _ in range(12):
                if not refresh.advance_refresh(run.run_id):
                    break
        run.refresh_from_db()
        return fetch

    def draft(self):
        draft = MonthlyUpdateDraft.objects.create(organization=self.organization, month=self.month,
            creation_key=self.creation_key, update_date=date(2026, 8, 31))
        payload = evidence_payload()
        payload["events"] = []
        snapshot = MonthlyEvidenceSnapshot.objects.create(organization=self.organization, month=self.month,
            payload=payload, content_hash=content_hash(payload))
        revision = save_revision(draft, {"summary": "Existing carefully written story",
            "_financial_history_selection": {"enabled": True, "shareAmounts": False}}, snapshot=snapshot,
            validation={"groundedness_status": "founder_asserted"})
        draft.refresh_from_db()
        return draft, revision

    def test_start_never_fetches_sources_or_creates_a_draft(self):
        with patch("startup_updates.financial_refresh.fetch_stage") as fetch:
            run = self.start()
        self.assertEqual(run.status, "queued")
        fetch.assert_not_called()
        self.assertFalse(MonthlyUpdateDraft.objects.exists())
        self.assertFalse(MonthlyEvidenceSnapshot.objects.exists())

    def test_refresh_uses_report_choice_and_existing_job_keeps_its_account(self):
        from integrations.services.source_accounts import select_report_account
        sibling = ExternalServiceConnection.objects.create(user=self.user, organization=self.organization,
            provider="xero", access_token="synthetic-sibling", external_account_id="tenant-sibling",
            scopes=["accounting.reports.read"])
        select_report_account(self.user, self.organization, "xero", self.xero.pk)
        original = self.start()
        self.assertEqual(original.run_request["connections"]["xero"]["id"], self.xero.pk)
        select_report_account(self.user, self.organization, "xero", sibling.pk)
        replay = self.start()
        self.assertEqual(replay.pk, original.pk)
        self.assertEqual(replay.run_request["connections"]["xero"]["id"], self.xero.pk)
        new = self.start(requestId=str(uuid4()))
        self.assertNotEqual(new.pk, original.pk)
        self.assertEqual(new.run_request["connections"]["xero"]["id"], sibling.pk)
        fetched = self.complete(original)
        self.assertEqual({call.args[2].pk for call in fetched.call_args_list if call.args[0] == "xero"}, {self.xero.pk})

    def test_disconnected_report_choice_does_not_fall_back_to_another_tenant(self):
        from integrations.services.source_accounts import select_report_account
        select_report_account(self.user, self.organization, "xero", self.xero.pk)
        ExternalServiceConnection.objects.create(user=self.user, organization=self.organization,
            provider="xero", access_token="synthetic-sibling", external_account_id="tenant-sibling")
        ExternalServiceConnection.objects.filter(pk=self.xero.pk).update(status="disconnected")
        with patch("startup_updates.financial_refresh.fetch_stage") as fetch:
            run = self.start()
        self.assertIsNone(run.run_request["connections"]["xero"])
        self.assertEqual(run.result["sources"]["xero"]["status"], "reconnect_required")
        fetch.assert_not_called()

    def test_idempotency_and_two_clicks_coalesce_without_mutating_pins(self):
        run = self.start()
        original = copy.deepcopy(run.run_request)
        again = self.start()
        second_request = str(uuid4())
        duplicate = self.start(requestId=second_request)
        self.assertEqual(run.pk, again.pk)
        self.assertEqual(run.pk, duplicate.pk)
        self.assertEqual(duplicate.run_request, original)
        self.complete(run)
        retried_alias = self.start(requestId=second_request)
        self.assertEqual(retried_alias.pk, run.pk)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        with self.assertRaises(RevisionConflict):
            self.start(targetMonth="2026-07-01")

    def test_six_fresh_reports_candidate_only_and_exact_preview(self):
        draft, revision = self.draft()
        run = self.start(updateId=draft.pk, expectedRevision=revision.pk)
        fetch = self.complete(run)
        reports = [call.args[1] for call in fetch.call_args_list if call.args[0] == "xero" and call.args[1] != "currency"]
        self.assertEqual(reports, [f"2026-{month:02d}-01" for month in range(3, 9)])
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision_id, revision.pk)
        self.assertEqual(draft.current_revision.structured_memo["summary"], "Existing carefully written story")
        self.assertEqual(draft.revisions.count(), 1)
        value = refresh.serialize_refresh(run)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["candidate"]["ownerPreview"]["points"][-1]["revenue"], 1000)
        self.assertEqual(value["candidate"]["audiencePreview"]["points"][-1]["revenue"], 100)
        self.assertNotIn("report_payload", str(value))
        self.assertNotIn("tenant-synthetic", str(value))

    def test_stripe_failure_keeps_xero_candidate_separate(self):
        def provider(*args):
            if args[0] == "stripe":
                raise ValueError("private provider details")
            return self.provider(*args)
        run = self.start()
        self.complete(run, provider)
        value = refresh.serialize_refresh(run)
        self.assertEqual(value["status"], "partial")
        self.assertEqual(value["candidate"]["ownerPreview"]["points"][-1]["revenue"], 1000)
        self.assertNotIn("private provider", str(value))

    def test_xero_failure_does_not_substitute_stripe_or_cached_chart(self):
        draft, revision = self.draft()
        def provider(*args):
            if args[0] == "xero":
                raise ValueError("Xero unavailable")
            return self.provider(*args)
        run = self.start(updateId=draft.pk, expectedRevision=revision.pk)
        self.complete(run, provider)
        self.assertEqual(refresh.serialize_refresh(run)["status"], "failed")
        self.assertIsNone(refresh.serialize_refresh(run)["candidate"])
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision_id, revision.pk)

    def test_disconnection_during_io_discards_stale_worker_result(self):
        def provider(*args):
            result = self.provider(*args)
            ExternalServiceConnection.objects.filter(pk=self.xero.pk).update(status="disconnected")
            return result
        run = self.start()
        self.complete(run, provider)
        self.assertEqual(run.status, "failed")
        self.assertFalse(MonthlyEvidenceSnapshot.objects.exists())

    def test_data_deletion_during_io_cannot_recreate_financial_evidence(self):
        def provider(*args):
            result = self.provider(*args)
            StartupDataDeletionRequest.objects.create(organization=self.organization,
                request_id="synthetic-deletion", provider="startup", delete_derived_data=True,
                started_at=timezone.now())
            return result
        run = self.start()
        self.complete(run, provider)
        self.assertEqual(run.status, "failed")
        self.assertFalse(MonthlyEvidenceSnapshot.objects.exists())

    def test_expired_job_does_not_start_new_provider_reads(self):
        run = self.start()
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(days=2))
        with patch("startup_updates.financial_refresh.fetch_stage") as fetch:
            self.assertFalse(refresh.advance_refresh(run.run_id))
        fetch.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_active_lease_prevents_duplicate_reads_and_old_fence_cannot_commit(self):
        run = self.start()
        run.result.update(leaseToken="existing-worker", leaseUntil=(timezone.now() + timedelta(minutes=1)).isoformat())
        run.save(update_fields=["result"])
        with patch("startup_updates.financial_refresh.fetch_stage") as fetch:
            self.assertFalse(refresh.advance_refresh(run.run_id))
        fetch.assert_not_called()
        run.result["leaseUntil"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        run.save(update_fields=["result"])
        def stale(*args):
            current = ContentFactoryRun.objects.get(pk=run.pk)
            current.result["leaseToken"] = "newer-worker"
            current.save(update_fields=["result"])
            return self.provider(*args)
        with patch("startup_updates.financial_refresh.fetch_stage", side_effect=stale):
            self.assertFalse(refresh.advance_refresh(run.run_id))
        run.refresh_from_db()
        self.assertNotIn("xero", run.result)

    def test_retry_preserves_progress_and_bounds_attempts(self):
        run = self.start()
        with patch("startup_updates.financial_refresh.fetch_stage", side_effect=TimeoutError()):
            for attempt in range(3):
                refresh.advance_refresh(run.run_id)
                run.refresh_from_db()
                if attempt < 2:
                    self.assertIn("retryAt", run.result)
                    run.result["retryAt"] = (timezone.now() - timedelta(seconds=1)).isoformat()
                    run.save(update_fields=["result"])
        run.refresh_from_db()
        self.assertEqual(run.result["sources"]["xero"]["status"], "failed")

    def test_candidate_is_scoped_to_company_user_month_revision_and_settings(self):
        draft, revision = self.draft()
        run = self.start(updateId=draft.pk, expectedRevision=revision.pk)
        self.complete(run)
        base = {"company": self.company, "user": self.user, "draft": draft,
            "expected_revision": revision.pk, "input_sources": ["gmail", "xero", "stripe"]}
        self.assertEqual(refresh.candidate_for_save(run.run_id, **base).pk, run.result["snapshotId"])
        with self.assertRaises(Http404):
            refresh.candidate_for_save(run.run_id, **{**base, "user": self.other})
        for changes in ({"expected_revision": revision.pk + 1}, {"input_sources": ["xero"]}):
            with self.assertRaises(RevisionConflict):
                refresh.candidate_for_save(run.run_id, **{**base, **changes})
        self.settings.default_currency = "USD"
        self.settings.save(update_fields=["default_currency"])
        with self.assertRaises(RevisionConflict):
            refresh.candidate_for_save(run.run_id, **base)

    def test_expired_or_revoked_candidate_cannot_save(self):
        draft, revision = self.draft()
        run = self.start(updateId=draft.pk, expectedRevision=revision.pk)
        self.complete(run)
        base = {"company": self.company, "user": self.user, "draft": draft, "expected_revision": revision.pk}
        run.result["candidate"]["expiresAt"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        run.save(update_fields=["result"])
        with self.assertRaises(RevisionConflict):
            refresh.candidate_for_save(run.run_id, **base)
        run.result["candidate"]["expiresAt"] = (timezone.now() + timedelta(hours=1)).isoformat()
        run.save(update_fields=["result"])
        ExternalServiceConnection.objects.filter(pk=self.xero.pk).update(external_account_id="other-tenant")
        with self.assertRaises(RevisionConflict):
            refresh.candidate_for_save(run.run_id, **base)

    def test_new_draft_candidate_uses_creation_identity(self):
        run = self.start()
        self.complete(run)
        draft = MonthlyUpdateDraft.objects.create(organization=self.organization, month=self.month, creation_key=self.creation_key)
        snapshot = refresh.candidate_for_save(run.run_id, company=self.company, user=self.user, draft=draft, expected_revision=None)
        self.assertEqual(snapshot.pk, run.result["snapshotId"])
        draft.creation_key = uuid4()
        with self.assertRaises(RevisionConflict):
            refresh.candidate_for_save(run.run_id, company=self.company, user=self.user, draft=draft, expected_revision=None)

    def request(self, view, method, data=None, **kwargs):
        request = getattr(self.factory, method)(f"/?company_id={self.company.pk}", data=data or {}, format="json")
        force_authenticate(request, user=self.user)
        return view.as_view()(request, **kwargs)

    def test_owner_endpoints_and_no_store(self):
        response = self.request(FinancialRefreshView, "post", self.data)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        response = self.request(FinancialRefreshStatusView, "get", refresh_id=response.data["refreshId"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "queued")
        request = self.factory.post(f"/?company_id={self.company.pk}", self.data, format="json")
        force_authenticate(request, user=self.other)
        self.assertIn(FinancialRefreshView.as_view()(request).status_code, {400, 403, 404})

    def test_normal_save_keeps_typed_story_and_original_evidence_with_exact_candidate(self):
        draft, revision = self.draft()
        run = self.start(updateId=draft.pk, expectedRevision=revision.pk)
        self.complete(run)
        response = self.request(VibeRaisingMonthlyUpdateView, "post", {
            "updateId": draft.pk, "expectedRevision": revision.pk, "month": "August", "year": 2026,
            "summary": "Words typed while the finance request was running", "highlights": "A new milestone",
            "metrics": {}, "saveMode": "draft", "audienceVisibility": ["just_me"],
            "inputSources": ["stripe", "xero"], "financialCandidateId": run.run_id,
            "financialHistorySelection": {"enabled": True, "shareAmounts": False}})
        self.assertEqual(response.status_code, 200, response.data)
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision.snapshot_id, revision.snapshot_id)
        self.assertEqual(response.data["update"]["summary"], "Words typed while the finance request was running")
        self.assertEqual(response.data["update"]["financialHistoryOwnerPreview"], run.result["candidate"]["ownerPreview"])
        self.assertEqual(response.data["update"]["financialHistory"], run.result["candidate"]["audiencePreview"])
        self.assertEqual(draft.current_revision.structured_memo["financial_snapshot"], revision.structured_memo["financial_snapshot"])
        self.assertIsNone(draft.published_revision_id)
