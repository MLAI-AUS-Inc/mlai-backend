"""Pulse "Draft with MLAI" dependability; no database or network access."""

from contextlib import nullcontext
from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from startup_updates import preflight, services
from startup_updates.founder_messages import (
    GENERIC_FAILURE, NO_FACTS, SERVICE_BUSY, SOURCES_NEED_ATTENTION, founder_failure_message, run_source_warnings,
)


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


class DefaultMonthTests(SimpleTestCase):
    def test_first_week_defaults_to_the_month_just_finished(self):
        # 3 Nov 09:00 in Melbourne.
        self.assertEqual(services.default_startup_update_target_month(reference=utc(2026, 11, 2, 22)), date(2026, 10, 1))
        # 7 Nov 23:30 in Melbourne is still the first week.
        self.assertEqual(services.default_startup_update_target_month(reference=utc(2026, 11, 7, 12, 30)), date(2026, 10, 1))

    def test_later_days_default_to_the_current_month(self):
        # 8 Nov 01:00 in Melbourne, although UTC still says 7 Nov.
        self.assertEqual(services.default_startup_update_target_month(reference=utc(2026, 11, 7, 14)), date(2026, 11, 1))

    def test_january_rolls_back_a_year(self):
        self.assertEqual(services.default_startup_update_target_month(reference=utc(2027, 1, 3, 2)), date(2026, 12, 1))

    def test_founder_timezone_falls_back_to_melbourne(self):
        self.assertEqual(services.founder_timezone_name(None), "Australia/Melbourne")
        self.assertEqual(services.founder_timezone_name(Obj(reporting_timezone="UTC")), "Australia/Melbourne")
        self.assertEqual(services.founder_timezone_name(Obj(reporting_timezone="Not/AZone")), "Australia/Melbourne")
        self.assertEqual(services.founder_timezone_name(Obj(reporting_timezone="Pacific/Auckland")), "Pacific/Auckland")
        # A founder ahead of Melbourne crosses into day 8 earlier.
        self.assertEqual(services.default_startup_update_target_month(
            reference=utc(2026, 11, 7, 11), timezone_name="Pacific/Auckland"), date(2026, 11, 1))

    def test_scheduled_create_can_ask_for_connected_sources_instead_of_gmail(self):
        from startup_updates.serializers import StartupUpdateRunCreateSerializer
        scheduled = StartupUpdateRunCreateSerializer(data={"user_id": 1, "domain": "a.example", "use_connected_sources": True})
        self.assertTrue(scheduled.is_valid(), scheduled.errors)
        self.assertNotIn("input_sources", scheduled.validated_data)
        legacy = StartupUpdateRunCreateSerializer(data={"user_id": 1, "domain": "a.example"})
        self.assertTrue(legacy.is_valid(), legacy.errors)
        self.assertEqual(legacy.validated_data["input_sources"], ["gmail"])


class FinancialWindowTests(SimpleTestCase):
    def run_request(self, now):
        from startup_updates.activity_scope import monthly_source_period
        return {"target_month": "2026-11-01", "current_month": "2026-11-01", "reporting_timezone": "Australia/Melbourne",
            "narrative_period": monthly_source_period(date(2026, 11, 1), timezone_name="Australia/Melbourne", as_of=now)}

    def test_new_month_for_a_founder_ahead_of_utc_does_not_fail(self):
        now = utc(2026, 10, 31, 20)  # 1 Nov 07:00 in Melbourne, still October in UTC.
        with self.assertRaises(ValueError):
            # The old preflight call: UTC "now" rejects the founder's month.
            services.build_startup_update_target_windows(date(2026, 11, 1), reference=now)
        window = services.startup_update_run_financial_window(self.run_request(now))
        self.assertEqual(window, {"financial_start_date": date(2026, 11, 1), "financial_end_date": date(2026, 11, 1)})

    def test_window_ends_at_the_runs_frozen_cutoff_in_its_timezone(self):
        window = services.startup_update_run_financial_window(self.run_request(utc(2026, 11, 16, 8)))
        self.assertEqual(window["financial_end_date"], date(2026, 11, 16))
        finished = services.startup_update_run_financial_window(self.run_request(utc(2026, 12, 20)))
        self.assertEqual(finished["financial_end_date"], date(2026, 11, 30))


