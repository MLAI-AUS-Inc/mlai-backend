from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from organizations.models import Organization
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.luma import LumaAPIError, LumaAttendeeReportService
from integrations.services.luma_sync import (
    LUMA_METRIC_SOURCE,
    publish_luma_event_metrics,
    sync_luma_connection,
)
from integrations.tests_luma import FakeSession
from startup_updates.models import LumaEventSelection, StartupMetricObservation
from startup_updates.services import (
    merge_luma_metrics_into_structured_memo,
    normalize_startup_update_input_sources,
)

User = get_user_model()

MELB = ZoneInfo("Australia/Melbourne")


def _event(event_id: str, *, start_at: str, end_at: str | None = None, name: str = "") -> dict:
    payload = {"id": event_id, "name": name or event_id, "start_at": start_at}
    if end_at is not None:
        payload["end_at"] = end_at
    return payload


class CollectEndedEventRegistrationsTests(TestCase):
    """LumaAttendeeReportService.collect_ended_event_registrations (no DB needed)."""

    def _service(self, handler):
        return LumaAttendeeReportService(api_key="key", base_url="https://luma.test", session=FakeSession(handler))

    def test_excludes_future_events_and_counts_registrations(self):
        def handler(path, params):
            if path == "/v1/calendar/list-events":
                return {
                    "entries": [
                        # Future event — must be skipped.
                        _event("future", start_at="2026-06-01T03:00:00Z", end_at="2026-06-01T05:00:00Z"),
                        # Past event with an end time.
                        _event("p1", start_at="2026-03-15T03:00:00Z", end_at="2026-03-15T05:00:00Z"),
                        # Past event missing end_at — included on start_at.
                        _event("p2", start_at="2026-04-10T03:00:00Z"),
                    ],
                    "has_more": False,
                }
            if path == "/v1/event/get-guests":
                rosters = {
                    # 2 registered, 1 checked in.
                    "p1": [
                        {"guest": {"id": "p1-0", "checked_in_at": "2026-03-15T05:00:00Z"}},
                        {"guest": {"id": "p1-1"}},
                    ],
                    # 3 registered, none checked in.
                    "p2": [{"guest": {"id": f"p2-{i}"}} for i in range(3)],
                }
                return {"entries": rosters.get(params.get("event_id"), []), "has_more": False}
            raise AssertionError(path)

        now = datetime(2026, 5, 1, 12, 0, tzinfo=MELB)
        results = self._service(handler).collect_ended_event_attendance(now=now)

        by_id = {item["event"]["id"]: item for item in results}
        self.assertEqual(set(by_id), {"p1", "p2"})  # future excluded
        self.assertEqual(by_id["p1"]["registration_count"], 2)
        self.assertEqual(by_id["p1"]["checked_in_count"], 1)
        self.assertEqual(by_id["p2"]["registration_count"], 3)
        self.assertEqual(by_id["p2"]["checked_in_count"], 0)

    def test_paginates_full_calendar(self):
        def handler(path, params):
            if path == "/v1/calendar/list-events":
                if params.get("pagination_cursor") == "page-2":
                    return {"entries": [_event("p2", start_at="2026-02-10T03:00:00Z", end_at="2026-02-10T05:00:00Z")], "has_more": False}
                return {
                    "entries": [_event("p1", start_at="2026-03-10T03:00:00Z", end_at="2026-03-10T05:00:00Z")],
                    "has_more": True,
                    "next_cursor": "page-2",
                }
            if path == "/v1/event/get-guests":
                return {"entries": [{"guest": {"id": "g"}}], "has_more": False}
            raise AssertionError(path)

        now = datetime(2026, 5, 1, 12, 0, tzinfo=MELB)
        results = self._service(handler).collect_ended_event_attendance(now=now)

        self.assertEqual({item["event"]["id"] for item in results}, {"p1", "p2"})

    def test_event_ids_filter_limits_guest_fetch(self):
        guest_calls: list[str] = []

        def handler(path, params):
            if path == "/v1/calendar/list-events":
                return {
                    "entries": [
                        _event("p1", start_at="2026-03-10T03:00:00Z", end_at="2026-03-10T05:00:00Z"),
                        _event("p2", start_at="2026-03-12T03:00:00Z", end_at="2026-03-12T05:00:00Z"),
                    ],
                    "has_more": False,
                }
            if path == "/v1/event/get-guests":
                guest_calls.append(params.get("event_id"))
                return {"entries": [{"guest": {"id": "g"}}], "has_more": False}
            raise AssertionError(path)

        now = datetime(2026, 5, 1, 12, 0, tzinfo=MELB)
        results = self._service(handler).collect_ended_event_attendance(now=now, event_ids=["p1"])

        self.assertEqual({item["event"]["id"] for item in results}, {"p1"})
        self.assertEqual(guest_calls, ["p1"])  # no guest fetch for the unselected event

    def test_list_ended_events_returns_page(self):
        def handler(path, params):
            self.assertEqual(path, "/v1/calendar/list-events")
            return {
                "entries": [
                    _event("future", start_at="2026-06-01T03:00:00Z", end_at="2026-06-01T05:00:00Z"),
                    _event("p1", start_at="2026-03-10T03:00:00Z", end_at="2026-03-10T05:00:00Z"),
                ],
                "has_more": True,
                "next_cursor": "next",
            }

        now = datetime(2026, 5, 1, 12, 0, tzinfo=MELB)
        page = self._service(handler).list_ended_events(now=now, limit=10)

        self.assertEqual([e["id"] for e in page["events"]], ["p1"])  # future excluded, no guest fetch
        self.assertEqual(page["next_cursor"], "next")
        self.assertTrue(page["has_more"])


class PublishLumaEventMetricsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", domain="acme.com")

    def _events(self):
        return [
            {"event": {"id": "m1", "name": "March A"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 10},
            {"event": {"id": "m2", "name": "March B"}, "start_at": datetime(2026, 3, 20, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 5},
            {"event": {"id": "a1", "name": "April A"}, "start_at": datetime(2026, 4, 2, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 8},
        ]

    def _metric(self, metric_key, month):
        return StartupMetricObservation.objects.get(
            organization=self.org,
            source_provider=LUMA_METRIC_SOURCE,
            metric_key=metric_key,
            period_month=month,
        )

    def test_buckets_by_month(self):
        from datetime import date

        publish_luma_event_metrics(organization=self.org, events=self._events())

        march = date(2026, 3, 1)
        april = date(2026, 4, 1)
        self.assertEqual(self._metric("eventsRun", march).value_number, Decimal("2"))
        self.assertEqual(self._metric("eventRegistrations", march).value_number, Decimal("15"))
        self.assertEqual(self._metric("eventsRun", april).value_number, Decimal("1"))
        self.assertEqual(self._metric("eventRegistrations", april).value_number, Decimal("8"))
        # Evidence + display fields.
        march_run = self._metric("eventsRun", march)
        self.assertEqual(march_run.value_text, "2")
        self.assertEqual(march_run.metric_name, "Events Run")
        self.assertEqual(march_run.confidence, 1.0)
        self.assertEqual(set(march_run.source_record_ids), {"m1", "m2"})

    def test_reruns_upsert_without_duplicates(self):
        from datetime import date

        publish_luma_event_metrics(organization=self.org, events=self._events())
        # Re-run with an extra March event and more registrations.
        events = self._events() + [
            {"event": {"id": "m3", "name": "March C"}, "start_at": datetime(2026, 3, 25, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 7},
        ]
        publish_luma_event_metrics(organization=self.org, events=events)

        # All four metrics per month, upserted (no duplicate rows): 2 months x 4.
        self.assertEqual(
            StartupMetricObservation.objects.filter(organization=self.org, source_provider=LUMA_METRIC_SOURCE).count(),
            8,
        )
        march = date(2026, 3, 1)
        self.assertEqual(self._metric("eventsRun", march).value_number, Decimal("3"))
        self.assertEqual(self._metric("eventRegistrations", march).value_number, Decimal("22"))

    def test_computes_attendees_and_check_in_rate(self):
        from datetime import date

        events = [
            {"event": {"id": "m1", "name": "March A"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 10, "checked_in_count": 4},
            {"event": {"id": "m2", "name": "March B"}, "start_at": datetime(2026, 3, 20, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 10, "checked_in_count": 5},
        ]
        publish_luma_event_metrics(organization=self.org, events=events)

        march = date(2026, 3, 1)
        self.assertEqual(self._metric("eventAttendees", march).value_number, Decimal("9"))
        rate = self._metric("eventCheckInRate", march)
        self.assertEqual(rate.value_number, Decimal("45.0"))  # 9 / 20 * 100
        self.assertEqual(rate.unit, "%")

    def test_selected_metrics_only_writes_chosen(self):
        publish_luma_event_metrics(
            organization=self.org,
            events=self._events(),
            selected_metrics=["eventsRun"],
        )
        self.assertEqual(
            StartupMetricObservation.objects.filter(organization=self.org, source_provider=LUMA_METRIC_SOURCE).count(),
            2,  # eventsRun for two months only
        )
        self.assertFalse(
            StartupMetricObservation.objects.filter(
                organization=self.org, source_provider=LUMA_METRIC_SOURCE, metric_key="eventRegistrations"
            ).exists()
        )


class SyncLumaConnectionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", domain="acme.com")
        self.user = User.objects.create_user(email="founder@example.com", role="participant")

    def _connection(self, organization):
        return ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.LUMA,
            organization=organization,
            access_token="luma-secret",
            account_label="Luma",
            external_account_id="founder@example.com",
            status=ExternalServiceConnectionStatus.CONNECTED,
        )

    @patch("integrations.services.luma_sync.LumaAttendeeReportService")
    def test_sync_writes_metrics_and_updates_connection(self, mock_service_cls):
        mock_service_cls.return_value = SimpleNamespace(
            collect_ended_event_attendance=lambda **kwargs: [
                {"event": {"id": "m1", "name": "March"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 12, "checked_in_count": 9},
            ]
        )
        connection = self._connection(self.org)

        result = sync_luma_connection(connection)

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["eventsSynced"], 1)
        self.assertEqual(result["catalogEventsSynced"], 1)
        # Constructed with the founder's own key.
        mock_service_cls.assert_called_once_with(api_key="luma-secret")
        connection.refresh_from_db()
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.CONNECTED)
        self.assertIsNotNone(connection.last_synced_at)
        self.assertEqual(connection.sync_cursor["luma_events_synced"], 1)
        self.assertTrue(
            StartupMetricObservation.objects.filter(
                organization=self.org, source_provider="luma", metric_key="eventRegistrations"
            ).exists()
        )
        event = LumaEventSelection.objects.get(connection=connection, event_id="m1")
        self.assertEqual(event.event_name, "March")
        self.assertEqual(event.start_at, datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")))
        self.assertFalse(event.selected)

    def test_sync_requires_linked_organization(self):
        from integrations.services.luma import LumaConfigurationError

        connection = self._connection(None)
        with self.assertRaises(LumaConfigurationError):
            sync_luma_connection(connection)


class LumaConnectEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.user)
        self.connect_url = "/api/v1/integrations/luma/connect"
        self.sync_url = "/api/v1/integrations/sources/sync"

    @patch("integrations.services.external_connectors.resolve_connector_organization")
    @patch("integrations.services.external_connectors.LumaAttendeeReportService")
    def test_connect_stores_encrypted_key(self, mock_service_cls, mock_resolve):
        org = Organization.objects.create(name="Acme", domain="acme.com")
        mock_resolve.return_value = org
        mock_service_cls.return_value = SimpleNamespace(get_recent_ended_events=lambda **kwargs: [])

        response = self.api_client.post(self.connect_url, {"apiKey": "luma-secret"}, format="json")

        self.assertEqual(response.status_code, 200)
        connection = ExternalServiceConnection.objects.get(user=self.user, provider=ExternalServiceProvider.LUMA)
        self.assertEqual(connection.access_token, "luma-secret")  # round-trips through EncryptedTextField
        self.assertEqual(connection.organization, org)
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.CONNECTED)
        sources = {source["key"]: source for source in response.data["sources"]}
        self.assertEqual(sources["luma"]["status"], "connected")

    def test_connect_requires_api_key(self):
        response = self.api_client.post(self.connect_url, {}, format="json")
        self.assertEqual(response.status_code, 400)

    @patch("integrations.services.external_connectors.LumaAttendeeReportService")
    def test_connect_rejects_invalid_key(self, mock_service_cls):
        def boom(**kwargs):
            raise LumaAPIError("rejected", status_code=401)

        mock_service_cls.return_value = SimpleNamespace(get_recent_ended_events=boom)

        response = self.api_client.post(self.connect_url, {"apiKey": "bad-key"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            ExternalServiceConnection.objects.filter(user=self.user, provider=ExternalServiceProvider.LUMA).exists()
        )

    @patch("integrations.services.luma_sync.LumaAttendeeReportService")
    def test_sync_dispatch_writes_metrics(self, mock_service_cls):
        org = Organization.objects.create(name="Acme", domain="acme.com")
        ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.LUMA,
            organization=org,
            access_token="luma-secret",
            account_label="Luma",
            external_account_id="founder@example.com",
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        mock_service_cls.return_value = SimpleNamespace(
            collect_ended_event_attendance=lambda **kwargs: [
                {"event": {"id": "m1"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 4, "checked_in_count": 2},
                {"event": {"id": "a1"}, "start_at": datetime(2026, 4, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 9, "checked_in_count": 3},
            ]
        )

        response = self.api_client.post(self.sync_url, {"providers": ["luma"]}, format="json")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            StartupMetricObservation.objects.filter(
                organization=org, source_provider="luma", metric_key="eventsRun"
            ).count(),
            2,  # one row per month
        )

    @patch("integrations.services.external_connectors.resolve_connector_organization")
    @patch("integrations.services.external_connectors.LumaAttendeeReportService")
    def test_disconnect_clears_token(self, mock_service_cls, mock_resolve):
        mock_resolve.return_value = Organization.objects.create(name="Acme", domain="acme.com")
        mock_service_cls.return_value = SimpleNamespace(get_recent_ended_events=lambda **kwargs: [])
        self.api_client.post(self.connect_url, {"apiKey": "luma-secret"}, format="json")
        connection = ExternalServiceConnection.objects.get(user=self.user, provider=ExternalServiceProvider.LUMA)

        response = self.api_client.delete(f"/api/v1/integrations/sources/connections/{connection.id}")

        self.assertEqual(response.status_code, 200)
        connection.refresh_from_db()
        self.assertEqual(connection.status, ExternalServiceConnectionStatus.DISCONNECTED)
        self.assertEqual(connection.access_token, "")


class LumaSelectionEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="founder@example.com", role="participant")
        self.org = Organization.objects.create(name="Acme", domain="acme.com")
        self.connection = ExternalServiceConnection.objects.create(
            user=self.user,
            provider=ExternalServiceProvider.LUMA,
            organization=self.org,
            access_token="luma-secret",
            account_label="Luma",
            external_account_id="founder@example.com",
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        self.api_client = APIClient()
        self.api_client.force_authenticate(self.user)

    @patch("integrations.services.external_connectors.LumaAttendeeReportService")
    def test_events_list_upserts_selections_and_returns_metrics(self, mock_service_cls):
        mock_service_cls.return_value = SimpleNamespace(
            list_ended_events=lambda **kwargs: {
                "events": [
                    {"id": "e1", "name": "Event 1", "url": "https://luma.test/e1", "start_at": "2026-03-10T03:00:00Z"},
                    {"id": "e2", "name": "Event 2", "start_at": "2026-04-10T03:00:00Z"},
                ],
                "has_more": False,
                "next_cursor": None,
            }
        )

        response = self.api_client.get("/api/v1/integrations/luma/events")

        self.assertEqual(response.status_code, 200)
        self.assertEqual({event["eventId"] for event in response.data["events"]}, {"e1", "e2"})
        self.assertEqual(
            {metric["key"] for metric in response.data["availableMetrics"]},
            {"eventsRun", "eventRegistrations", "eventAttendees", "eventCheckInRate"},
        )
        self.assertEqual(LumaEventSelection.objects.filter(connection=self.connection).count(), 2)

    def test_save_selections_persists_events_and_metrics(self):
        for event_id in ("e1", "e2", "e3"):
            LumaEventSelection.objects.create(
                connection=self.connection, user=self.user, organization=self.org,
                event_id=event_id, event_name=event_id,
            )

        response = self.api_client.post(
            "/api/v1/integrations/luma/selections",
            {"eventIds": ["e1", "e3"], "metrics": ["eventsRun", "eventAttendees"]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["selectedEventCount"], 2)
        self.assertEqual(
            set(
                LumaEventSelection.objects.filter(connection=self.connection, selected=True)
                .values_list("event_id", flat=True)
            ),
            {"e1", "e3"},
        )
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.provider_metadata["selected_metrics"], ["eventsRun", "eventAttendees"])
        status_resp = self.api_client.get("/api/v1/integrations/sources/status")
        sources = {source["key"]: source for source in status_resp.data["sources"]}
        self.assertEqual(sources["luma"]["selectedEventCount"], 2)
        self.assertEqual(sources["luma"]["status"], "connected")

    @patch("integrations.services.luma_sync.LumaAttendeeReportService")
    def test_sync_respects_selected_events_and_metrics(self, mock_service_cls):
        LumaEventSelection.objects.create(
            connection=self.connection, user=self.user, organization=self.org,
            event_id="e1", event_name="E1", selected=True,
        )
        self.connection.provider_metadata = {"selected_metrics": ["eventsRun"]}
        self.connection.save(update_fields=["provider_metadata"])

        captured = {}

        def _collect(**kwargs):
            captured["event_ids"] = kwargs.get("event_ids")
            return [
                {"event": {"id": "e1", "name": "E1"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 5, "checked_in_count": 2},
            ]

        mock_service_cls.return_value = SimpleNamespace(collect_ended_event_attendance=_collect)

        sync_luma_connection(self.connection)

        self.assertEqual(set(captured["event_ids"]), {"e1"})  # only the selected event is fetched
        keys = set(
            StartupMetricObservation.objects.filter(organization=self.org, source_provider="luma")
            .values_list("metric_key", flat=True)
        )
        self.assertEqual(keys, {"eventsRun"})  # only the selected metric is written

    @patch("integrations.services.luma_sync.LumaAttendeeReportService")
    def test_sync_removes_stale_metrics(self, mock_service_cls):
        StartupMetricObservation.objects.create(
            organization=self.org, metric_key="eventRegistrations", metric_name="Event Registrations",
            value_text="99", value_number=Decimal("99"), period_month=date(2026, 1, 1),
            source_provider="luma",
        )
        self.connection.provider_metadata = {"selected_metrics": ["eventsRun"]}
        self.connection.save(update_fields=["provider_metadata"])
        mock_service_cls.return_value = SimpleNamespace(
            collect_ended_event_attendance=lambda **kwargs: [
                {"event": {"id": "e1"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 5, "checked_in_count": 2},
            ]
        )

        sync_luma_connection(self.connection)

        self.assertFalse(
            StartupMetricObservation.objects.filter(
                organization=self.org, source_provider="luma", period_month=date(2026, 1, 1)
            ).exists()
        )
        self.assertEqual(
            set(
                StartupMetricObservation.objects.filter(organization=self.org, source_provider="luma")
                .values_list("metric_key", flat=True)
            ),
            {"eventsRun"},
        )


class LumaDraftMergeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Acme", domain="acme.com")

    def _metric(self, metric_key, value, *, unit="", month=date(2026, 3, 1)):
        return StartupMetricObservation.objects.create(
            organization=self.org,
            metric_key=metric_key,
            metric_name=metric_key,
            value_text=str(value),
            value_number=Decimal(str(value)),
            unit=unit,
            period_month=month,
            source_provider="luma",
        )

    def test_merge_injects_luma_metrics_into_kpi_snapshot(self):
        self._metric("eventsRun", 3)
        self._metric("eventCheckInRate", "75.0", unit="%")

        memo, ids = merge_luma_metrics_into_structured_memo(
            organization=self.org, month=date(2026, 3, 1), structured_memo={}
        )

        snapshot = {item["metric_key"]: item for item in memo["kpi_snapshot"]}
        self.assertEqual(set(snapshot), {"eventsRun", "eventCheckInRate"})
        self.assertEqual(snapshot["eventsRun"]["source_provider"], "luma")
        self.assertEqual(snapshot["eventsRun"]["label"], "Events Run")
        self.assertEqual(snapshot["eventCheckInRate"]["unit"], "%")
        self.assertTrue(ids)

    def test_merge_preserves_existing_snapshot_and_is_noop_without_luma(self):
        existing = {"kpi_snapshot": [{"metric_key": "mrr", "value": "$1"}]}
        memo, ids = merge_luma_metrics_into_structured_memo(
            organization=self.org, month=date(2026, 3, 1), structured_memo=existing
        )
        self.assertEqual(memo["kpi_snapshot"], [{"metric_key": "mrr", "value": "$1"}])
        self.assertEqual(ids, [])

    def test_luma_survives_input_source_allow_lists(self):
        from vibe_raising.views import VIBE_RAISING_INPUT_SOURCE_KEYS

        self.assertIn("luma", VIBE_RAISING_INPUT_SOURCE_KEYS)
        self.assertIn("luma", normalize_startup_update_input_sources(["gmail", "luma"]))
