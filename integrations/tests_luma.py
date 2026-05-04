from __future__ import annotations

import base64
import csv
import io
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from integrations.services.luma import LumaAttendeeReportService
from roo.models import PointsAdmin


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, headers, params, timeout):
        call = {
            "path": urlparse(url).path,
            "headers": dict(headers),
            "params": dict(params),
            "timeout": timeout,
        }
        self.calls.append(call)
        result = self.handler(call["path"], call["params"])
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(result)


class LumaAttendeeReportServiceTests(SimpleTestCase):
    def test_paginates_events_and_guests(self):
        def handler(path, params):
            if path == "/v1/calendar/list-events":
                if params.get("pagination_cursor") == "page-2":
                    return {
                        "entries": [
                            {
                                "id": "evt-2",
                                "name": "Second",
                                "start_at": "2026-04-20T08:00:00Z",
                                "end_at": "2026-04-20T10:00:00Z",
                            }
                        ],
                        "has_more": False,
                    }
                return {
                    "entries": [
                        {
                            "id": "future",
                            "name": "Future",
                            "start_at": "2026-05-05T08:00:00Z",
                            "end_at": "2026-05-05T10:00:00Z",
                        },
                        {
                            "id": "evt-1",
                            "name": "First",
                            "start_at": "2026-04-29T08:00:00Z",
                            "end_at": "2026-04-29T10:00:00Z",
                        },
                    ],
                    "has_more": True,
                    "next_cursor": "page-2",
                }
            if path == "/v1/event/get-guests":
                if params.get("pagination_cursor") == "guest-page-2":
                    return {"entries": [{"guest": {"id": "gst-2"}}], "has_more": False}
                return {
                    "entries": [{"guest": {"id": "gst-1"}}],
                    "has_more": True,
                    "next_cursor": "guest-page-2",
                }
            raise AssertionError(path)

        session = FakeSession(handler)
        service = LumaAttendeeReportService(api_key="key", base_url="https://luma.test", session=session)
        now = datetime(2026, 5, 4, 12, 0, tzinfo=ZoneInfo("Australia/Melbourne"))

        events = service.get_recent_ended_events(count=2, now=now)
        guests = service.list_guests(event_id="evt-1")

        self.assertEqual([event["id"] for event in events], ["evt-1", "evt-2"])
        self.assertEqual([guest["guest"]["id"] for guest in guests], ["gst-1", "gst-2"])
        self.assertEqual(session.calls[0]["headers"]["x-luma-api-key"], "key")
        self.assertTrue(
            any(
                call["path"] == "/v1/event/get-guests"
                and call["params"].get("pagination_cursor") == "guest-page-2"
                for call in session.calls
            )
        )

    def test_selects_ended_events_for_melbourne_date(self):
        def handler(path, params):
            self.assertEqual(path, "/v1/calendar/list-events")
            return {
                "entries": [
                    {
                        "id": "evt-target",
                        "name": "April 29 Event",
                        "start_at": "2026-04-28T23:30:00Z",
                        "end_at": "2026-04-29T02:00:00Z",
                    },
                    {
                        "id": "evt-old",
                        "name": "April 28 Event",
                        "start_at": "2026-04-28T08:00:00Z",
                        "end_at": "2026-04-28T10:00:00Z",
                    },
                ],
                "has_more": False,
            }

        service = LumaAttendeeReportService(
            api_key="key",
            base_url="https://luma.test",
            session=FakeSession(handler),
        )
        now = datetime(2026, 5, 4, 12, 0, tzinfo=ZoneInfo("Australia/Melbourne"))

        events = service.get_ended_events_for_date(date(2026, 4, 29), count=1, now=now)

        self.assertEqual([event["id"] for event in events], ["evt-target"])

    def test_builds_report_summary_and_base64_csv(self):
        def handler(path, params):
            if path == "/v1/calendar/list-events":
                return {
                    "entries": [
                        {
                            "id": "evt-1",
                            "name": "MLAI Demo Night",
                            "url": "https://luma.test/demo",
                            "start_at": "2026-04-29T08:00:00Z",
                            "end_at": "2026-04-29T10:00:00Z",
                        }
                    ],
                    "has_more": False,
                }
            if path == "/v1/event/get-guests":
                return {
                    "entries": [
                        {
                            "guest": {
                                "id": "gst-1",
                                "user_name": "Ada Lovelace",
                                "user_email": "ada@example.com",
                                "approval_status": "approved",
                                "registered_at": "2026-04-28T01:00:00Z",
                                "event_tickets": [
                                    {
                                        "id": "ticket-1",
                                        "name": "General",
                                        "checked_in_at": "2026-04-29T08:45:00Z",
                                    }
                                ],
                                "registration_answers": [
                                    {"label": "Dietary", "answer": ["Vegetarian", "No nuts"]},
                                    {
                                        "label": "Company",
                                        "question_type": "company",
                                        "answer_company": "Analytical Engines",
                                        "answer_job_title": "Founder",
                                    },
                                ],
                            }
                        }
                    ],
                    "has_more": False,
                }
            raise AssertionError(path)

        service = LumaAttendeeReportService(
            api_key="key",
            base_url="https://luma.test",
            session=FakeSession(handler),
        )
        report = service.build_attendee_report(event_count=1, include_csv=True)

        event = report["events"][0]
        self.assertEqual(report["total_guest_count"], 1)
        self.assertEqual(event["guest_count"], 1)
        self.assertEqual(event["checked_in_count"], 1)
        self.assertEqual(event["csv"]["filename"], "luma-mlai-2026-04-29-mlai-demo-night.csv")

        csv_content = base64.b64decode(event["csv"]["content_base64"]).decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(csv_content)))
        self.assertEqual(rows[0]["email"], "ada@example.com")
        self.assertEqual(rows[0]["ticket_names"], "General")
        self.assertEqual(rows[0]["question: Dietary"], "Vegetarian; No nuts")
        self.assertEqual(rows[0]["question: Company"], "Analytical Engines - Founder")


