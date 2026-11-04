"""Unit contract tests for the Chat meeting-room facade; no database required."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="meeting-room-unit-only",
        USE_TZ=True,
        USE_I18N=False,
        DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
        REST_FRAMEWORK={
            "UNAUTHENTICATED_USER": None,
            "DEFAULT_AUTHENTICATION_CLASSES": [],
        },
    )

from rest_framework.authentication import BaseAuthentication
from rest_framework.test import APIRequestFactory, force_authenticate


class NoSessionAuthentication(BaseAuthentication):
    def authenticate(self, request):
        return None

    def authenticate_header(self, request):
        return "Bearer"


class MeetingRoomError(Exception):
    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class InsufficientBalanceError(Exception):
    pass


class MeetingRoomFacadeTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.current_balance.return_value = 9
        self.service.serialize_booking.return_value = {"id": "booking-1"}
        self.booking = SimpleNamespace(points_cost=2)
        self.service.book.return_value = (self.booking, True)
        self.user = SimpleNamespace(
            pk="member-123", slack_id="UMEMBER", is_authenticated=True
        )
        modules = {}
        for name, attrs in {
            "roo.meeting_rooms": {
                "MeetingRoomError": MeetingRoomError,
                "MeetingRoomService": self.service,
            },
            "roo.permissions": {"InsufficientBalanceError": InsufficientBalanceError},
            "community_chat.authentication": {
                "CommunityChatAccountAuthentication": NoSessionAuthentication
            },
        }.items():
            module = ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(
                "community_chat._meeting_room_test_view",
                Path(__file__).resolve().parents[1] / "meeting_room_views.py",
            )
            self.views = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.views)
        self.factory = APIRequestFactory()

    def request(self, view, data=None, authenticated=True):
        request = self.factory.post("/meeting-rooms/", data or {}, format="json")
        if authenticated:
            force_authenticate(request, user=self.user)
        return view(request)

    def test_booking_uses_the_authenticated_member_not_client_identity(self):
        view = self.views.MeetingRoomBookView.as_view(throttle_classes=[])
        response = self.request(
            view,
            {
                "room_slug": "small-meeting-room",
                "starts_at": "2030-01-02T10:00:00+11:00",
                "ends_at": "2030-01-02T11:00:00+11:00",
                "client_request_id": "9810a8dc-e16b-4d44-b36f-1672688504c7",
                "confirmation_expires_at": "2030-01-02T09:59:00+11:00",
                "expected_points_cost": 2,
                "slack_user_id": "UOTHER",
                "target_slack_user_id": "UOTHER",
            },
        )
        self.assertEqual(response.status_code, 201)
        args = self.service.book.call_args.kwargs
        self.assertEqual(args["user"], self.user)
        self.assertEqual(args["requested_by_slack_id"], "UMEMBER")
        self.assertEqual(args["room_slug"], "small-meeting-room")
        self.assertEqual(args["expected_points_cost"], 2)
        self.assertEqual(
            args["starts_at"], datetime(2030, 1, 1, 23, tzinfo=timezone.utc)
        )
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_availability_cannot_mix_day_and_interval(self):
        view = self.views.MeetingRoomAvailabilityView.as_view(throttle_classes=[])
        response = self.request(
            view,
            {
                "room_slug": "small-meeting-room",
                "date": "2030-01-02",
                "starts_at": "2030-01-02T10:00:00+11:00",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "invalid_request")
        self.service.availability.assert_not_called()

    def test_anonymous_call_is_rejected_before_the_service(self):
        view = self.views.MyMeetingRoomBookingsView.as_view(throttle_classes=[])
        request = self.factory.get("/meeting-rooms/my-bookings/")
        response = view(request)
        self.assertEqual(response.status_code, 401)
        self.service.my_bookings.assert_not_called()
