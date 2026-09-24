from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.exceptions import NotFound, ValidationError

from organizations.models import Organization
from startup_updates.models import MonthlyUpdateDraft, StartupProfile
from startup_updates.revisions import RevisionConflict
from startup_updates.update_identity import (
    default_generation_date, identity_payload, narrative_window,
    parse_generation_date, resolve_update, run_update,
)


class GenerationDateCompatibilityTests(SimpleTestCase):
    def test_saved_date_is_used_when_request_omits_it(self):
        self.assertIsNone(parse_generation_date({"updateId": 7}, update_id=7))
        draft = SimpleNamespace(month=date(2026, 3, 1), update_date=date(2026, 3, 14))
        self.assertEqual(default_generation_date(draft, today=date(2026, 9, 24)), date(2026, 3, 14))

    def test_month_only_draft_uses_period_end_or_today(self):
        draft = SimpleNamespace(month=date(2026, 3, 1), update_date=None)
        self.assertEqual(default_generation_date(draft, today=date(2026, 9, 24)), date(2026, 3, 31))
        draft.month = date(2026, 9, 1)
        self.assertEqual(default_generation_date(draft, today=date(2026, 9, 24)), date(2026, 9, 24))
        draft.month = date(2025, 12, 1)
        self.assertEqual(default_generation_date(draft, today=date(2026, 9, 24)), date(2025, 12, 31))

    def test_explicit_invalid_date_and_new_update_without_date_are_rejected(self):
        for data, update_id in (({"updateDate": ""}, 7), ({"updateDate": "not-a-date"}, 7), ({}, None)):
            with self.subTest(data=data, update_id=update_id), self.assertRaises(ValidationError):
                parse_generation_date(data, update_id=update_id)
        self.assertEqual(
            parse_generation_date({"updateDate": "2026-03-15"}, update_id=7), date(2026, 3, 15),
        )

    def test_future_month_only_draft_cannot_default_to_an_earlier_day(self):
        draft = SimpleNamespace(month=date(2026, 10, 1), update_date=None)
        with self.assertRaises(ValidationError):
            default_generation_date(draft, today=date(2026, 9, 24))


class IndependentUpdateIdentityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Example", domain="identity.example.invalid")
        StartupProfile.objects.create(organization=self.org, reporting_timezone="Australia/Melbourne")
        self.month = date(2026, 9, 1)

    def create(self, key=None, day=15):
        return resolve_update(self.org, month=self.month, creation_key=key or uuid4(), update_date=date(2026, 9, day))[0]

    def test_two_same_day_posts_and_a_legacy_slot_are_independent(self):
        old = MonthlyUpdateDraft.objects.create(organization=self.org, month=self.month)
        first, second = self.create(), self.create()
        self.assertEqual(len({old.pk, first.pk, second.pk}), 3)
        self.assertEqual(MonthlyUpdateDraft.objects.monthly_slots().get(organization=self.org, month=self.month), old)

    def test_creation_retry_returns_exact_draft(self):
        key = uuid4()
        first = self.create(key)
        second = self.create(key, day=16)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.update_date, first.update_date)

    def test_month_only_old_clients_cannot_choose_an_independent_update(self):
        self.create()
        with self.assertRaises(RevisionConflict):
            resolve_update(self.org, month=self.month)

    def test_identity_is_company_scoped(self):
        draft = self.create()
        other = Organization.objects.create(name="Other", domain="other.identity.invalid")
        with self.assertRaises(NotFound):
            resolve_update(other, month=self.month, update_id=draft.pk)

    def test_edit_by_id_keeps_accounting_month(self):
        first = self.create()
        result, created = resolve_update(self.org, month=date(2026, 10, 1), update_id=first.pk)
        self.assertFalse(created)
        self.assertEqual(result.month, self.month)

    def test_worker_resolves_only_its_target(self):
        first, second = self.create(), self.create()
        run = SimpleNamespace(run_request={"organization_id": self.org.pk, "update_id": second.pk})
        self.assertEqual(run_update(run, self.month, create=True).pk, second.pk)
        with self.assertRaises(RevisionConflict):
            run_update(run, date(2026, 8, 1))
        self.assertNotEqual(first.pk, second.pk)

    def test_legacy_month_precision_does_not_invent_a_day(self):
        draft = MonthlyUpdateDraft.objects.create(organization=self.org, month=self.month)
        self.assertEqual(identity_payload(draft)["datePrecision"], "month")
        self.assertIsNone(identity_payload(draft)["updateDate"])

    def test_published_date_comes_from_reviewed_content(self):
        draft = self.create(day=15)
        self.assertEqual(identity_payload(draft, {"update_date": "2026-09-08"})["updateDate"], "2026-09-08")
        self.assertIsNone(identity_payload(draft, {"update_date": None})["updateDate"])

    def test_legacy_backfill_preserves_ids_and_month_precision(self):
        from importlib import import_module
        from django.apps import apps
        published = datetime(2026, 9, 11, tzinfo=dt_timezone.utc)
        draft = MonthlyUpdateDraft.objects.create(organization=self.org, month=self.month, published_at=published)
        import_module("startup_updates.migrations.0023_independent_update_identity").preserve_publication_times(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        draft.refresh_from_db()
        self.assertEqual(draft.first_published_at, published)
        self.assertIsNone(draft.update_date)
        self.assertIsNone(draft.creation_key)

    def test_repeated_financial_snapshots_keep_the_latest_actual_cutoff(self):
        from vibe_raising.metric_history import build_metric_history
        def memo(value, cutoff):
            return {"reporting_period": {"cutoff": cutoff}, "kpi_snapshot": [{"metric_key": "revenue", "value_number": value, "value": str(value), "unit": "AUD"}]}
        history = build_metric_history([(self.month, memo(10, "2026-09-08T00:00:00Z")), (self.month, memo(20, "2026-09-15T00:00:00Z")), (self.month, memo(20, "2026-09-15T00:00:00Z"))])
        self.assertEqual(len(history["revenue"]["points"]), 1)
        self.assertEqual(history["revenue"]["points"][0]["value"], 20)

    @patch("startup_updates.update_identity.timezone.now", return_value=datetime(2026, 9, 15, 6, tzinfo=dt_timezone.utc))
    def test_first_window_and_same_day_predecessor_use_exact_cutoffs(self, now):
        draft = self.create()
        first = narrative_window(self.org, draft, date(2026, 9, 15))
        self.assertEqual(first["start"], "2026-09-01T00:00:00+10:00")
        self.assertTrue(first["end_exclusive"])
        prior = self.create()
        prior.published_at = now.return_value
        prior.structured_memo = {"update_date": "2026-09-15", "narrative_period": {"end": "2026-09-15T02:00:00+00:00"}}
        prior.save()
        second = narrative_window(self.org, draft, date(2026, 9, 15))
        self.assertEqual(second["start"], "2026-09-15T02:00:00+00:00")

    @patch("startup_updates.update_identity.timezone.now", return_value=datetime(2026, 11, 1, tzinfo=dt_timezone.utc))
    def test_dst_end_boundary_uses_reporting_timezone(self, now):
        window = narrative_window(self.org, self.create(), date(2026, 10, 4))
        self.assertEqual(window["end"], "2026-10-05T00:00:00+11:00")
        with self.assertRaises(ValidationError):
            narrative_window(self.org, self.create(), date(2026, 10, 4), requested_start="2026-10-05T00:00:00+11:00")


class IndependentFounderSaveTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from rest_framework.test import APIClient
        self.user = get_user_model().objects.create_user(email="date-founder@example.invalid", password="local-only")
        profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        self.company = VibeRaisingCompany.objects.create(profile=profile, name="Date startup", domain="", registered=False)
        profile.active_company = self.company
        profile.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def save(self, **extra):
        return self.client.post("/api/v1/vibe-raising/updates/", {
            "companyId": str(self.company.pk), "month": "March", "year": 2026,
            "updateDate": "2026-03-14", "creationKey": str(uuid4()), "saveMode": "draft",
            "highlights": "Shipped an independent release.", **extra,
        }, format="json")

    def test_save_edit_publish_and_archive_visibility(self):
        a, b = self.save(), self.save()
        self.assertIn(a.status_code, (200, 201), a.data)
        self.assertIn(b.status_code, (200, 201), b.data)
        first, second = a.data["update"], b.data["update"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.client.get(f"/api/v1/vibe-raising/updates/?company_id={self.company.pk}").data["updates"], [])
        edited = self.save(updateId=first["id"], creationKey=first["creationKey"], expectedRevision=first["revisionId"], highlights="Only this entry changed.")
        self.assertIn(edited.status_code, (200, 201), edited.data)
        untouched = MonthlyUpdateDraft.objects.get(pk=second["id"])
        self.assertEqual(untouched.current_revision_id, second["revisionId"])
        saved = edited.data["update"]
        receipt = self.client.post(f"/api/v1/vibe-raising/updates/{saved['id']}/publish/", {
            "companyId": str(self.company.pk), "revisionId": saved["revisionId"], "revisionHash": saved["revisionHash"], "audienceVisibility": ["just_me"],
        }, format="json")
        self.assertEqual(receipt.status_code, 200, receipt.data)
        archive = self.client.get(f"/api/v1/vibe-raising/updates/?company_id={self.company.pk}").data["updates"]
        self.assertEqual([item["id"] for item in archive], [first["id"]])
        self.assertEqual(archive[0]["updateDate"], "2026-03-14")

    def test_stale_revision_cannot_change_date(self):
        saved = self.save().data["update"]
        result = self.save(updateId=saved["id"], creationKey=saved["creationKey"], updateDate="2026-03-16", expectedRevision=999999)
        self.assertEqual(result.status_code, 409, result.data)
        self.assertEqual(MonthlyUpdateDraft.objects.get(pk=saved["id"]).update_date, date(2026, 3, 14))

    def test_unapproved_date_edit_does_not_move_a_legacy_publication(self):
        from vibe_raising.views import _ensure_binding_for_company
        organization, _, _ = _ensure_binding_for_company(user=self.user, company=self.company)
        legacy = MonthlyUpdateDraft.objects.create(organization=organization, month=date(2026, 3, 1),
            published_at=datetime(2026, 9, 11, tzinfo=dt_timezone.utc), structured_memo={"highlights": ["Original story"]})
        edited = self.save(updateId=legacy.pk)
        self.assertEqual(edited.status_code, 200, edited.data)
        published = self.client.get(f"/api/v1/vibe-raising/updates/?company_id={self.company.pk}").data["updates"][0]
        self.assertEqual(published["highlights"], "Original story")
        self.assertIsNone(published["updateDate"])
        self.assertEqual(published["datePrecision"], "month")

    def test_lost_creation_response_can_be_retried_without_another_revision(self):
        key = str(uuid4())
        first = self.save(creationKey=key)
        retry = self.save(creationKey=key)
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(first.data["update"]["revisionId"], retry.data["update"]["revisionId"])
        self.assertEqual(MonthlyUpdateDraft.objects.count(), 1)
        self.assertEqual(self.save(creationKey=key, highlights="Different writing").status_code, 409)

    @patch("vibe_raising.views._dispatch_run_to_valley", return_value=True)
    def test_ai_start_targets_one_draft_and_freezes_a_backdated_window(self, dispatch):
        first = self.save().data["update"]
        second = self.save().data["update"]
        response = self.client.post("/api/v1/vibe-raising/email-draft/start/", {
            "companyId": str(self.company.pk), "inputSources": ["manual_documents"], "manualSummary": "We shipped our release.",
            "targetMonth": "2026-03-01", "updateDate": "2026-03-14", "updateId": second["id"],
            "creationKey": second["creationKey"], "expectedRevision": second["revisionId"],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        from workflow_runs.models import ContentFactoryRun
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.run_request["update_id"], int(second["id"]))
        self.assertEqual(run.run_request["narrative_period"]["end"][:10], "2026-03-15")
        self.assertEqual(run.run_request["financial_cutoff"][:10], "2026-03-14")
        self.assertEqual(MonthlyUpdateDraft.objects.get(pk=first["id"]).current_revision_id, first["revisionId"])


from integrations.tests_startup_updates import StartupUpdateApiTestCase
from startup_updates import tests_revisions as revision_tests


class IndependentPipelineTests(StartupUpdateApiTestCase):
    pin = revision_tests.MonthlyEvidencePipelineTests.pin
    submit = revision_tests.MonthlyEvidencePipelineTests.submit

    def setUp(self):
        super().setUp()
        from startup_updates.models import UserStartupBinding
        from startup_updates.services import create_startup_update_run
        StartupProfile.objects.create(organization=self.organization, default_currency="AUD", reporting_timezone="Australia/Melbourne")
        self.binding = UserStartupBinding.objects.create(user=self.user, organization=self.organization, google_connection=self.google_connection)
        self.run = create_startup_update_run(organization=self.organization, binding=self.binding, target_month=date(2026, 3, 1), input_sources=["gmail"])
        self.first, _ = resolve_update(self.organization, month=date(2026, 3, 1), creation_key=uuid4(), update_date=date(2026, 3, 8))
        self.target, _ = resolve_update(self.organization, month=date(2026, 3, 1), creation_key=uuid4(), update_date=date(2026, 3, 15))
        self.run.run_request.update({"update_id": self.target.pk, "creation_key": str(self.target.creation_key), "base_revision": None,
            "narrative_period": {"start": "2026-03-08T00:00:00+11:00", "end": "2026-03-16T00:00:00+11:00", "timezone": "Australia/Melbourne", "end_exclusive": True}})
        self.run.save(update_fields=["run_request"])

    def generate(self):
        pin = self.pin()
        item = {"month": "2026-03-01", "snapshot_id": pin["snapshot_id"], "expected_revision": None,
            "structured_memo": {"highlights": ["We delivered the release."]}}
        response = self.submit(item)
        self.assertEqual(response.status_code, 200, response.data)
        return item, response

    def test_generation_and_retry_only_write_the_target(self):
        item, response = self.generate()
        self.assertEqual(response.data["drafts"][0]["updateId"], self.target.pk)
        self.first.refresh_from_db()
        self.assertIsNone(self.first.current_revision_id)
        retry = self.submit(item)
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.data["drafts"][0]["revisionId"], response.data["drafts"][0]["revisionId"])
        self.target.refresh_from_db()
        self.assertEqual(self.target.current_revision.structured_memo["narrative_period"], self.run.run_request["narrative_period"])

    def test_cancel_restores_the_target_without_deleting_another_same_month_draft(self):
        self.generate()
        from startup_updates.services import cancel_startup_update_run
        result = cancel_startup_update_run(run_id=self.run.run_id, organization=self.organization,
            binding_id=self.run.run_request["binding_id"], google_connection_id=self.google_connection.pk, cancelled_by_user_id=self.user.pk)
        self.assertTrue(result["cancel_applied"])
        self.target.refresh_from_db()
        self.first.refresh_from_db()
        self.assertIsNone(self.target.current_revision_id)
        self.assertEqual(self.target.update_date, date(2026, 3, 15))
        self.assertEqual(MonthlyUpdateDraft.objects.filter(organization=self.organization).count(), 2)

    def test_generation_rejects_a_founder_edit_after_the_run_started(self):
        from django.urls import reverse
        from startup_updates.revisions import capture_snapshot, save_revision
        save_revision(self.target, {"highlights": ["Founder writing"]}, snapshot=capture_snapshot(self.organization, self.target.month))
        with self._with_key():
            self.client.post(reverse("startup_updates_source_evidence_refresh", args=[self.run.run_id]), {}, format="json", **self.headers)
            response = self.client.post(reverse("startup_updates_evidence_snapshot", args=[self.run.run_id]), {}, format="json", **self.headers)
        self.assertEqual(response.status_code, 409, response.data)

    def test_source_window_can_cross_months_without_changing_accounting_month(self):
        from startup_updates.models import StartupEvent
        from startup_updates.services import build_timeline_payload
        self.run.run_request["narrative_period"]["start"] = "2026-02-26T00:00:00+11:00"
        self.run.save(update_fields=["run_request"])
        for day, month, title in [(date(2026, 2, 27), date(2026, 2, 1), "Included"), (date(2026, 2, 20), date(2026, 2, 1), "Too early"), (date(2026, 3, 17), date(2026, 3, 1), "Too late")]:
            StartupEvent.objects.create(organization=self.organization, run=self.run, canonical_key=title, event_type="product", title=title, event_date=day, month_bucket=month)
        timeline = build_timeline_payload(organization=self.organization, requested_months=["2026-03-01"], run=self.run)
        self.assertEqual(list(timeline["months"]), ["2026-03-01"])
        self.assertEqual([event["title"] for event in timeline["months"]["2026-03-01"]["events"]], ["Included"])
        snapshot = self.pin()["payload"]
        self.assertEqual([event["title"] for event in snapshot["events"]], ["Included"])
        self.assertEqual(snapshot["period"]["month"], "2026-03-01")
