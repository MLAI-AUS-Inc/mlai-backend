"""Reproducible scoring tests using synthetic dates and observations only."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from unittest import TestCase

from crm.engagement import engagement_rating
from crm.engagement_scoring import ActivityEvidence, SOURCES, VERSION, recency_weight, score_engagement

AS_OF = datetime(2026, 11, 2, 1, 0, tzinfo=timezone(timedelta(hours=11)))
TODAY = AS_OF.date()


def coverage(**changes):
    result = {source: {"status": "available"} for source in SOURCES}
    result.update(changes)
    return result


def activity(kind, age=0, *, event_id=None, verified=False, source=None):
    if source is None:
        source = "luma" if kind in ("checkin", "registration") else (
            "slack" if kind == "slack_message" else "app" if kind == "app_login" else "newsletter"
        )
    return ActivityEvidence(
        source=source, kind=kind, event_id=event_id or f"{kind}-{age}",
        occurred_at=TODAY - timedelta(days=age), human_verified=verified,
    )


def score(*activities, as_of=AS_OF, source_coverage=None):
    return score_engagement(activities, as_of=as_of, coverage=source_coverage or coverage())


class DecayTests(TestCase):
    def test_exact_window_boundaries_and_future_dates(self):
        for age, expected in ((-1, 0), (0, 1), (29, 1), (30, .5), (89, .5), (90, .25), (179, .25), (180, 0)):
            with self.subTest(age=age):
                self.assertEqual(recency_weight(age), expected)
                self.assertEqual(score(activity("checkin", age))["score"], 20 * expected)

    def test_daily_refresh_ages_evidence_without_changing_source_date(self):
        evidence = activity("checkin", 29)
        self.assertEqual(score(evidence)["score"], 20)
        self.assertEqual(score(evidence, as_of=AS_OF + timedelta(days=1))["score"], 10)
        self.assertEqual(evidence.occurred_at, TODAY - timedelta(days=29))

    def test_calendar_day_uses_melbourne_not_utc(self):
        late_utc = ActivityEvidence("slack", "slack_message", "late", "2026-10-03T14:30:00Z")
        self.assertEqual(score(late_utc)["score"], 5)
        earlier_utc = ActivityEvidence("slack", "slack_message", "early", "2026-10-03T12:30:00Z")
        self.assertEqual(score(earlier_utc)["score"], 2.5)

    def test_future_timestamp_on_same_calendar_day_is_ignored(self):
        future = ActivityEvidence("luma", "checkin", "future", "2026-11-02T12:00:00+11:00")
        self.assertEqual(score(future)["score"], 0)

    def test_naive_or_invalid_timestamps_are_not_guessed(self):
        for value in ("2026-02-30", "yesterday", datetime(2026, 11, 1), "2026-11-01T12:30:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                score(ActivityEvidence("luma", "checkin", "event", value))


class DeduplicationTests(TestCase):
    def test_repeated_source_event_does_not_inflate_score(self):
        evidence = activity("checkin", 1)
        self.assertEqual(score(evidence, evidence, evidence), score(evidence))

    def test_replayed_old_event_cannot_become_recent_even_after_expiry(self):
        old = activity("checkin", 180, event_id="same-event")
        replay = activity("checkin", 0, event_id="same-event")
        self.assertEqual(score(old, replay)["score"], 0)
        self.assertEqual(score(replay, old)["score"], 0)

    def test_checkin_supersedes_registration_for_same_event(self):
        result = score(activity("registration", 1, event_id="event"), activity("checkin", 1, event_id="event"))
        self.assertEqual(result["score"], 20)
        self.assertEqual(result["coverage"]["channel_points"]["registrations"], 0)

    def test_slack_counts_distinct_days_not_message_volume(self):
        messages = [activity("slack_message", 1, event_id=f"message-{i}") for i in range(100)]
        result = score(*messages)
        self.assertEqual(result["score"], 5)
        self.assertEqual(result["coverage"]["strong_dates_180d"], 1)

    def test_newsletter_actions_are_capped_per_kind_and_day(self):
        comments = [activity("newsletter_comment", 1, event_id=f"comment-{i}", verified=True) for i in range(100)]
        self.assertEqual(score(*comments)["score"], 5)


class SignalAndCapTests(TestCase):
    def test_events_registrations_slack_and_total_have_caps(self):
        events = [activity("checkin", day) for day in range(10)]
        registrations = [activity("registration", day) for day in range(10)]
        messages = [activity("slack_message", day) for day in range(20)]
        result = score(*events, *registrations, *messages)
        self.assertEqual(result["coverage"]["channel_points"], {
            "events": 60, "registrations": 6, "slack": 50, "app": 0, "newsletter": 0,
        })
        self.assertEqual(result["score"], 100)

    def test_app_latest_login_is_low_weight_and_never_strong(self):
        logins = [activity("app_login", day) for day in range(15)]
        result = score(*logins)
        self.assertEqual(result["score"], 5)
        self.assertEqual(result["stars"], 2)
        self.assertEqual(result["coverage"]["strong_dates_180d"], 0)
        self.assertFalse(result["coverage"]["app_usage_frequency_available"])
        self.assertIn("frequency unavailable", result["reason"])

    def test_opens_never_score_or_satisfy_strong_activity_gates(self):
        opens = [activity("newsletter_open", day, verified=True) for day in range(30)]
        result = score(*opens)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["stars"], 1)
        self.assertEqual(result["coverage"]["strong_dates_180d"], 0)
        self.assertFalse(result["coverage"]["email_opens_scored"])

    def test_unfiltered_clicks_remain_weak_and_capped_at_two(self):
        clicks = [activity("newsletter_click", day) for day in range(30)]
        result = score(*clicks)
        self.assertEqual(result["score"], 2)
        self.assertEqual(result["coverage"]["strong_dates_180d"], 0)
        self.assertIn("Weak newsletter", result["reason"])

    def test_verified_newsletter_actions_score_but_total_stays_fifteen(self):
        actions = [
            activity(kind, day, verified=True)
            for kind in ("newsletter_click", "newsletter_comment", "newsletter_share")
            for day in range(20)
        ]
        self.assertEqual(score(*actions)["coverage"]["channel_points"]["newsletter"], 15)

    def test_unverified_comments_and_shares_are_not_fabricated_strong_signals(self):
        result = score(activity("newsletter_comment"), activity("newsletter_share"))
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["coverage"]["strong_dates_180d"], 0)

    def test_registration_only_cannot_create_a_high_rating(self):
        result = score(*(activity("registration", day) for day in range(20)))
        self.assertEqual((result["stars"], result["score"]), (2, 6))


class StarGateTests(TestCase):
    def test_five_stars_requires_three_distinct_strong_dates(self):
        same_day = [activity("checkin", 1, event_id=f"event-{i}") for i in range(3)]
        result = score(*same_day)
        self.assertEqual((result["score"], result["stars"]), (60, 4))
        separate_days = [activity("checkin", day) for day in (1, 2, 3)]
        self.assertEqual(score(*separate_days)["stars"], 5)

    def test_five_stars_requires_a_strong_action_within_thirty_days(self):
        events = [activity("checkin", day) for day in range(30, 40)]
        result = score(*events, activity("app_login", 0), activity("newsletter_click", 0))
        self.assertGreaterEqual(result["score"], 50)
        self.assertEqual(result["stars"], 4)

    def test_four_stars_requires_strong_activity_within_ninety_days(self):
        events = [activity("checkin", day) for day in range(90, 105)]
        result = score(*events)
        self.assertGreaterEqual(result["score"], 50)
        self.assertEqual(result["stars"], 3)

    def test_strong_dates_are_deduplicated_across_sources(self):
        result = score(
            activity("checkin", 1, event_id="a"), activity("checkin", 1, event_id="b"),
            activity("slack_message", 1), activity("newsletter_click", 1, verified=True),
        )
        self.assertEqual(result["coverage"]["strong_dates_180d"], 1)


class CoverageTests(TestCase):
    def test_missing_coverage_does_not_claim_inactivity_or_a_complete_refresh(self):
        result = score(activity("slack_message", 1), source_coverage={"slack": {"status": "partial", "detail": "Public channels only"}})
        self.assertEqual(result["score"], 5)
        self.assertTrue(result["coverage"]["provisional"])
        self.assertFalse(result["coverage"]["refresh_safe"])
        self.assertIn("Missing evidence is not inactivity", result["coverage"]["summary"])
        self.assertEqual(result["coverage"]["unavailable_sources"], ["luma", "app", "newsletter"])

    def test_never_connected_provider_does_not_freeze_supported_sources(self):
        result = score(activity("checkin", 1), source_coverage=coverage(
            newsletter={"status": "unavailable", "expected": False, "detail": "No connected newsletter source"},
        ))
        self.assertEqual(result["score"], 20)
        self.assertTrue(result["coverage"]["refresh_safe"])
        self.assertTrue(result["coverage"]["provisional"])

    def test_transient_failure_flags_previous_rating_for_preservation(self):
        result = score(activity("checkin", 1), source_coverage=coverage(
            slack={"status": "unavailable", "expected": True, "detail": "Temporary fetch failure"},
        ))
        self.assertFalse(result["coverage"]["refresh_safe"])

    def test_stale_dated_evidence_remains_observed_but_refresh_is_not_safe(self):
        result = score(activity("slack_message", 30), source_coverage=coverage(
            slack={"status": "stale", "detail": "Bridge coverage stale"},
        ))
        self.assertEqual(result["score"], 2.5)
        self.assertEqual(result["coverage"]["stale_sources"], ["slack"])
        self.assertFalse(result["coverage"]["refresh_safe"])

    def test_no_assessable_source_returns_none_instead_of_one_star(self):
        unavailable = {source: {"status": "unavailable", "expected": False} for source in SOURCES}
        self.assertIsNone(score_engagement([], as_of=AS_OF, coverage=unavailable))
        self.assertIsNone(score_engagement([], as_of=AS_OF, coverage={}))

    def test_valid_snapshot_contract_and_no_input_mutation(self):
        source_coverage = coverage(slack={"status": "partial", "detail": "Public activity only"})
        before = deepcopy(source_coverage)
        result = score(activity("checkin", 2), source_coverage=source_coverage)
        self.assertEqual(engagement_rating({"engagement_rating": result}), result)
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["as_of"], "2026-11-02")
        self.assertEqual(source_coverage, before)

    def test_invalid_source_kind_coverage_and_ids_fail_before_any_io(self):
        for evidence in (
            ActivityEvidence("unknown", "checkin", "e", TODAY),
            ActivityEvidence("app", "checkin", "e", TODAY),
            ActivityEvidence("luma", "checkin", "", TODAY),
        ):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                score(evidence)
        for entry in ({"status": "healthy"}, {"status": "available", "expected": "false"}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                score(source_coverage=coverage(slack=entry))
