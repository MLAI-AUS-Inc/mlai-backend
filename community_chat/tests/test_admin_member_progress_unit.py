"""Exercise the rank, booking and privacy reports without creating a database."""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import admin_member_progress as reporting
from community_chat.admin_member_progress_data import (
    Activity, Person, _manual_track, categories, coworking_activities,
)
from community_chat.admin_member_progress_views import AdminMemberProgressView, MemberProgressParameters
from core.models import User

URL = "/api/v1/community-chat/admin/member-progress/"
NOW = datetime(2026, 11, 23, 1, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)


def filters(**params):
    serializer = MemberProgressParameters(data=params)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def person(number, region, current=None, opening=None):
    return Person(number, {number}, str(UUID(int=number)), f"Member {number}", region, (current, opening))


class MemberProgressTest(SimpleTestCase):
    def test_reconciles_booking_days_in_calendar_and_region_windows(self):
        sam = person(1, "VIC", 25_000_000, 12_000_000)
        lee = person(2, "NSW", 22_300_000_000, 22_300_000_000)
        no_region = person(3, None)
        sam.activity.append(Activity(TODAY, "roo", "award", 150_000, "community"))
        coworking_activities({1: sam, 4: sam, 2: lee}, [
            (1, TODAY), (4, TODAY), (1, date(2026, 10, 31)), (2, TODAY), (2, date(2026, 11, 22)),
        ])
        with patch.object(reporting, "load_people", return_value=[sam, lee, no_region]):
            overview = reporting.member_progress(filters(month="2026-11"), NOW)
            location = reporting.member_progress(filters(view="locations", days=7), NOW)
        self.assertEqual(len(overview["levels"]), 201)
        self.assertEqual((overview["levels"][2], overview["levels"][200]), (1, 1))
        self.assertEqual(overview["summary"]["members"], 3)
        self.assertEqual(overview["booking_summary"], {"bookings": 4, "members": 2, "active_days": 3})
        november = overview["months"][-1]
        self.assertEqual(november["bookings"], 3)
        self.assertEqual(november["roo"], Decimal(".15"))
        self.assertEqual(november["tracks"]["community"]["roo"], Decimal(".15"))
        self.assertIsNone(november["badges"])
        self.assertEqual([row["bookings"] for row in overview["month_members"]], [1, 2])
        self.assertEqual(location["region"], "VIC")
        self.assertEqual(location["coverage"], dict(known_regions=2, not_provided=0, other_answers=1, total=3))
        self.assertEqual(location["booking_summary"], dict(bookings=1, members=1, active_days=1))
        self.assertEqual(location["summary"]["check_ins"], 0)

    def test_badge_category_mapping_and_multi_track_wallet(self):
        counts = {row["id"]: row["badge_count"] for row in categories()}
        self.assertEqual(sum(counts.values()), 50)
        self.assertEqual((counts["data-engineering"], counts["community"], counts["design-product"]), (10, 9, 5))
        self.assertEqual(_manual_track({"action_ids": ["tech.test", "data.evaluate"]}), "data-engineering")
        self.assertEqual(_manual_track({"action_ids": ["event.plan", "community.cover"]}), "multiple-tracks")
        self.assertEqual(_manual_track({"action_ids": ["other"]}), "unassigned")

    def test_leadership_uses_distinct_dated_badge_evidence_within_90_days(self):
        anchor = dict(id="event.series", awarded_on=date(2026, 9, 1))
        outcome = dict(id="event.handoff", awarded_on=TODAY)
        ready = person(1, "VIC")
        historical = person(2, "VIC")
        ready.badge_collection = [anchor, outcome]
        historical.badge_collection = [{**anchor, "awarded_on": None}, outcome]
        with patch.object(reporting, "load_people", return_value=[ready, historical]):
            report = reporting.member_progress(filters(view="leadership"), NOW)
        self.assertEqual([row["id"] for row in report["emerging"]], [ready.id])
        self.assertEqual(len(report["hard_earned"]), 2)
        self.assertEqual(report["undated_badges"], 1)

    def test_missing_rank_and_booking_only_activity_are_distinct(self):
        missing = person(1, "UNKNOWN")
        coworking_activities({1: missing}, [(1, TODAY)])
        with patch.object(reporting, "load_people", return_value=[missing]):
            location = reporting.member_progress(filters(view="locations", region="UNKNOWN"), NOW)
            members = reporting.member_progress(filters(view="members", level="pending", metric="bookings"), NOW)
        self.assertEqual(location["summary"]["active"], 0)
        self.assertIsNone(location["summary"]["level_gains"])
        self.assertEqual(members["members"][0]["bookings"], 1)
        self.assertIsNone(members["members"][0]["level"])

    def test_route_and_request_filters(self):
        self.assertIs(resolve(URL).func.view_class, AdminMemberProgressView)
        self.assertEqual(filters(view="members", level="200")["level"], 200)
        for invalid in (dict(days=999), dict(months=24), dict(level=201), dict(view="member"), dict(region="AUS")):
            with self.subTest(invalid=invalid):
                self.assertFalse(MemberProgressParameters(data=invalid).is_valid())

    @override_settings(COMMUNITY_CHAT_MEMBER_PROGRESS_LOCATION_ACCOUNT_IDS={"8"})
    def test_location_filters_require_separate_permission_on_every_route(self):
        view = resolve(URL).func
        factory = APIRequestFactory()

        def get(params, account, role, verified=True):
            request = factory.get(URL, params)
            force_authenticate(request, user=User(pk=account, email="admin@example.test", is_active=True))
            with patch("community_chat.admin_access.has_verified_installation", return_value=verified), \
                 patch("community_chat.admin_access.is_mlai_admin_user", return_value=bool(role)), \
                 patch("community_chat.admin_access.mlai_admin_role", return_value=role), \
                 patch("community_chat.admin_access.PointsAdmin.objects.filter") as committee:
                committee.return_value.exists.return_value = False
                return view(request)

        with patch("community_chat.admin_member_progress_views.member_progress", return_value={"example": True}) as report:
            self.assertEqual(get({}, 8, "committee").status_code, 403)
            self.assertEqual(get({}, 8, "admin", verified=False).status_code, 403)
            self.assertEqual(get(dict(view="locations"), 9, "superuser").status_code, 403)
            self.assertEqual(get(dict(view="members", region="VIC"), 9, "admin").status_code, 403)
            report.assert_not_called()
            response = get(dict(view="locations"), 8, "admin")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "no-store")
