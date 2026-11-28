"""MLAI Wrapped export on PostgreSQL: committee selection, the year's ledger split, Luma check-ins and names."""

import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone

from django.test import TestCase, override_settings

from community_chat.onboarding_models import CommunityMemberProfile
from community_chat.wrapped_stats import sheet
from community_chat.wrapped_stats_data import preferred_names, wrapped_people
from core.models import User
from crm.models import Contact, CrmEvent, EventRegistration
from mentoring.models import MentorProfile, MentorSession
from roo.models import CoworkingBooking, Ledger, PointsAdmin, PointsRequest

NOW = datetime(2026, 12, 1, 1, tzinfo=timezone.utc)


def at(day):
    return datetime(day.year, day.month, day.day, 2, tzinfo=timezone.utc)


@override_settings(COMMUNITY_CHAT_VOLUNTEER_COMMUNITY="wrapped.example.test")
class WrappedStatsDatabaseTests(TestCase):
    def credit(self, user, roo, day, **fields):
        row = Ledger.objects.create(
            user=user, kind="EARN", delta=roo, delta_microroo=roo * 1_000_000,
            source=fields.pop("source", "MANUAL"), **fields,
        )
        Ledger.objects.filter(pk=row.pk).update(created_at=at(day))
        return row

    def setUp(self):
        self.ada = User.objects.create(
            email="Ada@Example.test", first_name="Augusta", last_name="King", slack_id="UADA",
        )
        self.bob = User.objects.create(email="bob@example.test", first_name="Bob", last_name="Ng", slack_id="UBOB")
        # Committee rows may be keyed by Slack ID only.
        PointsAdmin.objects.create(slack_user_id="UADA", role="committee")
        PointsAdmin.objects.create(slack_user_id="UBOB", user=self.bob, role="partner")
        CommunityMemberProfile.objects.create(user=self.ada, first_name=" Ada ", last_name="Lovelace")

        self.credit(self.ada, 12, date(2026, 2, 3), reference_type="TASK_SUBMISSION")
        self.credit(self.ada, 100, date(2026, 2, 10), description="Weekly committee points remuneration")
        self.credit(self.ada, 50, date(2026, 3, 1), source="purchased_topup", reference_type="POINTS_PURCHASE")
        self.credit(self.ada, 5, date(2026, 3, 2), source="STARTUP_UPDATE")
        self.credit(self.ada, 7, date(2025, 12, 30), reference_type="TASK_SUBMISSION")
        logged = self.credit(self.ada, 3, date(2026, 11, 22), reference_type="POINTS_REQUEST")
        PointsRequest.objects.create(
            requester_slack_id="UADA", target_slack_id="UADA", points=3, reason="Set up", status="approved",
            ledger_entry=logged, manual_time_key="wrapped-1", manual_time={"volunteer_minutes": 25},
        )
        self.credit(self.bob, 1, date(2026, 4, 1), reference_type="TASK_SUBMISSION")
        CoworkingBooking.objects.create(user=self.ada, date=date(2026, 4, 1), points_cost=4)
        CoworkingBooking.objects.create(user=self.ada, date=date(2026, 4, 2), points_cost=4, status="cancelled")

        event = CrmEvent.objects.create(luma_event_id="evt-1", name="Demo night")
        other = CrmEvent.objects.create(luma_event_id="evt-2", name="Hack day")
        contact = Contact.objects.create(email="ada@example.test", source="luma")
        EventRegistration.objects.create(event=event, contact=contact, checked_in_at=at(date(2026, 6, 2)))
        EventRegistration.objects.create(
            event=other, contact=contact, checked_in_at=at(date(2026, 6, 3)), removed_at=at(date(2026, 6, 4)),
        )

        mentor = MentorProfile.objects.create(user=self.ada, is_active=True)
        for status in (MentorSession.STATUS_COMPLETED, MentorSession.STATUS_CANCELLED):
            MentorSession.objects.create(
                mentor=mentor, founder=self.bob, status=status,
                starts_at=at(date(2026, 11, 10)), ends_at=at(date(2026, 11, 10)) + timedelta(minutes=30),
                price_roo=10, location_type="video", brief_stage="Seed", brief_struggle="Sales",
                brief_outcome="A plan", client_request_id=uuid.uuid4(),
                requested_at=at(date(2026, 11, 1)), expires_at=at(date(2026, 11, 3)),
            )

    def test_committee_export_splits_earned_and_paid_points_for_the_calendar_year(self):
        people, selection = wrapped_people(2026, NOW)
        self.assertEqual(selection, dict(unknown_profile_ids=[], without_qualifying_record=[]))
        [ada] = people
        self.assertEqual((ada.name, ada.name_source), ("Ada Lovelace", "chat_profile"))
        self.assertEqual(ada.roo_earned_microroo, 20_000_000)
        self.assertEqual(ada.roo_paid_microroo, 100_000_000)
        self.assertEqual((ada.volunteer_minutes, ada.logged_minutes), (145, 25))
        self.assertEqual(ada.monthly_updates, 1)
        self.assertEqual((ada.coworking_days, ada.events_attended), (1, 1))
        self.assertEqual((ada.mentoring_given, ada.mentoring_received), (1, 0))

        rows = list(csv.reader(io.StringIO(sheet(people)["csv"])))
        self.assertEqual(rows[1][0], "Ada Lovelace")
        text = sheet(people)["csv"].lower()
        for private in ("ada@example.test", str(self.ada.community_chat_profile_id), "uada"):
            self.assertNotIn(private, text)

    def test_explicit_members_and_unknown_ids_are_reported_not_merged(self):
        missing = str(uuid.uuid4())
        people, selection = wrapped_people(
            2026, NOW, scope="members", member_ids=[str(self.bob.community_chat_profile_id), missing],
        )
        [bob] = people
        self.assertEqual((bob.name, bob.mentoring_received, bob.roo_earned_microroo), ("Bob Ng", 1, 1_000_000))
        self.assertEqual(selection["unknown_profile_ids"], [missing])

    def test_names_fall_back_from_override_to_chat_profile_to_account(self):
        people, _ = wrapped_people(2026, NOW, scope="all")
        by_id = {person.id: person for person in people}
        self.assertEqual(by_id[str(self.bob.community_chat_profile_id)].name_source, "account")
        overrides = {str(self.ada.community_chat_profile_id): "Countess Ada"}
        people, _ = wrapped_people(2026, NOW, scope="committee", overrides=overrides)
        self.assertEqual((people[0].name, people[0].name_source), ("Countess Ada", "override"))
        self.assertEqual(preferred_names([], {}), {})