class SkippedSourceTests(SimpleTestCase):
    def test_failed_tools_are_skipped_while_something_usable_remains(self):
        skipped = {}
        checks = [{"provider": "gmail", "status": "needs_reconnect"}, {"provider": "slack", "status": "ready"},
            {"provider": "xero", "status": "temporarily_unavailable", "reason": "rate_limited"}]
        self.assertTrue(preflight.skip_failed_checks(checks, skipped))
        self.assertEqual(skipped["gmail"]["action"], "reconnect")
        self.assertEqual(skipped["gmail"]["code"], "needs_reconnect")
        self.assertEqual(skipped["gmail"]["message"], "Gmail needs reconnecting, so MLAI drafted this update without it.")
        self.assertEqual(skipped["xero"], {"provider": "xero", "code": "rate_limited", "action": "retry",
            "message": "MLAI could not read Xero just now, so this draft leaves it out."})
        self.assertNotIn("slack", skipped)

    def test_nothing_usable_keeps_the_barrier_closed(self):
        skipped = {}
        self.assertFalse(preflight.skip_failed_checks([{"provider": "gmail", "status": "needs_reconnect"}], skipped))
        # Founder notes or documents are never silently dropped.
        skipped = {}
        self.assertFalse(preflight.skip_failed_checks([{"provider": "slack", "status": "ready"},
            {"provider": "manual_documents", "status": "needs_input"}], skipped))
        self.assertNotIn("manual_documents", skipped)

    def test_effective_sources_exclude_backend_and_worker_skips(self):
        request = {"input_sources": ["gmail", "slack", "xero", "stripe"], "skipped_sources": {"xero": {"code": "x"}}}
        result = {"skipped_sources": {"gmail": {"code": "collection_failed"}}}
        self.assertEqual(preflight.effective_input_sources(request, result), ["slack", "stripe"])
        self.assertEqual(set(preflight.run_skipped_sources(request, result)), {"xero", "gmail"})

    def test_bank_feed_label_reads_naturally(self):
        entry = preflight.skipped_source_entry({"provider": "bank_feed", "status": "needs_reconnect"})
        self.assertEqual(entry["message"], "Your bank feed needs reconnecting, so MLAI drafted this update without it.")
        entry = preflight.skipped_source_entry({"provider": "notion"}, stage="collection")
        self.assertEqual(entry, {"provider": "notion", "code": "collection_failed", "action": "retry",
            "message": "MLAI could not finish reading Notion, so this draft may miss some of it."})


class ConnectedSourcesTests(SimpleTestCase):
    def test_only_healthy_connections_the_founder_did_not_switch_off(self):
        user = Obj(pk=7)
        profile = Obj(progress_configuration={"chat_source_preferences": {"3:7": {"luma": False}, "3:9": {"slack": False}}})
        organization = Obj(pk=4, startup_profile=profile)
        rows = MagicMock()
        rows.filter.return_value.values_list.return_value = ["slack", "xero", "luma"]
        with patch("integrations.models.ExternalServiceConnection.objects", rows), patch(
            "integrations.services.external_connectors.google_connection_for_org", return_value=Obj(scope="gmail")), patch(
            "integrations.services.external_connectors.has_gmail_read_scope", return_value=True):
            self.assertEqual(preflight.connected_update_sources(user, organization), ["gmail", "slack", "xero"])
        statuses = rows.filter.call_args.kwargs["status__in"]
        self.assertEqual(set(statuses), {"connected", "syncing"})


