"""MLAI Wrapped stat sheet: points split, volunteer hours, CSV shape and the admin gate, without a database."""

import csv
import io
from datetime import date, datetime, timezone
from unittest.mock import patch
from uuid import UUID

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat import wrapped_stats_data as data
from community_chat.admin_member_progress_data import Activity, Person
from community_chat.admin_wrapped_stats_views import AdminWrappedStatsView
from community_chat.wrapped_stats import (
    EARNED, HEADINGS, MAX_CELL, MAX_HEADING, MAX_ROWS, MAX_STAT_COLUMNS, PAID, PURCHASED, WrappedPerson,
    hours, points_bucket, sheet, standing,
)
from core.models import User

URL = "/api/v1/community-chat/admin/wrapped-stats/"
NOW = datetime(2026, 11, 28, 1, tzinfo=timezone.utc)
ROO = 1_000_000


def credit(**row):
    return {"source": "MANUAL", "reference_type": None, "description": "", **row}


def parse(text):
    return list(csv.reader(io.StringIO(text)))


class PointsSplitTest(SimpleTestCase):
    def test_paid_points_are_remuneration_and_never_volunteering(self):
        self.assertEqual(points_bucket(credit(description="Weekly committee points remuneration")), (PAID, False))
        self.assertEqual(points_bucket(credit(reference_type="MENTOR_SESSION")), (PAID, False))
        self.assertEqual(points_bucket(credit(source="purchased_topup")), (PURCHASED, False))
        self.assertEqual(points_bucket(credit(reference_type="APPLE_IAP")), (PURCHASED, False))

    def test_earned_points_count_toward_volunteering_only_when_they_count_toward_level(self):
        self.assertEqual(points_bucket(credit(reference_type="TASK_SUBMISSION")), (EARNED, True))
        self.assertEqual(points_bucket(credit(reference_type="POINTS_REQUEST")), (EARNED, True))
        self.assertEqual(points_bucket(credit(reference_type="VOLUNTEER_CONTRIBUTION"), "event_setup"), (EARNED, True))
        self.assertEqual(
            points_bucket(credit(reference_type="VOLUNTEER_CONTRIBUTION"), "monthly_startup_update"), (EARNED, False),
        )
        self.assertEqual(points_bucket(credit(description="link-love")), (EARNED, False))
        self.assertEqual(points_bucket(credit(source="STARTUP_UPDATE")), (EARNED, False))
        self.assertEqual(points_bucket(credit(reference_type="VOLUNTEER_LEVEL_BONUS")), (EARNED, False))

    def test_volunteer_minutes_prefer_logged_time_then_ten_minutes_a_point(self):
        person = WrappedPerson(id="p", name="Ada")
        person.add_credit(6 * ROO, EARNED, True)
        person.add_credit(3 * ROO, EARNED, True, logged_minutes=45)
        person.add_credit(5 * ROO, EARNED, False)
        person.add_credit(100 * ROO, PAID, False)
        person.add_credit(50 * ROO, PURCHASED, False)
        self.assertEqual((person.volunteer_minutes, person.logged_minutes), (105, 45))
        self.assertEqual(person.roo_earned_microroo, 14 * ROO)
        self.assertEqual(str(hours(105)), "1.8")
        self.assertEqual(str(hours(120)), "2")


