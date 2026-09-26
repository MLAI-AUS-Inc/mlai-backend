"""Progress calculations; database-free tests, including disclosure boundaries."""
import copy
import unittest
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError
from .progress import (series_from_observations, materialize_charts, public_chart_payload,
    validate_chart_specs, charts_for_revision, compatible_series)
from .progress_google_analytics import monthly_rows
from .progress_views import CustomMetricSerializer
from integrations.services.google_analytics import _extract_totals
from startup_updates.founder_metrics import founder_metric_changes, FinancialMetricEditError
from startup_updates.services import render_monthly_update_markdown

END = date(2026, 9, 18)

def obs(key="ga.totalUsers", value=10, **kwargs):
    fields = dict(pk=1, metric_key=key, metric_name=key, value_number=Decimal(str(value)) if value is not None else None,
        source_provider="google_analytics", unit="count", period_month=date(2026, 8, 1),
        observed_at=datetime(2026, 9, 1, tzinfo=dt_timezone.utc), source_metadata={
            "progress_definition_version": 1, "property_id": "123", "connection_id": 1,
            "timezone": "Australia/Melbourne", "period_start": "2026-08-01", "period_end": "2026-08-31"})
    fields.update(kwargs)
    return SimpleNamespace(**fields)

def build(*items):
    return series_from_observations(items, configuration={}, timezone_name="Australia/Melbourne", end_date=END)

def spec(item, **kwargs):
    return dict(id=item["id"], seriesIds=[item["id"]], months=6, type="line", caption="", **kwargs)

class ProgressContractTests(SimpleTestCase):
    def test_repeat_observation_replaces_not_sums_and_preserves_zero(self):
        series = build(obs(value=9), obs(value=0, pk=2))[0]
        self.assertEqual([point["value"] for point in series["points"]], [0])
        self.assertEqual(series["aggregation"], "unique")

    def test_scope_and_currency_are_distinct(self):
        one = obs()
        other = obs(pk=2, source_metadata={**one.source_metadata, "property_id": "456"})
        self.assertEqual(len(build(one, other)), 2)
        stripe = obs(key="revenue", source_provider="financial", unit="AUD", source_metadata={"definition_version":2, "basis":"paid_stripe_invoice_sales_excluding_tax"})
        self.assertEqual(len(build(stripe, obs(**{**vars(stripe), "pk":2, "unit":"USD", "key":"revenue"}))), 2)

    def test_uncertain_stripe_and_legacy_ga_do_not_become_ready(self):
        self.assertEqual(build(obs(source_metadata={})), [])
        self.assertEqual(build(obs(key="revenue", source_provider="financial", unit="AUD", source_metadata={"definition_version":2, "basis":"paid_stripe_invoice_sales_excluding_tax", "needs_confirmation":True})), [])

    def test_old_partial_observation_does_not_become_complete_as_time_passes(self):
        item = obs(key="eventRegistrations", source_provider="luma", observed_at=datetime(2026,8,12,tzinfo=dt_timezone.utc), source_metadata={"calculation_basis":"luma_events"})
        point = build(item)[0]["points"][0]
        self.assertTrue(point["partial"])
        self.assertEqual(point["periodEnd"], "2026-08-12")
        self.assertIsNone(build(obs(value=None))[0]["points"][0]["value"])

    def test_untracked_attendance_is_not_zero_attendance(self):
        self.assertEqual(build(obs(key="eventAttendees", source_provider="luma", value=0, source_metadata={"calculation_basis":"luma_events"})), [])

    def test_exact_series_selection_and_read_only_frozen_copy(self):
        one, two = build(obs(), obs(key="ga.sessions", pk=2))
        frozen = materialize_charts([spec(one)], [one,two], end_date=END)
        one["points"][0]["value"] = 900
        self.assertEqual(frozen[0]["series"][0]["points"][0]["value"], 10)
        safe = public_chart_payload(frozen)
        self.assertNotIn("scope", safe[0]["series"][0])
        self.assertNotIn("observationId", safe[0]["series"][0]["points"][0])
        self.assertEqual(len(safe[0]["series"]), 1)
        with self.assertRaises(ValidationError):
            materialize_charts([{**spec(one),"seriesIds":["other_company_id"]}], [one,two], end_date=END)
        self.assertFalse(compatible_series([one,two]))

    def test_empty_selection_and_range_without_values(self):
        self.assertEqual(materialize_charts([], [], end_date=END), [])
        item = build(obs())[0]
        with self.assertRaises(ValidationError):
            materialize_charts([spec(item)], [item], end_date=date(2026,1,1))
        with self.assertRaises(ValidationError):
            validate_chart_specs([spec(item),spec(item)])

    def test_inheritance_ignores_client_numbers_and_feature_flag_blocks_new_selection(self):
        previous = [{"spec":{"id":"saved"},"series":[],"cutoff":"2026-08-31"}]
        memo = {"progress_charts":[{"value":99999}]}
        self.assertEqual(charts_for_revision(None,memo,SimpleNamespace(structured_memo={"progress_charts":previous})), previous)
        self.assertNotIn("progress_charts",memo)
        with override_settings(STARTUP_PROGRESS_ENABLED=False), self.assertRaises(ValidationError):
            charts_for_revision(None,{"_progress_chart_specs":[]},None)

    def test_markdown_disclosure_matches_explicit_chart_selection(self):
        item = build(obs())[0]
        memo={"kpi_snapshot":[{"label":"Hidden income","value":"99999"}],"highlights":["We shipped"],"progress_charts":[]}
        self.assertNotIn("Hidden income",render_monthly_update_markdown(memo))
        memo["progress_charts"]=materialize_charts([spec(item)], [item], end_date=END)
        rendered=render_monthly_update_markdown(memo)
        self.assertIn("Website users", rendered)
        self.assertIn("2026-08: 10",rendered)
        self.assertNotIn("Hidden income",rendered)

    def test_custom_validation_and_imported_figures_are_protected(self):
        metric={"key":"ga.totalUsers","value":"10","display_value":"10","source_provider":"google_analytics"}
        self.assertEqual(founder_metric_changes({"ga.totalUsers":"10"},[metric]),{})
        with self.assertRaises(FinancialMetricEditError):
            founder_metric_changes({"ga.totalUsers":"11"},[metric])
        body={"expectedVersion":0,"label":"Pilots","definition":"Active pilots","category":"customers","unit":"pilots","aggregation":"stock","points":[{"date":"2026-08-01","value":0}]}
        self.assertTrue(CustomMetricSerializer(data=body).is_valid())
        self.assertFalse(CustomMetricSerializer(data={**body,"unit":"AUD"}).is_valid())
        self.assertFalse(CustomMetricSerializer(data={**body,"points":[{"date":"2026-08-01","value":"NaN"}]}).is_valid())