class LumaAttendeeReportViewTests(APITestCase):
    def setUp(self):
        self.url = reverse("luma_attendee_report")
        self.admin_slack_id = "ULUMAADMIN"
        self.committee_slack_id = "ULUMACOMMITTEE"
        self.partner_slack_id = "ULUMAPARTNER"
        self.portfolio_slack_id = "ULUMAPORTFOLIO"
        self.inactive_slack_id = "ULUMAINACTIVE"
        PointsAdmin.objects.create(slack_user_id=self.admin_slack_id, role="admin", is_active=True)
        PointsAdmin.objects.create(slack_user_id=self.committee_slack_id, role="committee", is_active=True)
        PointsAdmin.objects.create(slack_user_id=self.partner_slack_id, role="partner", is_active=True)
        PointsAdmin.objects.create(slack_user_id=self.portfolio_slack_id, role="portfolio_lead", is_active=True)
        PointsAdmin.objects.create(slack_user_id=self.inactive_slack_id, role="admin", is_active=False)

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_allowed_roles_can_get_luma_report(self, mock_permission):
        captured = []

        class FakeService:
            def build_attendee_report(self, **kwargs):
                captured.append(kwargs)
                return {"events": [], "total_guest_count": 0}

        with patch("integrations.api_views_luma.LumaAttendeeReportService", return_value=FakeService()):
            for slack_id in [self.admin_slack_id, self.committee_slack_id, self.partner_slack_id]:
                response = self.client.get(self.url, {"slack_user_id": slack_id})
                self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(captured), 3)
        self.assertTrue(all(call["include_csv"] is False for call in captured))

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_denied_roles_do_not_call_luma(self, mock_permission):
        fake_service = SimpleNamespace(build_attendee_report=lambda **kwargs: self.fail("should not call Luma"))

        with patch("integrations.api_views_luma.LumaAttendeeReportService", return_value=fake_service):
            for slack_id in ["UNOTADMIN", self.portfolio_slack_id, self.inactive_slack_id]:
                response = self.client.get(self.url, {"slack_user_id": slack_id})
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(LUMA_API_KEY="")
    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_missing_luma_key_returns_configuration_error(self, mock_permission):
        response = self.client.get(self.url, {"slack_user_id": self.admin_slack_id})

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("LUMA_API_KEY", response.data["error"])

    @patch("core.permissions.HasRooApiKey.has_permission", return_value=True)
    def test_query_params_are_passed_to_service(self, mock_permission):
        captured = []

        class FakeService:
            def build_attendee_report(self, **kwargs):
                captured.append(kwargs)
                return {"events": [], "total_guest_count": 0}

        with patch("integrations.api_views_luma.LumaAttendeeReportService", return_value=FakeService()):
            response = self.client.get(
                self.url,
                {
                    "slack_user_id": self.admin_slack_id,
                    "event_count": "99",
                    "event_date": "2026-04-29",
                    "approval_status": "approved",
                    "include_csv": "true",
                },
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(captured[0]["event_count"], 10)
        self.assertEqual(captured[0]["event_date"], date(2026, 4, 29))
        self.assertEqual(captured[0]["approval_status"], "approved")
        self.assertTrue(captured[0]["include_csv"])
