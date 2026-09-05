import hashlib
import secrets
from datetime import timedelta, timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from community_chat.authentication import USAGE_TOKEN_PREFIX
from community_chat.models import (
    TokenUsageAccount,
    TokenUsageDailyBucket,
    TokenUsageSession,
)
from community_chat.token_usage import local_usage_date


SESSION_UUID = "11111111-2222-3333-4444-555555555555"
OTHER_UUID = "99999999-8888-7777-6666-555555555555"


def now_ms():
    return int(timezone.now().timestamp() * 1000)


def session_row(**overrides):
    row = {
        "session_id": SESSION_UUID,
        "model": "claude-opus-5",
        "started_at": now_ms(),
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
    }
    row.update(overrides)
    return row


class TokenUsageIngestTests(APITestCase):
    """The reporter wire protocol: POST /usage/api/ingest with a Bearer token."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="member@example.com")
        self.token, self.account = self.mint_account(self.user)
        self.url = reverse("token_usage_ingest")

    def mint_account(self, user):
        raw = USAGE_TOKEN_PREFIX + secrets.token_urlsafe(32)
        account = TokenUsageAccount.objects.create(
            user=user,
            token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
        return raw, account

    def ingest(self, sessions, token=None, source="claude_code", url=None):
        return self.client.post(
            url or self.url,
            {"source": source, "sessions": sessions},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )

    def test_accepts_a_session_and_stores_it(self):
        response = self.ingest([session_row()])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["accepted"], 1)
        self.assertEqual(response.data["rejected"], [])
        stored = TokenUsageSession.objects.get()
        self.assertEqual(stored.account, self.account)
        self.assertEqual(stored.input_tokens, 1000)

    def test_reporting_the_same_session_replaces_rather_than_adds(self):
        """The reporter re-sends whole-session totals on every hook fire."""
        self.ingest([session_row(input_tokens=1000)])
        self.ingest([session_row(input_tokens=4000)])

        self.assertEqual(TokenUsageSession.objects.count(), 1)
        self.assertEqual(TokenUsageSession.objects.get().input_tokens, 4000)

    def test_repeated_cumulative_reports_credit_only_positive_daily_deltas(self):
        self.ingest([session_row(input_tokens=1000, output_tokens=50)])
        self.ingest([session_row(input_tokens=1000, output_tokens=50)])
        self.ingest([session_row(input_tokens=1400, output_tokens=75)])

        bucket = TokenUsageDailyBucket.objects.get()
        self.assertEqual(bucket.usage_date, local_usage_date())
        self.assertEqual(bucket.input_tokens, 400)
        self.assertEqual(bucket.output_tokens, 25)
        self.assertEqual(TokenUsageSession.objects.get().input_tokens, 1400)

    def test_first_live_snapshot_establishes_a_baseline_without_daily_usage(self):
        self.ingest([session_row(input_tokens=5_000_000_000)])

        self.assertEqual(TokenUsageSession.objects.get().input_tokens, 5_000_000_000)
        self.assertFalse(TokenUsageDailyBucket.objects.exists())

    def test_session_growth_after_melbourne_midnight_uses_the_new_day(self):
        melbourne_now = timezone.now().astimezone(ZoneInfo("Australia/Melbourne"))
        first_report = (
            melbourne_now.replace(hour=23, minute=55, second=0, microsecond=0)
            - timedelta(days=1)
        ).astimezone(datetime_timezone.utc)
        second_report = first_report + timedelta(minutes=10)

        with patch("community_chat.usage_views.timezone.now", return_value=first_report):
            self.ingest([session_row(input_tokens=100)])
        with patch("community_chat.usage_views.timezone.now", return_value=second_report):
            self.ingest([session_row(input_tokens=175)])

        bucket = TokenUsageDailyBucket.objects.get()
        self.assertEqual(bucket.input_tokens, 75)
        self.assertEqual(bucket.usage_date, local_usage_date(second_report))

    def test_history_establishes_a_baseline_without_inventing_daily_usage(self):
        self.ingest(
            [session_row(input_tokens=1000)],
            url=reverse("token_usage_history"),
        )
        self.assertFalse(TokenUsageDailyBucket.objects.exists())

        self.ingest([session_row(input_tokens=1000)])
        self.assertFalse(TokenUsageDailyBucket.objects.exists())

        self.ingest([session_row(input_tokens=1250)])
        self.assertEqual(TokenUsageDailyBucket.objects.get().input_tokens, 250)

    def test_same_session_id_under_two_sources_is_two_rows(self):
        self.ingest([session_row()], source="claude_code")
        self.ingest([session_row()], source="codex")

        self.assertEqual(TokenUsageSession.objects.count(), 2)

    def test_same_session_two_models_is_two_rows(self):
        self.ingest([session_row(), session_row(model="claude-haiku-4-5")])

        self.assertEqual(TokenUsageSession.objects.count(), 2)

    def test_one_bad_row_does_not_block_the_rest_of_the_batch(self):
        response = self.ingest(
            [
                session_row(),
                session_row(session_id="", model="claude-opus-5"),
                session_row(session_id=OTHER_UUID),
            ]
        )

        self.assertEqual(response.data["accepted"], 2)
        self.assertEqual(len(response.data["rejected"]), 1)
        self.assertEqual(response.data["rejected"][0]["index"], 1)
        self.assertEqual(TokenUsageSession.objects.count(), 2)

    def test_future_timestamps_are_rejected(self):
        """A row stamped ahead of now would sit in every window at once."""
        year_ahead = now_ms() + 365 * 24 * 60 * 60 * 1000
        response = self.ingest([session_row(started_at=year_ahead)])

        self.assertEqual(response.data["accepted"], 0)
        self.assertEqual(len(response.data["rejected"]), 1)
        self.assertEqual(TokenUsageSession.objects.count(), 0)

    def test_path_shaped_session_ids_are_rejected(self):
        """Keeps member project and client directory names out of our DB."""
        response = self.ingest(
            [session_row(session_id="/Users/sam/clients/BigBank-Confidential")]
        )

        self.assertEqual(response.data["accepted"], 0)
        self.assertEqual(TokenUsageSession.objects.count(), 0)

    def test_absurd_token_counts_are_rejected(self):
        response = self.ingest([session_row(input_tokens=9_007_199_254_740_991)])

        self.assertEqual(response.data["accepted"], 0)
        self.assertEqual(TokenUsageSession.objects.count(), 0)

    def test_negative_and_non_numeric_counts_coerce_to_zero(self):
        response = self.ingest(
            [session_row(input_tokens=-5, output_tokens="lots", reasoning_tokens=None)]
        )

        self.assertEqual(response.data["accepted"], 1)
        stored = TokenUsageSession.objects.get()
        self.assertEqual(stored.input_tokens, 0)
        self.assertEqual(stored.output_tokens, 0)
        self.assertEqual(stored.reasoning_tokens, 0)

    def test_synthetic_models_are_skipped_without_being_rejected(self):
        response = self.ingest([session_row(model="<synthetic>")])

        self.assertEqual(response.data["accepted"], 0)
        self.assertEqual(response.data["rejected"], [])
        self.assertEqual(TokenUsageSession.objects.count(), 0)

    def test_unknown_source_fails_the_whole_request(self):
        response = self.ingest([session_row()], source="notarealtool")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_one_account_cannot_write_another_accounts_rows(self):
        other = get_user_model().objects.create_user(email="other@example.com")
        other_token, other_account = self.mint_account(other)

        self.ingest([session_row(input_tokens=1000)])
        self.ingest([session_row(input_tokens=7777)], token=other_token)

        self.assertEqual(TokenUsageSession.objects.filter(account=self.account).count(), 1)
        self.assertEqual(
            TokenUsageSession.objects.get(account=self.account).input_tokens, 1000
        )
        self.assertEqual(
            TokenUsageSession.objects.get(account=other_account).input_tokens, 7777
        )

    def test_missing_credential_is_rejected(self):
        response = self.client.post(
            self.url,
            {"source": "claude_code", "sessions": [session_row()]},
            format="json",
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_unknown_usage_token_is_rejected(self):
        response = self.ingest([session_row()], token=USAGE_TOKEN_PREFIX + "nope")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertEqual(TokenUsageSession.objects.count(), 0)

    def test_history_endpoint_shares_the_ingest_contract(self):
        response = self.ingest([session_row()], url=reverse("token_usage_history"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["accepted"], 1)


class TokenUsageAccountTests(APITestCase):
    """Minting, hiding and leaving the board."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="member@example.com")
        self.client.force_authenticate(user=self.user)
        self.url = reverse("token_usage_token")

    def report(self, raw_token):
        """Post as the reporter would.

        A separate client on purpose: ``force_authenticate`` bypasses the
        authenticators, so reusing ``self.client`` would never exercise
        TokenUsageAuthentication and a stale-token assertion would pass for
        the wrong reason.
        """
        return APIClient().post(
            reverse("token_usage_ingest"),
            {"source": "claude_code", "sessions": [session_row()]},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {raw_token}",
        )

    def test_mint_returns_a_usable_token_once(self):
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        raw = response.data["token"]
        self.assertTrue(raw.startswith(USAGE_TOKEN_PREFIX))
        self.assertEqual(response.data["api_base"], "https://api.mlai.au/api/v1/community-chat/usage")
        account = TokenUsageAccount.objects.get(user=self.user)
        self.assertEqual(
            account.token_hash, hashlib.sha256(raw.encode("utf-8")).hexdigest()
        )

    def test_the_raw_token_is_never_stored(self):
        raw = self.client.post(self.url, {}, format="json").data["token"]

        self.assertNotIn(raw, TokenUsageAccount.objects.get(user=self.user).token_hash)

    def test_minting_again_rotates_and_invalidates_the_old_token(self):
        first = self.client.post(self.url, {}, format="json").data["token"]
        second = self.client.post(self.url, {}, format="json").data["token"]

        self.assertNotEqual(first, second)
        self.assertEqual(TokenUsageAccount.objects.filter(user=self.user).count(), 1)
        self.assertIn(
            self.report(first).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertEqual(self.report(second).status_code, status.HTTP_200_OK)

    def test_visibility_can_be_toggled_without_losing_history(self):
        self.client.post(self.url, {}, format="json")
        response = self.client.patch(self.url, {"is_public": False}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(TokenUsageAccount.objects.get(user=self.user).is_public)

    def test_leaving_the_board_really_deletes_the_data(self):
        raw = self.client.post(self.url, {}, format="json").data["token"]
        self.assertEqual(self.report(raw).status_code, status.HTTP_200_OK)
        self.assertEqual(TokenUsageSession.objects.count(), 1)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(TokenUsageAccount.objects.count(), 0)
        self.assertEqual(TokenUsageSession.objects.count(), 0)

    def test_status_reports_whether_the_member_is_connected(self):
        before = self.client.get(self.url)
        self.assertFalse(before.data["connected"])

        self.client.post(self.url, {}, format="json")
        after = self.client.get(self.url)
        self.assertTrue(after.data["connected"])

    def test_minting_requires_an_authenticated_member(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.url, {}, format="json")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class TokenUsageLeaderboardTests(APITestCase):
    """The MLAI board: members only, ranked on total tokens, no cost."""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(email="member@example.com")
        self.rival = get_user_model().objects.create_user(email="rival@example.com")
        self.client.force_authenticate(user=self.user)
        self.url = reverse("token_usage_leaderboard")

    def account_for(self, user):
        return TokenUsageAccount.objects.create(
            user=user, token_hash=hashlib.sha256(user.email.encode()).hexdigest()
        )

    def add_usage(
        self,
        account,
        total,
        started_at=None,
        session_id=SESSION_UUID,
        source="claude_code",
        **token_overrides,
    ):
        session = TokenUsageSession.objects.create(
            account=account,
            source=source,
            session_id=session_id,
            model="claude-opus-5",
            input_tokens=total,
            started_at=started_at or timezone.now(),
            **token_overrides,
        )
        if started_at is None:
            self.add_daily_usage(account, total, session_id=session_id)
        return session

    def add_daily_usage(
        self,
        account,
        total,
        usage_date=None,
        session_id=SESSION_UUID,
        source="claude_code",
    ):
        return TokenUsageDailyBucket.objects.create(
            account=account,
            usage_date=usage_date or local_usage_date(),
            source=source,
            session_id=session_id,
            model="claude-opus-5",
            input_tokens=total,
        )

    def test_ranks_by_grand_total_descending(self):
        self.add_usage(self.account_for(self.user), 100)
        self.add_usage(self.account_for(self.rival), 900)

        entries = self.client.get(self.url).data["entries"]

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["rank"], 1)
        self.assertEqual(entries[0]["grand_total"], 900)
        self.assertEqual(entries[1]["grand_total"], 100)

    def test_never_returns_a_cost_field(self):
        self.add_usage(self.account_for(self.user), 100)

        entries = self.client.get(self.url).data["entries"]

        self.assertNotIn("cost", entries[0])

    def test_hidden_members_are_excluded(self):
        account = self.account_for(self.user)
        account.is_public = False
        account.save(update_fields=["is_public"])
        self.add_usage(account, 100)

        response = self.client.get(self.url)

        self.assertEqual(response.data["entries"], [])
        self.assertEqual(response.data["you"]["grand_total"], 100)
        self.assertTrue(response.data["connected"])

    def test_window_filters_older_rows(self):
        account = self.account_for(self.user)
        self.add_usage(account, 100, started_at=timezone.now() - timezone.timedelta(days=40))
        self.add_daily_usage(
            account,
            100,
            usage_date=local_usage_date() - timedelta(days=40),
        )

        entry = self.client.get(self.url, {"window": "7d"}).data["entries"][0]
        self.assertEqual(entry["grand_total"], 0)
        self.assertEqual(entry["sessions"], 0)
        self.assertEqual(len(self.client.get(self.url, {"window": "all"}).data["entries"]), 1)

    def test_history_sessions_use_their_start_date_in_recent_windows(self):
        account = self.account_for(self.user)
        self.add_usage(
            account,
            250,
            started_at=timezone.now() - timedelta(days=3),
        )

        entry = self.client.get(self.url, {"window": "7d"}).data["entries"][0]

        self.assertEqual(entry["grand_total"], 250)
        self.assertEqual(entry["sessions"], 1)

    def test_daily_window_keeps_other_historical_contributors_visible(self):
        self.add_usage(self.account_for(self.user), 100)
        rival = self.account_for(self.rival)
        self.add_usage(
            rival,
            900,
            started_at=timezone.now() - timezone.timedelta(days=40),
            session_id=OTHER_UUID,
        )

        entries = self.client.get(self.url, {"window": "today"}).data["entries"]

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["grand_total"], 100)
        self.assertTrue(entries[0]["has_reported"])
        self.assertEqual(entries[1]["grand_total"], 0)
        self.assertEqual(entries[1]["sessions"], 0)
        self.assertTrue(entries[1]["has_reported"])

    def test_connected_account_without_reported_sessions_remains_visible(self):
        self.account_for(self.rival)

        response = self.client.get(self.url, {"window": "today"})

        self.assertEqual(len(response.data["entries"]), 1)
        self.assertEqual(response.data["entries"][0]["grand_total"], 0)
        self.assertFalse(response.data["entries"][0]["has_reported"])

    @override_settings(TOKEN_USAGE_LEADERBOARD_TIME_ZONE="UTC")
    def test_window_uses_session_start_calendar_and_returns_metadata(self):
        account = self.account_for(self.user)
        anchor = timezone.now().date()
        self.add_usage(account, 300, started_at=timezone.now())
        self.add_usage(
            account,
            700,
            started_at=timezone.now() - timedelta(days=1),
            session_id=OTHER_UUID,
        )

        response = self.client.get(
            self.url,
            {"window": "today", "date": anchor.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["timezone"], "UTC")
        self.assertEqual(response.data["window_basis"], "session_started_at")
        self.assertEqual(response.data["total_basis"], "source_normalized")
        self.assertEqual(response.data["date_from"], anchor.isoformat())
        self.assertEqual(response.data["date_to"], anchor.isoformat())
        self.assertEqual(response.data["entries"][0]["grand_total"], 300)

    def test_future_or_invalid_calendar_anchor_is_rejected(self):
        future = timezone.now().date() + timedelta(days=1)

        invalid = self.client.get(self.url, {"window": "today", "date": "nope"})
        future_response = self.client.get(
            self.url,
            {"window": "today", "date": future.isoformat()},
        )

        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(future_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_window_is_rejected_instead_of_showing_all_time(self):
        response = self.client.get(self.url, {"window": "yesterday-ish"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_daily_sessions_are_distinct_across_sources(self):
        account = self.account_for(self.user)
        self.add_usage(account, 100, source="claude_code")
        self.add_usage(
            account,
            200,
            source="codex",
            session_id=OTHER_UUID,
        )

        entry = self.client.get(self.url, {"window": "today"}).data["entries"][0]

        self.assertEqual(entry["sessions"], 2)
        self.assertEqual(entry["grand_total"], 300)

    def test_codex_cache_is_not_double_counted_in_headline_total(self):
        account = self.account_for(self.user)
        self.add_usage(
            account,
            1_000,
            source="codex",
            output_tokens=100,
            cache_read_tokens=800,
            reasoning_tokens=20,
        )

        entry = self.client.get(self.url, {"window": "all"}).data["entries"][0]

        self.assertEqual(entry["grand_total"], 1_120)
        self.assertEqual(entry["cache_read_tokens"], 800)

    @patch("community_chat.usage_views.fetch_public_tokenmaxer_entries")
    def test_federates_public_tokenmaxer_contributors_without_claiming_membership(
        self, fetch_entries
    ):
        fetch_entries.return_value = [
            {
                "external_id": "tokenmaxer:jackmcpickle",
                "display_name": "jackmcpickle",
                "profile_url": "https://tokenmaxer.quest/u/jackmcpickle",
                "sessions": 12,
                "grand_total": 900,
                "input_tokens": 800,
                "output_tokens": 100,
                "cache_read_tokens": 700,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
            }
        ]
        self.add_usage(self.account_for(self.user), 100)

        response = self.client.get(self.url, {"window": "all"})

        self.assertEqual([entry["origin"] for entry in response.data["entries"]], [
            "tokenmaxer",
            "mlai",
        ])
        external = response.data["entries"][0]
        self.assertEqual(external["display_name"], "jackmcpickle")
        self.assertIsNone(external["public_key"])
        self.assertEqual(
            external["profile_url"],
            "https://tokenmaxer.quest/u/jackmcpickle",
        )
        self.assertEqual(response.data["you"]["rank"], 2)
        self.assertEqual(response.data["scope"], "australia")

    @patch("community_chat.usage_views.fetch_public_tokenmaxer_entries")
    def test_mlai_scope_returns_only_member_rows_with_local_ranks(
        self, fetch_entries
    ):
        fetch_entries.return_value = [
            {
                "external_id": "tokenmaxer:leader",
                "display_name": "Australia leader",
                "profile_url": "https://tokenmaxer.quest/u/leader",
                "sessions": 20,
                "grand_total": 10_000,
                "input_tokens": 9_000,
                "output_tokens": 1_000,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_tokens": 0,
            }
        ]
        self.add_usage(self.account_for(self.user), 100)
        self.add_usage(self.account_for(self.rival), 900)

        response = self.client.get(
            self.url,
            {"scope": "mlai", "window": "all"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["scope"], "mlai")
        self.assertEqual(
            [entry["origin"] for entry in response.data["entries"]],
            ["mlai", "mlai"],
        )
        self.assertEqual(
            [entry["rank"] for entry in response.data["entries"]],
            [1, 2],
        )
        self.assertEqual(response.data["you"]["rank"], 2)
        fetch_entries.assert_not_called()

    def test_invalid_scope_is_rejected(self):
        response = self.client.get(self.url, {"scope": "world"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["error"],
            "scope must be one of: mlai, australia",
        )

    def test_caller_outside_the_cut_still_sees_their_own_rank(self):
        self.add_usage(self.account_for(self.user), 1)
        self.add_usage(self.account_for(self.rival), 900)

        response = self.client.get(self.url, {"limit": 1})

        self.assertEqual(len(response.data["entries"]), 1)
        self.assertIsNotNone(response.data["you"])
        self.assertEqual(response.data["you"]["rank"], 2)

    def test_unconnected_caller_gets_a_board_and_no_you_row(self):
        self.add_usage(self.account_for(self.rival), 900)

        response = self.client.get(self.url)

        self.assertEqual(len(response.data["entries"]), 1)
        self.assertIsNone(response.data["you"])
        self.assertFalse(response.data["connected"])

    def test_sessions_are_counted_distinctly_across_models(self):
        account = self.account_for(self.user)
        self.add_usage(account, 100)
        TokenUsageSession.objects.create(
            account=account,
            source="claude_code",
            session_id=SESSION_UUID,
            model="claude-haiku-4-5",
            input_tokens=50,
            started_at=timezone.now(),
        )

        entry = self.client.get(self.url, {"window": "all"}).data["entries"][0]

        self.assertEqual(entry["sessions"], 1)
        self.assertEqual(entry["grand_total"], 150)

    def test_default_window_is_today(self):
        response = self.client.get(self.url)

        self.assertEqual(response.data["window"], "today")

    def test_board_requires_an_authenticated_member(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(self.url)

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class TokenUsageDailyCorrectionCommandTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="member@example.com")
        self.account = TokenUsageAccount.objects.create(
            user=self.user,
            token_hash=hashlib.sha256(self.user.email.encode()).hexdigest(),
        )
        self.bucket = TokenUsageDailyBucket.objects.create(
            account=self.account,
            usage_date=local_usage_date(),
            source="claude_code",
            session_id=SESSION_UUID,
            model="claude-opus-5",
            input_tokens=5_000_000_000,
        )
        self.session = TokenUsageSession.objects.create(
            account=self.account,
            source="claude_code",
            session_id=SESSION_UUID,
            model="claude-opus-5",
            input_tokens=5_000_000_000,
            started_at=timezone.now(),
        )

    def command_args(self):
        return (
            "correct_token_usage_daily_buckets",
            "--email",
            self.user.email,
            "--usage-date",
            local_usage_date().isoformat(),
        )

    def test_dry_run_reports_without_deleting(self):
        output = StringIO()

        call_command(*self.command_args(), stdout=output)

        self.assertIn('"daily_bucket_rows": 1', output.getvalue())
        self.assertIn('"input_tokens": 5000000000', output.getvalue())
        self.assertTrue(TokenUsageDailyBucket.objects.filter(pk=self.bucket.pk).exists())
        self.assertTrue(TokenUsageSession.objects.filter(pk=self.session.pk).exists())

    def test_apply_deletes_only_daily_buckets_and_preserves_sessions(self):
        output = StringIO()

        call_command(
            *self.command_args(),
            "--apply",
            "--confirm-email",
            self.user.email,
            stdout=output,
        )

        self.assertIn('"remaining_daily_bucket_rows": 0', output.getvalue())
        self.assertFalse(TokenUsageDailyBucket.objects.filter(pk=self.bucket.pk).exists())
        self.assertTrue(TokenUsageSession.objects.filter(pk=self.session.pk).exists())

    def test_apply_requires_matching_confirmation(self):
        with self.assertRaisesMessage(CommandError, "--confirm-email must exactly match"):
            call_command(*self.command_args(), "--apply")

    def test_unknown_account_is_rejected(self):
        with self.assertRaisesMessage(CommandError, "No token-usage account"):
            call_command(
                "correct_token_usage_daily_buckets",
                "--email",
                "missing@example.com",
                "--usage-date",
                local_usage_date().isoformat(),
            )
