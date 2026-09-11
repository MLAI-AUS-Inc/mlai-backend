"""DRF contract tests with a mocked domain service; no DB or migrations.

Run: .venv/bin/python -m unittest community_chat.tests.test_coworking_unit
SQL locking and account-token verification belong to the existing integration
suites. These tests use dummy database settings and never load .env.
"""
from datetime import datetime, timezone as datetime_timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="coworking-unit-only", USE_TZ=True, USE_I18N=False,
        DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
        REST_FRAMEWORK={"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": []},
    )

from rest_framework.authentication import BaseAuthentication
from rest_framework.test import APIRequestFactory, force_authenticate


class NoSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        return None

    def authenticate_header(self, request):
        return "Bearer"


class InsufficientBalanceError(Exception):
    pass


class CoworkingTodayTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.model = Mock()
        self.model.objects.filter.return_value.first.return_value = None
        self.service.get_coworking_cost.return_value = 8
        self.booking = SimpleNamespace(pk="booking-123", points_cost=4)
        self.service.book.return_value = (self.booking, True)
        self.user = SimpleNamespace(pk="member-123", slack_id="UMEMBER", is_authenticated=True)
        modules = {}
        for name, attrs in {
            "roo.models": {"CoworkingBooking": self.model},
            "roo.services": {"CoworkingService": self.service},
            "roo.permissions": {"InsufficientBalanceError": InsufficientBalanceError},
            "community_chat.authentication": {"CommunityChatAccountAuthentication": NoSessionAuthentication},
        }.items():
            module = ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(
                "community_chat._coworking_test_view",
                Path(__file__).resolve().parents[1] / "coworking_views.py",
            )
            self.views = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.views)
        # Keep real DRF dispatch/permissions; disable only the cache throttle.
        self.view = self.views.CoworkingTodayView.as_view(throttle_classes=[])
        self.factory = APIRequestFactory()
        self.clock = patch.object(self.views.timezone, "now", return_value=datetime(
            2026, 9, 10, 23, 30, tzinfo=datetime_timezone.utc))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def request(self, method="get", data=None, authenticated=True):
        request = getattr(self.factory, method)("/coworking/today/", data or {}, format="json")
        if authenticated:
            force_authenticate(request, user=self.user)
        return self.view(request)

    def test_read_is_owner_scoped_and_uses_melbourne_day(self):
        response = self.request(data={"user_id": "someone-else"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "date": "2026-09-11", "status": "available", "booking_id": None,
            "points_cost": 8, "resets_at": "2026-09-12T00:00:00+10:00",
        })
        self.assertEqual(self.model.objects.filter.call_args.kwargs["user"], self.user)
        self.assertEqual(str(self.model.objects.filter.call_args.kwargs["date"]), "2026-09-11")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.service.book.assert_not_called()

    def test_existing_booking_is_restored_without_charging(self):
        self.model.objects.filter.return_value.first.return_value = self.booking
        response = self.request()
        self.assertEqual(response.data["status"], "booked")
        self.assertEqual(response.data["points_cost"], 4)
        self.service.get_coworking_cost.assert_not_called()
        self.service.book.assert_not_called()

    def test_book_uses_only_authenticated_identity_and_returns_receipt(self):
        response = self.request("post", {
            "date": "2026-09-11", "slack_user_id": "UOTHER", "user_id": "other",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["booking_id"], "booking-123")
        self.assertEqual(response.data["status"], "booked")
        args = self.service.book.call_args.kwargs
        self.assertEqual(args["user"], self.user)
        self.assertEqual(args["created_by_slack_id"], "UMEMBER")
        self.assertEqual(str(args["booking_date"]), "2026-09-11")

    def test_duplicate_service_receipt_is_success(self):
        self.service.book.return_value = (self.booking, False)
        response = self.request("post", {"date": "2026-09-11"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["booking_id"], "booking-123")

    def test_no_slack_link_is_required_for_account_booking(self):
        self.user.slack_id = None
        self.assertEqual(self.request("post", {"date": "2026-09-11"}).status_code, 201)
        self.assertEqual(self.service.book.call_args.kwargs["created_by_slack_id"], "")

    def test_failure_never_returns_booked(self):
        for failure, code in [(InsufficientBalanceError(), "insufficient_points"),
                              (ValueError("No availability"), "booking_unavailable")]:
            with self.subTest(code=code):
                self.service.book.side_effect = failure
                response = self.request("post", {"date": "2026-09-11"})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], code)
                self.assertNotIn("booking_id", response.data)

    def test_old_missing_or_future_dates_cannot_book(self):
        for day in [None, "2026-09-10", "2026-09-12", "invalid"]:
            with self.subTest(day=day):
                response = self.request("post", {"date": day})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["code"], "booking_date_changed")
        self.service.book.assert_not_called()

    def test_daylight_saving_reset_is_local_midnight(self):
        self.clock.return_value = datetime(2026, 10, 3, 15, 30, tzinfo=datetime_timezone.utc)
        with patch.object(self.views.timezone, "now", return_value=self.clock.return_value):
            response = self.request()
        self.assertEqual(response.data["date"], "2026-10-04")
        self.assertEqual(response.data["resets_at"], "2026-10-05T00:00:00+11:00")

    def test_anonymous_requests_cannot_read_or_book(self):
        for method in ["get", "post"]:
            response = self.request(method, {"date": "2026-09-11"}, authenticated=False)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response["Cache-Control"], "private, no-store")
        self.service.book.assert_not_called()
        self.model.objects.filter.assert_not_called()
