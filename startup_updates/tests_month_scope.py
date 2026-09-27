"""Calendar-month connector evidence regressions; no database or provider calls."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from integrations.services.google_analytics import period_bounds_for_run
from startup_updates import activity_scope, api_views, services, update_identity


SEPTEMBER = {
    "start": "2026-09-01T00:00:00+10:00", "end": "2026-10-01T00:00:00+10:00",
    "timezone": "Australia/Melbourne", "end_exclusive": True,
}
REQUEST = {"target_month": "2026-09-01", "reporting_timezone": "Australia/Melbourne", "narrative_period": SEPTEMBER}


class MonthlyWindowTests(SimpleTestCase):
    def test_september_edited_in_november_still_covers_only_september(self):
        org = Obj(startup_profile=Obj(reporting_timezone="Australia/Melbourne"))
        with patch.object(update_identity.timezone, "now", return_value=datetime(2026, 11, 15, tzinfo=timezone.utc)), patch.object(update_identity, "previous_publications") as prior:
            actual = update_identity.narrative_window(org, Obj(pk=1, month=date(2026, 9, 1)), date(2026, 9, 8), default_days=30)
        self.assertEqual(actual, SEPTEMBER)
        prior.assert_not_called()

    def test_regenerating_current_month_includes_earlier_days(self):
        org = Obj(startup_profile=Obj(reporting_timezone="Australia/Melbourne"))
        now = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
        with patch.object(update_identity.timezone, "now", return_value=now):
            actual = update_identity.narrative_window(org, Obj(pk=1, month=date(2026, 9, 1)), date(2026, 9, 8), default_days=30)
        self.assertEqual(actual["start"], SEPTEMBER["start"])
        self.assertEqual(actual["end"], "2026-09-26T12:00:00+10:00")

    def test_dst_and_year_boundaries(self):
        october = activity_scope.monthly_source_period(date(2026, 10, 1), timezone_name="Australia/Melbourne", as_of=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(october["start"], "2026-10-01T00:00:00+10:00")
        self.assertEqual(october["end"], "2026-11-01T00:00:00+11:00")
        december = activity_scope.monthly_source_period(date(2026, 12, 1), as_of=datetime(2027, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(december["end"], "2027-01-01T00:00:00+00:00")

    def test_custom_range_cannot_escape_month(self):
        org = Obj(startup_profile=Obj(reporting_timezone="Australia/Melbourne"))
        with patch.object(update_identity.timezone, "now", return_value=datetime(2026, 11, 1, tzinfo=timezone.utc)):
            for extra in ({"requested_start": "2026-08-31T23:59:59+10:00"}, {"requested_end": "2026-10-02T00:00:00+10:00"}):
                with self.subTest(extra=extra), self.assertRaises(ValidationError):
                    update_identity.narrative_window(org, Obj(month=date(2026, 9, 1)), date(2026, 9, 30), **extra)

    def test_legacy_rolling_run_is_clamped_before_source_queries(self):
        request = {**REQUEST, "activity_window_days": 30, "narrative_period": {**SEPTEMBER, "start": "2026-08-28T00:00:00+10:00"}}
        self.assertEqual(activity_scope.run_activity_period(request), SEPTEMBER)
        start, end = api_views._get_run_window_bounds(Obj(run_request=request))
        self.assertEqual(start, datetime.fromisoformat(SEPTEMBER["start"]))
        self.assertEqual(end + timedelta(microseconds=1), datetime.fromisoformat(SEPTEMBER["end"]))

    def test_source_queries_bound_a_legacy_month_without_narrative(self):
        run = Obj(run_request={"target_month": "2026-09-01", "reporting_timezone": "Australia/Melbourne", "backfill_window_start": "2026-08-01T00:00:00+10:00", "backfill_window_end": "2026-10-15T00:00:00+11:00"})
        start, end = api_views._get_run_window_bounds(run)
        self.assertEqual(start.isoformat(), SEPTEMBER["start"])
        self.assertEqual(end + timedelta(microseconds=1), datetime.fromisoformat(SEPTEMBER["end"]))

    def test_pinned_older_runs_cannot_be_reused(self):
        self.assertFalse(activity_scope.run_uses_month_scope(REQUEST))
        current = {**REQUEST, "source_period_contract": "calendar_month_v1"}
        self.assertTrue(activity_scope.run_uses_month_scope(current))
        self.assertFalse(activity_scope.run_uses_month_scope({**current, "narrative_period": {**SEPTEMBER, "start": "2026-08-28T00:00:00+10:00"}}))

    def test_worker_snapshot_and_submit_reject_old_pins_before_writing(self):
        from startup_updates.revisions import RevisionConflict
        run = Obj(run_request={**REQUEST, "evidence_snapshots": {"2026-09-01": {"snapshot_id": 44}}})
        for view_type, method in ((api_views.StartupUpdateEvidenceSnapshotView, "post"), (api_views.StartupUpdateDraftResultsView, "post"), (api_views.StartupUpdateDraftResultsView, "get")):
            with self.subTest(view=view_type, method=method), patch.object(api_views, "_locked_pipeline_run_context", return_value=(run, Obj(), Obj(), None, Obj())), patch.object(api_views, "_reject_if_run_cancelled", return_value=None), patch.object(api_views, "get_object_or_404") as fetch, patch.object(api_views, "_update_run_step") as write, self.assertRaises(RevisionConflict) as caught:
                getattr(view_type, method).__wrapped__(view_type(), Obj(data={}), "old-run")
            self.assertIn("Cancel it", str(caught.exception.detail))
            fetch.assert_not_called()
            write.assert_not_called()
            self.assertEqual(run.run_request["evidence_snapshots"]["2026-09-01"]["snapshot_id"], 44)

    def test_worker_rejects_incompatible_immutable_snapshot_under_new_run(self):
        from startup_updates.revisions import RevisionConflict
        request = {**REQUEST, "source_period_contract": "calendar_month_v1", "draft_months": ["2026-09-01"]}
        good = {"source_period_contract": "calendar_month_v1", "narrative_period": SEPTEMBER, "period": {"month": "2026-09-01"}}
        activity_scope.require_month_source_contract(request, snapshot=Obj(month=date(2026, 9, 1), payload=good))
        for payload in ({**good, "source_period_contract": None}, {**good, "narrative_period": {**SEPTEMBER, "start": "2026-08-28T00:00:00+10:00"}}, {**good, "period": {"month": "2026-08-01"}}):
            with self.subTest(payload=payload), self.assertRaises(RevisionConflict):
                activity_scope.require_month_source_contract(request, snapshot=Obj(month=date(2026, 9, 1), payload=payload))

    def test_financial_fetch_starts_in_represented_month(self):
        windows = services.build_startup_update_target_windows("2026-09-01", reference=datetime(2026, 11, 1, tzinfo=timezone.utc), timezone_name="Australia/Melbourne")
        self.assertEqual(windows["financial_start_date"], date(2026, 9, 1))
        self.assertEqual(windows["financial_end_date"], date(2026, 9, 30))

    def test_analytics_is_calendar_month_even_with_legacy_rolling_flag(self):
        actual = period_bounds_for_run({**REQUEST, "activity_window_days": 30})
        self.assertEqual(actual, ("2026-09-01", "2026-09-30", "2026-08-01", "2026-08-31"))
        actual = period_bounds_for_run({**REQUEST, "narrative_period": {**SEPTEMBER, "end": "2026-09-07T12:00:00+10:00"}})
        self.assertEqual(actual[:2], ("2026-09-01", "2026-09-07"))

    def test_timestamp_filter_handles_month_boundaries_and_missing_dates(self):
        for field in ("posted_at", "internal_date", "last_edited_time"):
            for stamp, expected in ((SEPTEMBER["start"], True), (SEPTEMBER["end"], False), ("2026-08-31T13:59:59Z", False), ("2026-09-30T13:59:59Z", True), (None, False)):
                with self.subTest(field=field, stamp=stamp):
                    self.assertEqual(activity_scope.message_in_activity_window({field: stamp}, SEPTEMBER), expected)

    def test_invalid_persisted_windows_fail_closed(self):
        for period in ({"start": "bad", "end": SEPTEMBER["end"]}, {"start": "2026-09-01", "end": "2026-10-01"}):
            with self.assertRaises(ValueError):
                activity_scope.activity_window(period)

    def test_later_notion_edits_use_only_retained_in_month_body(self):
        old = {"notion_page_id": "page", "last_edited_time": "2026-09-12T00:00:00Z", "cleaned_text": "September fact"}
        newest = {**old, "last_edited_time": "2026-10-12T00:00:00Z", "cleaned_text": "October fact"}
        cached = activity_scope.cached_notion_month_version({"id": "page", "last_edited_time": newest["last_edited_time"]}, {"old": old, "new": newest}, SEPTEMBER)
        self.assertEqual(cached, old)
        self.assertIsNone(activity_scope.cached_notion_month_version({"id": "missing"}, {"old": old}, SEPTEMBER))

    def test_prior_publications_are_one_published_copy_per_earlier_month(self):
        august = Obj(pk=8, month=date(2026, 8, 1), published_revision_id=None, update_date=None,
            structured_memo={"highlights": ["August delivery"]}, first_published_at=None,
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        org = Obj(pk=4)
        with patch.object(update_identity.MonthlyUpdateDraft, "objects") as rows, patch("startup_updates.monthly_groups.monthly_representatives") as representatives:
            representatives.return_value.exclude.return_value.select_related.return_value = [august]
            actual = update_identity.previous_publications(org, 9, date(2026, 9, 26))
        rows.filter.assert_called_once_with(organization=org, month__lt=date(2026, 9, 1), published_at__isnull=False)
        representatives.assert_called_once_with(rows.filter.return_value, published=True)
        self.assertEqual(actual[0][0], august)
        self.assertEqual(actual[0][2], "2026-08")

    def test_luma_has_no_adjacent_month_context(self):
        with patch.object(services.ExternalServiceConnection, "objects") as connections, patch.object(services.LumaEventSelection, "objects") as events, patch.object(services.StartupMetricObservation, "objects"):
            connections.filter.return_value.exclude.return_value.order_by.return_value.first.return_value = Obj(pk=3)
            context = services.build_luma_run_context(organization=Obj(pk=4), target_month=date(2026, 9, 1), activity_period=SEPTEMBER)
        filters = events.filter.call_args.kwargs
        self.assertEqual(filters["start_at__gte"].isoformat(), SEPTEMBER["start"])
        self.assertLessEqual(filters["start_at__lt"], datetime.fromisoformat(SEPTEMBER["end"]))
        self.assertEqual(context["context_days_each_side"], 0)
        self.assertEqual(context["event_selection_mode"], "target_month")

    def test_linear_later_project_status_is_not_historical_evidence(self):
        project = MagicMock(raw_payload={"updatedAt": "2026-10-05T00:00:00Z"}, extraction_hints={}, description="October launch", status_name="Launched", linear_project_id="project", name="Project", start_date=None, target_date=None)
        project.issues.order_by.return_value.filter.return_value = MagicMock(**{"__iter__.return_value": iter([]), "count.return_value": 0})
        project.project_updates.order_by.return_value.filter.return_value = MagicMock(**{"__iter__.return_value": iter([]), "count.return_value": 0})
        bundle = services.compact_linear_project_bundle(project, activity_period=SEPTEMBER)
        self.assertIsNone(bundle["description"])
        self.assertIsNone(bundle["status_name"])
        self.assertNotIn("linear:project:project", bundle["source_record_ids"])
        self.assertEqual(bundle["issue_count"], 0)