class SheetTest(SimpleTestCase):
    def people(self):
        ada = WrappedPerson(
            id=str(UUID(int=1)), name="Ada Lovelace", roo_earned_microroo=120_400_000,
            volunteer_minutes=600, events_attended=9, coworking_days=20, badges=2, latest_badge="Mic Is On",
            level=12, rank="Prompt Wrangler", levels_climbed=7,
            active_days=[date(2026, 3, 1), date(2026, 3, 9), date(2026, 5, 1)],
        )
        first = WrappedPerson(id=str(UUID(int=2)), name="Sam Lee", roo_earned_microroo=10 * ROO, events_attended=9)
        second = WrappedPerson(id=str(UUID(int=3)), name=" sam  LEE ", name_source="chat_profile")
        unnamed = WrappedPerson(id=str(UUID(int=4)), name="MLAI member", name_source="missing")
        return [ada, first, second, unnamed]

    def test_csv_matches_the_media_intel_stat_sheet_contract(self):
        result = sheet(self.people())
        rows = parse(result["csv"])
        self.assertEqual(rows[0][0], "Name")
        self.assertLessEqual(len(rows[0]) - 1, MAX_STAT_COLUMNS)
        self.assertTrue(all(len(heading) <= MAX_HEADING for heading in rows[0]))
        self.assertEqual(len(set(rows[0])), len(rows[0]))
        self.assertEqual(rows[0], list(HEADINGS))
        self.assertNotIn("Roo points paid", rows[0])
        self.assertTrue(all(len(cell) <= MAX_CELL for row in rows for cell in row))
        self.assertEqual(result["rows"], 4)
        self.assertNotIn("@", result["csv"])
        for person in self.people():
            self.assertNotIn(person.id, result["csv"])

    def test_values_words_blanks_and_standings(self):
        rows = parse(sheet(self.people())["csv"])
        by_name = {row[0]: dict(zip(rows[0], row)) for row in rows[1:]}
        ada = by_name["Ada Lovelace"]
        self.assertEqual(ada["Name"], "Ada Lovelace")
        self.assertEqual(ada["Roo points earned"], "120")
        self.assertEqual(ada["Volunteer hours (est.)"], "10")
        self.assertEqual(ada["Latest badge"], "Mic Is On")
        self.assertEqual(ada["Rank"], "Prompt Wrangler")
        self.assertEqual(ada["Busiest month"], "March")
        self.assertEqual(ada["Roo points earned standing"], "Top 25%")
        self.assertEqual(ada["Events attended standing"], "Top 25%")
        self.assertEqual(ada["Volunteer hours standing"], "Top 25%")
        sam = by_name["Sam Lee"]
        self.assertEqual(sam["Events attended standing"], "Top 25%")
        self.assertEqual(sam["Roo points earned standing"], "Top 50%")
        self.assertEqual(sam["Mentoring sessions given"], "")
        self.assertEqual(sam["Days coworking standing"], "")

    def test_same_names_stay_separate_and_are_flagged_for_review(self):
        result = sheet(self.people())
        names = [row[0] for row in parse(result["csv"])[1:]]
        self.assertEqual(sorted(names), sorted(["Ada Lovelace", "Sam Lee", " sam  LEE ", "MLAI member"]))
        [collision] = result["review"]["name_collisions"]
        self.assertEqual({m["id"] for m in collision["members"]}, {str(UUID(int=2)), str(UUID(int=3))})
        self.assertEqual(result["review"]["needs_name"], [
            dict(id=str(UUID(int=4)), name="MLAI member", reason="no name on record"),
        ])

    def test_pending_rank_names_are_kept_and_listed_for_review(self):
        pending = WrappedPerson(id=str(UUID(int=5)), name="Grace Hopper", level=28, rank="Hype Debugger")
        result = sheet([*self.people(), pending])
        rows = parse(result["csv"])
        by_name = {row[0]: dict(zip(rows[0], row)) for row in rows[1:]}
        self.assertEqual(by_name["Grace Hopper"]["Rank"], "Hype Debugger")
        self.assertEqual(result["review"]["pending_ranks"], [dict(
            id=str(UUID(int=5)), name="Grace Hopper", rank="Hype Debugger",
            reason="rank name pending committee decision",
        )])

    def test_standing_is_relative_to_the_exported_group(self):
        self.assertEqual(standing([5, 3, 3, 0], 3), "Top 50%")
        self.assertEqual(standing([5, 3, 3, 0], 5), "Top 25%")
        self.assertEqual(standing([5, 3, 3, 0], 0), "")
        self.assertEqual(standing([5], 5), "")


def roster_person(number, *activity, ranks=(None, None), badges=None):
    person = Person(number, {number, number + 100}, str(UUID(int=number)), f"Member {number}", ranks=ranks)
    person.activity.extend(activity)
    person.badge_collection = badges
    return person