class FounderMessageTests(SimpleTestCase):
    def test_internal_errors_become_plain_sentences(self):
        raw = ("Backend request failed: POST https://api.mlai.au/api/v1/integrations/startup-updates/runs/r1/slack-backfill "
            "returned 400: {\"error\": \"not_in_channel\"}")
        self.assertEqual(founder_failure_message(raw), GENERIC_FAILURE)
        self.assertEqual(founder_failure_message("Selected sources need attention: gmail: Reconnect"), SOURCES_NEED_ATTENTION)
        self.assertEqual(founder_failure_message("Error code: 429 insufficient_quota"), SERVICE_BUSY)
        self.assertEqual(founder_failure_message(
            "Your connections were checked, but no usable facts were found for this period."), NO_FACTS)
        self.assertEqual(founder_failure_message(""), GENERIC_FAILURE)

    def test_founder_sentences_and_context_limits_pass_through(self):
        for text in ("MLAI could not read any of your tools for this month. Reconnect a tool or add notes or a document, then try again.",
                "pulse_context_limit: Pulse could not fit this evidence into a processing step."):
            self.assertEqual(founder_failure_message(text), text)

    def test_top_level_source_warnings(self):
        run = Obj(run_request={"skipped_sources": {"gmail": preflight.skipped_source_entry(
            {"provider": "gmail", "status": "needs_reconnect"})}},
            result={"skipped_sources": {"slack": preflight.skipped_source_entry({"provider": "slack"}, stage="collection")},
                "event_source_progress": {"bank_feed": {"complete": True, "skipped_rows": 2}}})
        warnings = run_source_warnings(run, {"xero": ["Xero reports scope is missing; reconnect Xero to calculate Revenue"],
            "gmail": ["Gmail source is unavailable for this run."]})
        self.assertEqual(warnings, [
            {"provider": "gmail", "code": "needs_reconnect", "action": "reconnect",
                "message": "Gmail needs reconnecting, so MLAI drafted this update without it."},
            {"provider": "slack", "code": "collection_failed", "action": "retry",
                "message": "MLAI could not finish reading Slack, so this draft may miss some of it."},
            {"provider": "bank_feed", "code": "rows_skipped", "action": None,
                "message": "MLAI left out 2 bank transactions it could not read."},
            {"provider": "xero", "code": "needs_reconnect", "action": "reconnect",
                "message": "Xero needs reconnecting for MLAI to read all of it."},
        ])
        self.assertEqual(run_source_warnings(None), [])


class GmailGateTests(SimpleTestCase):
    def test_confident_update_worthy_email_is_hydrated_without_the_model_flag(self):
        from startup_updates.api_views import classified_needs_thread_context
        self.assertTrue(classified_needs_thread_context({"relevance_label": "update_worthy"}))
        self.assertTrue(classified_needs_thread_context({"relevance_label": "update_worthy", "needs_thread_context": False}))
        self.assertTrue(classified_needs_thread_context({"relevance_label": "ambiguous"}))
        self.assertFalse(classified_needs_thread_context({"relevance_label": "background"}))
        self.assertFalse(classified_needs_thread_context({"relevance_label": "irrelevant", "needs_thread_context": False}))


class NotionEarlyStopTests(SimpleTestCase):
    def test_stops_once_search_reaches_pages_older_than_the_month(self):
        from startup_updates.api_views import notion_results_reached_before_period
        period = {"start": "2026-10-01T00:00:00+11:00", "end": "2026-11-01T00:00:00+11:00"}
        recent = [{"last_edited_time": "2026-11-05T00:00:00Z"}, {"last_edited_time": "2026-10-20T00:00:00Z"}]
        self.assertFalse(notion_results_reached_before_period(recent, period))
        self.assertTrue(notion_results_reached_before_period([*recent, {"last_edited_time": "2026-09-29T00:00:00Z"}], period))
        self.assertFalse(notion_results_reached_before_period(recent, None))


class ReviewRequiredRunTests(SimpleTestCase):
    def test_review_required_run_is_closed_and_no_longer_blocks(self):
        run = Obj(workflow="startup_monthly_update", status="blocked", result={"review_required": True},
            resume_available=True, error="Your draft is saved and needs your factual review.", save=MagicMock())
        self.assertTrue(services.settle_review_required_run(run))
        self.assertEqual((run.status, run.resume_available, run.error), ("completed", False, ""))
        other = Obj(workflow="startup_monthly_update", status="blocked", result={}, save=MagicMock())
        self.assertFalse(services.settle_review_required_run(other))
        other.save.assert_not_called()
        with patch.object(services, "_iter_startup_update_runs", return_value=[
                Obj(workflow="startup_monthly_update", status="blocked", result={"review_required": True},
                    resume_available=True, error="", save=MagicMock(), run_request={})]):
            self.assertIsNone(services.get_open_startup_update_run(organization=Obj(domain="a.example")))


