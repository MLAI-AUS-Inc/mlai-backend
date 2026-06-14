from __future__ import annotations

from datetime import datetime
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
from startup_updates.models import StartupMetricObservation

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
                counts = {"p1": 2, "p2": 3}
                event_id = params.get("event_id")
                return {"entries": [{"guest": {"id": f"{event_id}-{i}"}} for i in range(counts.get(event_id, 0))], "has_more": False}
            raise AssertionError(path)

        now = datetime(2026, 5, 1, 12, 0, tzinfo=MELB)
        results = self._service(handler).collect_ended_event_registrations(now=now)

        by_id = {item["event"]["id"]: item for item in results}
        self.assertEqual(set(by_id), {"p1", "p2"})  # future excluded
        self.assertEqual(by_id["p1"]["registration_count"], 2)
        self.assertEqual(by_id["p2"]["registration_count"], 3)

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
        results = self._service(handler).collect_ended_event_registrations(now=now)

        self.assertEqual({item["event"]["id"] for item in results}, {"p1", "p2"})


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

        # Still exactly 2 metrics per month (no duplicate rows).
        self.assertEqual(
            StartupMetricObservation.objects.filter(organization=self.org, source_provider=LUMA_METRIC_SOURCE).count(),
            4,
        )
        march = date(2026, 3, 1)
        self.assertEqual(self._metric("eventsRun", march).value_number, Decimal("3"))
        self.assertEqual(self._metric("eventRegistrations", march).value_number, Decimal("22"))


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
            collect_ended_event_registrations=lambda **kwargs: [
                {"event": {"id": "m1", "name": "March"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 12},
            ]
        )
        connection = self._connection(self.org)

        result = sync_luma_connection(connection)

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["eventsSynced"], 1)
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
            collect_ended_event_registrations=lambda **kwargs: [
                {"event": {"id": "m1"}, "start_at": datetime(2026, 3, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 4},
                {"event": {"id": "a1"}, "start_at": datetime(2026, 4, 5, 3, 0, tzinfo=ZoneInfo("UTC")), "registration_count": 9},
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