class WrappedPeopleTest(SimpleTestCase):
    def run_export(self, people, **kwargs):
        ledger = {
            1: credit(reference_type="TASK_SUBMISSION"),
            2: credit(description="Weekly committee points remuneration"),
            3: credit(source="STARTUP_UPDATE"),
            4: credit(reference_type="POINTS_REQUEST"),
        }
        with patch.object(data, "load_people", return_value=people) as load, \
             patch.object(data, "committee_account_ids", return_value={101}), \
             patch.object(data, "preferred_names", side_effect=lambda chosen, _: {
                 p.account_id: (p.name, "account") for p in chosen}), \
             patch.object(data, "_ledger_facts", return_value=(ledger, {}, {4: 25})), \
             patch.object(data, "_event_check_ins", return_value={(1, "evt"): date(2026, 6, 2)}), \
             patch.object(data, "_mentoring", return_value=({(1, "s1")}, set())):
            result = data.wrapped_people(2026, NOW, **kwargs)
        return result, load

    def test_committee_year_reduces_activity_to_counts(self):
        badges = [
            dict(name="Camera Goblin", awarded_on=date(2026, 11, 2), recorded_on=date(2026, 11, 20)),
            dict(name="Folding Chair Union", awarded_on=None, recorded_on=date(2026, 11, 21)),
            dict(name="Old News", awarded_on=date(2025, 6, 1), recorded_on=date(2026, 11, 20)),
        ]
        committee = roster_person(
            1,
            Activity(date(2026, 2, 3), "roo", "1", 12 * ROO),
            Activity(date(2026, 2, 10), "roo", "2", 100 * ROO),
            Activity(date(2026, 3, 1), "roo", "3", 5 * ROO),
            Activity(date(2026, 11, 22), "roo", "4", 3 * ROO),
            Activity(date(2026, 4, 1), "booking", "2026-04-01"),
            Activity(date(2026, 9, 9), "check_in", "volunteer-evt"),
            ranks=(25_000_000, 12_000_000), badges=badges,
        )
        other = roster_person(2, Activity(date(2026, 2, 3), "roo", "1", ROO))
        (people, selection), load = self.run_export([committee, other])
        start, end_at = load.call_args.args[0], load.call_args.args[2]
        self.assertEqual(start, date(2026, 1, 1))
        self.assertEqual(end_at, NOW)
        [person] = people
        self.assertEqual(person.roo_earned_microroo, 20 * ROO)
        self.assertEqual((person.volunteer_minutes, person.logged_minutes), (145, 25))
        self.assertEqual(person.monthly_updates, 1)
        self.assertEqual((person.coworking_days, person.volunteer_check_ins, person.events_attended), (1, 1, 1))
        self.assertEqual((person.mentoring_given, person.mentoring_received), (1, 0))
        self.assertEqual((person.badges, person.latest_badge), (2, "Folding Chair Union"))
        self.assertEqual((person.level, person.levels_climbed), (2, 1))
        self.assertTrue(person.rank)
        self.assertEqual(selection, dict(unknown_profile_ids=[], without_qualifying_record=[]))

    def test_year_window_closes_at_the_end_of_a_finished_year(self):
        start, end_at = data.year_bounds(2026, datetime(2027, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(start, date(2026, 1, 1))
        self.assertEqual(data.local_day(end_at), date(2026, 12, 31))

    def test_scope_all_ignores_committee_selection(self):
        (people, _), _ = self.run_export([roster_person(1), roster_person(2)], scope="all")
        self.assertEqual(len(people), 2)
        with self.assertRaises(ValueError):
            data.wrapped_people(2026, NOW, scope="everyone")


class ExportSurfaceTest(SimpleTestCase):
    def test_command_rejects_bad_selection_before_reading(self):
        with patch.object(data, "load_people") as load:
            for args in (
                ["--year", "2024"], ["--year", "2030"], ["--year", "2026", "--member", "nope"],
                ["--year", "2026", "--scope", "members"], ["--year", "2026", "--name", "x"],
            ):
                with self.subTest(args=args), self.assertRaises(CommandError):
                    call_command("export_wrapped_stats", *args, stdout=io.StringIO(), stderr=io.StringIO())
            load.assert_not_called()

    def test_command_and_endpoint_reject_more_rows_than_media_intel_accepts(self):
        self.assertEqual(MAX_ROWS, 5000)
        crowd = ([WrappedPerson(id=str(UUID(int=n)), name=f"Member {n}") for n in range(5001)],
                 dict(unknown_profile_ids=[], without_qualifying_record=[]))
        with patch("community_chat.management.commands.export_wrapped_stats.wrapped_people", return_value=crowd), \
             self.assertRaisesMessage(CommandError, "narrow the scope"):
            call_command("export_wrapped_stats", "--year", "2026", "--scope", "all",
                         stdout=io.StringIO(), stderr=io.StringIO())
        request = APIRequestFactory().get(URL, dict(year=2026, scope="all"))
        force_authenticate(request, user=User(pk=8, email="admin@example.test", is_active=True))
        with patch("community_chat.admin_views.is_mlai_admin_request", return_value=True), \
             patch("community_chat.admin_wrapped_stats_views.member_progress_capabilities",
                   return_value={"member_progress"}), \
             patch("community_chat.admin_wrapped_stats_views.wrapped_people", return_value=crowd):
            response = resolve(URL).func(request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("narrow the scope", str(response.data["scope"]))

    def test_command_writes_csv_to_stdout_and_review_to_stderr(self):
        person = WrappedPerson(id=str(UUID(int=9)), name="Ada Lovelace", events_attended=3)
        pending = WrappedPerson(id=str(UUID(int=10)), name="Grace Hopper", rank="Department of Actually Shipping")
        out, err = io.StringIO(), io.StringIO()
        with patch("community_chat.management.commands.export_wrapped_stats.wrapped_people",
                   return_value=([person, pending], dict(unknown_profile_ids=[], without_qualifying_record=[]))) as run:
            call_command("export_wrapped_stats", "--year", "2026", "--name", f"{UUID(int=9)}=Ada L.",
                         stdout=out, stderr=err)
        self.assertEqual(run.call_args.kwargs["overrides"], {str(UUID(int=9)): "Ada L."})
        self.assertEqual(run.call_args.kwargs["scope"], "committee")
        self.assertEqual(parse(out.getvalue())[1][0], "Ada Lovelace")
        self.assertNotIn(str(UUID(int=9)), out.getvalue())
        self.assertIn(str(UUID(int=9)), err.getvalue())
        self.assertIn("rank name pending committee decision", err.getvalue())
        self.assertIn("Grace Hopper", err.getvalue())

    def test_endpoint_is_gated_like_member_progress(self):
        self.assertIs(resolve(URL).func.view_class, AdminWrappedStatsView)
        view = resolve(URL).func
        factory = APIRequestFactory()

        def get(params, role, verified=True):
            request = factory.get(URL, params)
            force_authenticate(request, user=User(pk=8, email="admin@example.test", is_active=True))
            with patch("community_chat.admin_access.has_verified_installation", return_value=verified), \
                 patch("community_chat.admin_access.is_mlai_admin_user", return_value=bool(role)), \
                 patch("community_chat.admin_access.mlai_admin_role", return_value=role), \
                 patch("community_chat.admin_access.PointsAdmin.objects.filter") as committee:
                committee.return_value.exists.return_value = False
                return view(request)

        empty = ([], dict(unknown_profile_ids=[], without_qualifying_record=[]))
        with patch("community_chat.admin_wrapped_stats_views.wrapped_people", return_value=empty) as run:
            self.assertEqual(get(dict(year=2026), "committee").status_code, 403)
            self.assertEqual(get(dict(year=2026), "admin", verified=False).status_code, 403)
            self.assertEqual(get(dict(year=2026), None).status_code, 403)
            run.assert_not_called()
            self.assertEqual(get({}, "admin").status_code, 400)
            self.assertEqual(get(dict(year=2030), "admin").status_code, 400)
            self.assertEqual(get(dict(year=2026, scope="members"), "admin").status_code, 400)
            run.assert_not_called()
            response = get(dict(year=2026, member=str(UUID(int=5))), "admin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["scope"], "members")
        self.assertEqual(run.call_args.kwargs["member_ids"], [str(UUID(int=5))])
        self.assertTrue(response.data["csv"].startswith("Name,"))