class GoogleAnalyticsProgressTests(SimpleTestCase):
    def test_monthly_users_and_rates_are_provider_results_not_sums(self):
        report={"rowCount":2,"rows":[{"dimensionValues":[{"value":"202607"}],"metricValues":[{"value":"12"},{"value":"0.5"}]},{"dimensionValues":[{"value":"202608"}],"metricValues":[{"value":"15"},{"value":"0.6"}]}]}
        values=monthly_rows(report,["totalUsers","engagementRate"],start=date(2026,7,1),end=END)
        self.assertEqual(values,[(date(2026,7,1),"totalUsers",12),(date(2026,7,1),"engagementRate",50),(date(2026,8,1),"totalUsers",15),(date(2026,8,1),"engagementRate",60)])
        with self.assertRaises(ValidationError):
            monthly_rows({**report,"rowCount":3},["totalUsers"],start=date(2026,7,1),end=END)

    def test_breakdown_rates_unique_counts_and_truncated_rows_are_not_added(self):
        report={"metricHeaders":[{"name":"totalUsers"},{"name":"engagementRate"},{"name":"eventCount"}],"dimensionHeaders":[{"name":"eventName"}],"rows":[{"dimensionValues":[{"value":"first"}],"metricValues":[{"value":"10"},{"value":"0.4"},{"value":"20"}]},{"dimensionValues":[{"value":"second"}],"metricValues":[{"value":"8"},{"value":"0.6"},{"value":"30"}]}]}
        self.assertEqual(_extract_totals(report),{"totalUsers":None,"engagementRate":None,"eventCount":50})
        self.assertEqual(_extract_totals({**report,"rowCount":100})["eventCount"],None)
