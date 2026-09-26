"""Database-free tests for automatic recent source defaults."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from community_chat.startups import source_preferences as preferences
from community_chat.startups.connections import DisconnectView
from community_chat.startups.lifecycle import source_capabilities
from integrations.services import external_connectors
from integrations.services.google_analytics import period_bounds_for_run
from startup_updates import activity_scope, services, update_identity

PERIOD = {"start": "2026-08-27T12:00:00+10:00", "end": "2026-09-26T12:00:00+10:00", "timezone": "Australia/Melbourne", "end_exclusive": True}


class SourcePreferenceTests(SimpleTestCase):
    def test_off_preserves_connection_and_usability(self):
        source = source_capabilities({"provider": "slack", "status": "connected", "connectionId": 3}, preferences={"slack": False})
        self.assertFalse(source["enabled"])
        self.assertTrue(source["usableForUpdates"])
        self.assertTrue(source["canDisconnect"])
        self.assertEqual(source["status"], "connected")
        self.assertEqual(source["activityWindowDays"], 30)

    def test_drive_does_not_promise_unimplemented_import(self):
        source = source_capabilities({"provider": "google_drive", "status": "connected", "connectionId": 3, "configured": True})
        self.assertFalse(source["enabled"])
        self.assertFalse(source["usableForUpdates"])
        self.assertTrue(source["canConnect"])
        self.assertIn("not available yet", source["warning"])

    def test_preferences_are_company_scoped(self):
        profile = Obj(progress_configuration={preferences.PREFERENCE_KEY: {"a": {"gmail": False, "linear": "false"}, "b": {"gmail": True}}})
        with patch.object(preferences.StartupProfile, "objects") as manager:
            manager.filter.return_value.first.return_value = profile
            actual = preferences.source_preferences(Obj(pk="a", organization_id=7))
            manager.filter.assert_called_once_with(organization_id=7)
        self.assertEqual(actual, {"gmail": False})

    def test_save_preserves_other_companies_and_progress_config(self):
        profile = MagicMock(progress_configuration={"version": 2, preferences.PREFERENCE_KEY: {"sibling": {"gmail": True}}})
        with patch.object(preferences.Organization, "objects"), patch.object(preferences.StartupProfile, "objects") as manager:
            manager.select_for_update.return_value.get_or_create.return_value = profile, False
            result = preferences.set_source_preference.__wrapped__(Obj(pk="owned", organization_id=7), "gmail", False)
        self.assertEqual(profile.progress_configuration, {"version": 2, preferences.PREFERENCE_KEY: {"sibling": {"gmail": True}, "owned": {"gmail": False}}})
        self.assertEqual(result["enabled"], False)

    def test_invalid_values_fail_before_persistence(self):
        for provider, value in (("other", True), ("gmail", "false"), ("slack", 0), ("luma", None)):
            with self.subTest(provider=provider, value=value), self.assertRaises(ValidationError):
                preferences.set_source_preference.__wrapped__(Obj(pk="owned", organization_id=7), provider, value)

    def test_post_does_not_disconnect(self):
        view = DisconnectView()
        view.company = Obj(pk="owned")
        with patch("community_chat.startups.connections.set_source_preference", return_value={"enabled": False}) as save, patch("community_chat.startups.connections.disconnect_external_connection") as disconnect:
            view.post(Obj(data={"enabled": False}), "slack")
        save.assert_called_once_with(view.company, "slack", False)
        disconnect.assert_not_called()


class RecentActivityTests(SimpleTestCase):
    def test_current_update_uses_thirty_days_without_previous_publication(self):
        now = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
        org = Obj(startup_profile=Obj(reporting_timezone="Australia/Melbourne"))
        with patch.object(update_identity.timezone, "now", return_value=now), patch.object(update_identity, "previous_publications") as prior:
            period = update_identity.narrative_window(org, Obj(pk=9), date(2026, 9, 26), default_days=30)
        self.assertEqual(period, PERIOD)
        prior.assert_not_called()

    def test_historical_window_includes_entire_update_date(self):
        org = Obj(startup_profile=Obj(reporting_timezone="UTC"))
        with patch.object(update_identity.timezone, "now", return_value=datetime(2026, 9, 26, tzinfo=timezone.utc)):
            period = update_identity.narrative_window(org, Obj(pk=9), date(2026, 2, 28), default_days=30)
        start, end = activity_scope.activity_window(period)
        self.assertEqual(end, datetime(2026, 3, 1, tzinfo=timezone.utc))
        self.assertEqual(end - start, timedelta(days=30))

    def test_cached_messages_are_half_open_and_require_date(self):
        for stamp, expected in ((PERIOD["start"], True), (PERIOD["end"], False), ("2025-01-01T00:00:00Z", False), (None, False)):
            self.assertEqual(activity_scope.message_in_activity_window({"posted_at": stamp}, PERIOD), expected)

    def test_analytics_window_and_prior_window_are_thirty_days(self):
        self.assertEqual(period_bounds_for_run({"activity_window_days": 30, "narrative_period": PERIOD}), ("2026-08-28", "2026-09-26", "2026-07-29", "2026-08-27"))
        self.assertEqual(period_bounds_for_run({"current_month": "2026-09-01"}), ("2026-09-01", "2026-09-30", "2026-08-01", "2026-08-31"))

    def test_discovery_pages_without_changing_manual_selections(self):
        user, organization = Obj(pk=2), Obj(pk=4)
        with patch.object(external_connectors, "serialize_slack_channels", side_effect=[{"channels": [{"channelId": "A"}], "nextCursor": "page-2"}, {"channels": [{"channelId": "B"}, {"channelId": "A"}]}]) as fetch, patch.object(external_connectors, "update_slack_channel_selections") as select:
            scope = activity_scope.discover_activity_resources(user, organization, ["slack", "gmail"])
        self.assertEqual(scope, {"slack": ["A", "B"]})
        self.assertEqual(fetch.call_args.kwargs, {"organization": organization, "cursor": "page-2", "limit": 200})
        select.assert_not_called()

    def test_repeated_pagination_fails_instead_of_truncating(self):
        with patch.object(external_connectors, "serialize_slack_channels", return_value={"channels": [], "nextCursor": "again"}), self.assertRaises(external_connectors.ConnectorConfigurationError):
            activity_scope.discover_activity_resources(Obj(), Obj(), ["slack"])

    def test_slack_bundle_does_not_reuse_old_cached_text(self):
        thread = Obj(message_payloads=[{"message_id": "old", "posted_at": "2025-01-01T00:00:00Z", "cleaned_text": "Old content"}], extraction_hints={}, channel_id="C1", channel_name="general", thread_ts="1", source_message_ids=["old"], source_message_count=1, cleaned_text="Old content", participant_summary={}, heuristic_score=0, heuristic_reasons=[], relevance_score=0, relevance_reason="")
        bundle = services.compact_slack_thread_bundle(thread, slack_thread_id="slack:1", activity_period=PERIOD)
        self.assertEqual(bundle["message_payloads"], [])
        self.assertEqual(bundle["source_message_ids"], [])
        self.assertEqual(bundle["cleaned_text"], "")

    def test_luma_uses_recent_window_without_month_buffer(self):
        with patch.object(services.ExternalServiceConnection, "objects") as connections, patch.object(services.LumaEventSelection, "objects") as events, patch.object(services.StartupMetricObservation, "objects"):
            connections.filter.return_value.exclude.return_value.order_by.return_value.first.return_value = Obj(pk=3)
            context = services.build_luma_run_context(organization=Obj(pk=4), target_month=date(2026, 9, 1), activity_period=PERIOD)
        filters = events.filter.call_args.kwargs
        self.assertEqual(filters["start_at__gte"].isoformat(), PERIOD["start"])
        self.assertEqual(filters["start_at__lte"] + timedelta(microseconds=1), datetime.fromisoformat(PERIOD["end"]))
        self.assertEqual(context["event_selection_mode"], "recent_activity")
        self.assertEqual(context["context_days_each_side"], 0)


class WorkerScopeTests(SimpleTestCase):
    def test_slack_historical_membership_uses_messages_and_exact_connection(self):
        from startup_updates import api_views
        connection = Obj(pk=3)
        run = Obj(run_request={"activity_window_days": 30, "backfill_window_start": PERIOD["start"], "backfill_window_end": PERIOD["end"]})
        threads = MagicMock()
        with patch.object(api_views.SlackMessageArtifact, "objects") as messages, patch.object(api_views, "Exists") as exists:
            api_views._slack_threads_in_run_window(threads, run, connection)
        self.assertEqual(messages.filter.call_args.kwargs["connection"], connection)
        bounds = messages.filter.return_value.filter.return_value.filter.call_args.kwargs
        self.assertEqual(bounds["posted_at__gte"], datetime.fromisoformat(PERIOD["start"]))
        self.assertEqual(bounds["posted_at__lte"], datetime.fromisoformat(PERIOD["end"]))
        threads.filter.assert_called_once_with(exists.return_value)

    def test_linear_catalog_keeps_unselected_projects_and_manual_flags(self):
        connection = Obj(pk=3)
        user, organization = Obj(pk=2), Obj(pk=4)
        payload = {"projects": {"nodes": [{"id": "old-project", "name": "Historical", "updatedAt": "2025-01-01T00:00:00Z"}], "pageInfo": {"hasNextPage": False}}}
        with patch.object(external_connectors, "_latest_linear_connection", return_value=connection), patch.object(external_connectors, "_linear_graphql_request", return_value=payload), patch("startup_updates.models.LinearProjectSelection.objects") as rows:
            result = activity_scope.discover_activity_resources(user, organization, ["linear"])
        self.assertEqual(result, {"linear": ["old-project"]})
        self.assertNotIn("selected", rows.update_or_create.call_args.kwargs["defaults"])
        self.assertEqual(rows.update_or_create.call_args.kwargs["connection"], connection)

    def test_catalog_failure_returns_actionable_client_error(self):
        from community_chat.startups.views import GenerateView
        with patch("vibe_raising.views.VibeRaisingEmailDraftStartView.post", side_effect=external_connectors.ConnectorConfigurationError("Catalog did not finish.")), self.assertRaises(ValidationError) as caught:
            GenerateView().post(Obj(data={"inputSources": ["slack"]}))
        self.assertIn("Catalog did not finish.", str(caught.exception.detail))


class CachedSourceReplayTests(SimpleTestCase):
    def test_two_new_rolling_drafts_can_reuse_recent_cached_items(self):
        scoped_artifacts = MagicMock()
        first = {"activity_window_days": 30, "narrative_period": PERIOD, "update_id": 1}
        prepared = activity_scope.prepare_activity_classification(first, "slack", scoped_artifacts)
        activity_scope.prepare_activity_classification(prepared, "slack", scoped_artifacts)
        self.assertEqual(scoped_artifacts.update.call_count, 1)
        second = {**first, "update_id": 2}
        activity_scope.prepare_activity_classification(second, "slack", scoped_artifacts)
        self.assertEqual(scoped_artifacts.update.call_count, 2)
        self.assertEqual(scoped_artifacts.update.call_args.kwargs["extraction_status"], "hydrated")
        self.assertNotIn("cleaned_text", scoped_artifacts.update.call_args.kwargs)
        self.assertNotIn("message_payloads", scoped_artifacts.update.call_args.kwargs)

    def test_historical_linear_child_activity_is_kept_even_if_project_was_edited_later(self):
        project = MagicMock(raw_payload={"updatedAt": "2026-10-01T00:00:00Z"})
        project.issues.filter.return_value.exists.return_value = True
        self.assertTrue(activity_scope.linear_project_has_activity(project, PERIOD))
        self.assertEqual(project.issues.filter.call_args.kwargs["updated_at_linear__lt"], datetime.fromisoformat(PERIOD["end"]))
        project.issues.filter.return_value.exists.return_value = False
        project.project_updates.filter.return_value.exists.return_value = False
        self.assertFalse(activity_scope.linear_project_has_activity(project, PERIOD))