class BankFeedRowTests(SimpleTestCase):
    def test_one_unreadable_transaction_does_not_fail_the_feed(self):
        from startup_updates import bank_sources
        run = Obj(pk=5, organization_id=4, result={}, run_request={
            "reporting_timezone": "Australia/Melbourne", "reporting_config": {"currency": "AUD"},
            "narrative_period": {"start": "2026-10-01T00:00:00+10:00", "end": "2026-11-01T00:00:00+11:00"},
            "source_connection_pins": {"bank_feed": 9}})
        rows = [
            {"id": "no-currency", "transactionDate": "2026-10-05", "amount": "-12.50", "status": "posted"},
            {"id": "account-currency", "transactionDate": "2026-10-06", "amount": "30", "account": {"currency": "nzd"}},
            {"id": "undated", "amount": "4"},
            {"id": "", "transactionDate": "2026-10-07", "amount": "4"},
        ]
        events = MagicMock()
        with patch.object(bank_sources, "stage_source", return_value=None), patch.object(
                bank_sources.StartupEvent, "objects", events):
            bank_sources.commit_bank_page(run, rows, {"complete": True})
        summaries = [call.kwargs["defaults"]["summary"] for call in events.update_or_create.call_args_list]
        self.assertEqual(len(summaries), 2)
        self.assertIn("AUD 12.50", summaries[0])
        self.assertIn("NZD 30", summaries[1])
        self.assertEqual(run.result["event_source_progress"]["bank_feed"], {"complete": True, "skipped_rows": 2})


class StripePagedSyncTests(SimpleTestCase):
    def test_each_call_saves_pages_and_resumes_from_its_cursor(self):
        from integrations.services import finance
        connection = Obj(pk=3, provider="stripe", access_token="synthetic", sync_cursor={}, save=MagicMock())
        pages = {
            ("/v1/invoices", None): ([{"id": "in_1"}], "in_1"),
            ("/v1/invoices", "in_1"): ([{"id": "in_2"}], None),
            ("/v1/subscriptions", None): ([{"id": "sub_1"}], None),
        }
        calls = []
        def page(_connection, path, _params, starting_after=None):
            calls.append((path, starting_after))
            return pages[(path, starting_after)]
        with patch.object(finance, "_stripe_page", side_effect=page), patch.object(
                finance, "_upsert_stripe_invoices", side_effect=lambda c, items: len(items)), patch.object(
                finance, "_upsert_stripe_subscriptions", side_effect=lambda c, items: len(items)), patch.object(
                finance.transaction, "atomic", return_value=nullcontext()):
            state = finance.sync_stripe_connection_page(connection, {}, time_budget_seconds=0)
            self.assertEqual(state, {"invoices_synced": 1, "starting_after": "in_1"})
            state = finance.sync_stripe_connection_page(connection, state, time_budget_seconds=0)
            self.assertEqual(state["phase"], "subscriptions")
            state = finance.sync_stripe_connection_page(connection, state, time_budget_seconds=0)
            self.assertTrue(state["complete"])
            self.assertEqual(finance.sync_stripe_connection_page(connection, state), state)
        self.assertEqual(calls, [("/v1/invoices", None), ("/v1/invoices", "in_1"), ("/v1/subscriptions", None)])
        self.assertEqual(connection.status, "connected")
        connection.save.assert_called_once()


class PreflightPartialDraftTests(SimpleTestCase):
    """The source check opens for the tools that work when asked to skip the rest."""

    def post(self, *, skip, checks):
        from startup_updates import api_views
        from startup_updates.preflight import SourcesNotReady
        saved = {"input_sources": ["gmail", "slack"], "current_month": "2026-11-01", "target_month": "2026-11-01",
            "reporting_timezone": "Australia/Melbourne", "source_preflight_version": 1,
            "narrative_period": {"start": "2026-11-01T00:00:00+11:00", "end": "2026-11-16T19:00:00+11:00"}}
        run = Obj(pk=1, run_id="r1", status="running", run_request=dict(saved), result={})
        locked = Obj(pk=1, run_id="r1", status="running", run_request=dict(saved), result={}, save=MagicMock())
        rows = MagicMock()
        rows.select_for_update.return_value.get.return_value = locked
        def fail(**kwargs):
            raise SourcesNotReady({"sourceChecks": checks})
        request = Obj(data={"skip_unavailable": True} if skip else {}, headers={})
        with patch.object(api_views, "_get_run_or_404", return_value=run), patch.object(
                api_views, "_get_org_and_binding_for_run", return_value=(Obj(pk=4), Obj(user=Obj(pk=7)), None, None)), patch(
                "integrations.services.source_accounts.report_account_scope", return_value=nullcontext()), patch(
                "startup_updates.preflight.require_source_preflight", side_effect=fail), patch(
                "vibe_raising.views._sync_selected_connector_sources_for_draft", return_value={}), patch(
                "startup_updates.services.refresh_startup_update_run_source_context") as refresh, patch(
                "startup_updates.execution.validate_execution"), patch.object(
                api_views.ContentFactoryRun, "objects", rows), patch.object(
                api_views.transaction, "atomic", return_value=nullcontext()):
            response = api_views.StartupUpdateSourcePreflightView().post(request, "r1")
        return response, locked, refresh

    def test_skip_drafts_from_the_tools_that_work(self):
        checks = [{"provider": "gmail", "status": "needs_reconnect", "message": "Reconnect"},
            {"provider": "slack", "status": "ready", "connectionId": 9}]
        response, locked, refresh = self.post(skip=True, checks=checks)
        self.assertTrue(response.data["ready"])
        self.assertEqual([item["provider"] for item in response.data["skippedSources"]], ["gmail"])
        self.assertEqual(response.data["skippedSources"][0]["action"], "reconnect")
        self.assertEqual(set(locked.run_request["skipped_sources"]), {"gmail"})
        self.assertTrue(locked.run_request["source_preflight_complete"])
        self.assertEqual(refresh.call_args.kwargs["input_sources"], ["slack"])
        self.assertEqual(refresh.call_args.kwargs["end_date"], date(2026, 11, 16))

    def test_without_skip_the_barrier_stays_closed_as_before(self):
        checks = [{"provider": "gmail", "status": "needs_reconnect"}, {"provider": "slack", "status": "ready"}]
        response, locked, refresh = self.post(skip=False, checks=checks)
        self.assertFalse(response.data["ready"])
        self.assertFalse(response.data["nothingUsable"])
        refresh.assert_not_called()

    def test_skip_with_nothing_usable_reports_it(self):
        response, _, refresh = self.post(skip=True, checks=[{"provider": "gmail", "status": "needs_reconnect"},
            {"provider": "slack", "status": "needs_reconnect"}])
        self.assertFalse(response.data["ready"])
        self.assertTrue(response.data["nothingUsable"])
        refresh.assert_not_called()


class EvidenceRefreshSkipTests(SimpleTestCase):
    def call(self, error, failures=0):
        from startup_updates import api_views
        run = Obj(pk=1, run_id="r1", status="running", save=MagicMock(),
            run_request={"input_sources": ["bank_feed", "slack"], "source_connection_pins": {"bank_feed": 9}},
            result={"event_source_progress": {"bank_feed": {"failures": failures}}})
        with patch.object(api_views, "_locked_pipeline_run_context", return_value=(run, Obj(), Obj(user=Obj()), None, None)), patch.object(
                api_views, "_reject_if_run_cancelled", return_value=None), patch.object(
                api_views.transaction, "atomic", return_value=nullcontext()), patch(
                "startup_updates.bank_sources.fetch_bank_page", side_effect=error):
            response = api_views.StartupUpdateSourceEvidenceRefreshView().post(Obj(headers={}, data={}), "r1")
        return response, run

    def test_reconnect_needed_skips_the_tool_at_once(self):
        response, run = self.call(PermissionError("The selected bank account changed."))
        self.assertEqual(response.data, {"pending": True, "provider": "bank_feed", "skipped": True})
        self.assertEqual(run.run_request["skipped_sources"]["bank_feed"]["action"], "reconnect")

    def test_repeated_failures_skip_after_the_limit(self):
        response, run = self.call(ValueError("Bank pagination did not advance."))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("skipped_sources", run.run_request)
        self.assertNotIn("pagination", str(response.data))
        response, run = self.call(ValueError("Bank pagination did not advance."), failures=2)
        self.assertTrue(response.data["skipped"])
        self.assertEqual(run.run_request["skipped_sources"]["bank_feed"]["action"], "retry")
