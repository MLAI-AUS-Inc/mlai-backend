"""Agent ranking contract checks without database access or migrations."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.token_agent_leaderboard import agent_leaderboard
from community_chat.token_usage import TOKEN_FIELDS
from community_chat.usage_views import (
    TokenUsageLeaderboardView,
    _aggregate_entries,
    _empty_entry,
)


def usage(source, total, sessions=1):
    return {"source": source, "grand_total": total, "sessions": sessions}


def group(account_id, source, **counts):
    return {
        "account_id": account_id, "source": source, "sessions": 1,
        **{field: 0 for field in TOKEN_FIELDS}, **counts,
    }


class AgentLeaderboardTests(SimpleTestCase):
    def test_ranks_agents_and_counts_each_participant_once_per_source(self):
        ranked = agent_leaderboard([
            {"source_totals": [usage("codex", 200), usage("claude_code", 100)]},
            {"source_totals": [usage("codex", 50), usage("codex", 25)]},
            {"source_totals": [usage("cursor", 0)]},
            {},
        ])
        self.assertEqual(ranked, [
            {"rank": 1, "source": "codex", "display_name": "Codex",
             "grand_total": 275, "sessions": 3, "participants": 2},
            {"rank": 2, "source": "claude_code", "display_name": "Claude Code",
             "grand_total": 100, "sessions": 1, "participants": 1},
        ])

    def test_ties_and_new_agents_have_stable_labels_and_ranks(self):
        ranked = agent_leaderboard([{"source_totals": [
            usage("new_agent", 100), usage("pi", 100), usage("cursor", 100),
            usage("opencode", 100),
        ]}])
        self.assertEqual([row["display_name"] for row in ranked],
                         ["Cursor", "New Agent", "OpenCode", "Pi"])
        self.assertEqual([row["rank"] for row in ranked], [1, 2, 3, 4])
        self.assertEqual(agent_leaderboard([]), [])

    def test_local_source_totals_use_existing_cache_normalization(self):
        rows = Mock()
        rows.values.return_value.annotate.return_value = [
            group(1, "codex", input_tokens=1000, output_tokens=100,
                  cache_read_tokens=800, reasoning_tokens=20, sessions=2),
            group(1, "claude_code", input_tokens=50, output_tokens=10,
                  cache_read_tokens=200, cache_creation_tokens=30),
            group(2, "pi", input_tokens=1000, cache_read_tokens=800),
        ]
        initial = {1: _empty_entry(1, has_reported=False)}
        entries = _aggregate_entries(rows, initial)
        self.assertEqual(entries[0]["source_totals"], [
            usage("codex", 1120, 2), usage("claude_code", 290),
        ])
        self.assertEqual(entries[0]["grand_total"], 1410)
        self.assertEqual(entries[1]["grand_total"], 1000)
        self.assertEqual(initial[1]["source_totals"], [])
        self.assertEqual(sum(row["grand_total"] for row in agent_leaderboard(entries)), 2410)


@override_settings(TOKEN_USAGE_LEADERBOARD_TIME_ZONE="UTC")
class AgentLeaderboardResponseTests(SimpleTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        self.member = SimpleNamespace(id=1, user_id=1, is_public=True)
        self.user = SimpleNamespace(id=99, is_authenticated=True)
        self.hidden = SimpleNamespace(id=99, user_id=99, is_public=False)
        self.public_rows = Mock()
        self.public_rows.filter.return_value = self.public_rows
        self.public_rows.values_list.return_value = [1]
        self.public_rows.values.return_value.annotate.return_value = [
            group(1, "claude_code", input_tokens=100),
        ]
        self.hidden_rows = Mock()
        self.hidden_rows.filter.return_value = self.hidden_rows
        self.hidden_rows.values.return_value.annotate.return_value = [
            group(99, "pi", input_tokens=999999),
        ]
        self.external = {
            "external_id": "tokenmaxer:external", "display_name": "External",
            "profile_url": "https://tokens.example/u/external",
            "sessions": 1, "grand_total": 200,
            "source_totals": [usage("codex", 200)],
            **{field: 0 for field in TOKEN_FIELDS},
        }

    def request(self, **params):
        def accounts(**filters):
            result = Mock()
            result.select_related.return_value = [self.member]
            result.first.return_value = self.hidden
            return result

        def sessions(**filters):
            return self.hidden_rows if "account" in filters else self.public_rows

        def profile(account, pubkeys):
            return {"public_id": str(account.id), "display_name": f"Member {account.id}",
                    "avatar_url": None, "public_key": None}

        with (
            patch("community_chat.usage_views.timezone.now", return_value=self.now),
            patch("community_chat.usage_views.TokenUsageAccount.objects.filter", side_effect=accounts),
            patch("community_chat.usage_views.TokenUsageSession.objects.filter", side_effect=sessions) as filtered,
            patch("community_chat.usage_views._public_keys_for", return_value={}),
            patch("community_chat.usage_views._member_payload", side_effect=profile),
            patch("community_chat.usage_views.fetch_public_tokenmaxer_entries", return_value=[self.external]) as external,
        ):
            request = APIRequestFactory().get("/usage/leaderboard/", params)
            force_authenticate(request, user=self.user)
            response = TokenUsageLeaderboardView.as_view(throttle_classes=[])(request)
            self.assertEqual(response.status_code, 200)
            filtered.assert_any_call(account__is_public=True, started_at__lte=self.now)
            return response.data, external

    def test_scope_pagination_and_hidden_caller_do_not_distort_agent_ranking(self):
        data, external = self.request(scope="australia", window="7d", limit=1)
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["you"]["grand_total"], 999999)
        self.assertEqual([row["source"] for row in data["agents"]], ["codex", "claude_code"])
        self.assertEqual([row["grand_total"] for row in data["agents"]], [200, 100])
        external.assert_called_once_with("7d")
        self.public_rows.filter.assert_called_once_with(
            started_at__gte=datetime(2026, 9, 19, tzinfo=timezone.utc),
            started_at__lt=datetime(2026, 9, 26, tzinfo=timezone.utc),
        )

    def test_mlai_scope_and_historical_anchors_exclude_federation(self):
        for params in [
            {"scope": "mlai", "window": "all"},
            {"scope": "australia", "window": "today", "date": "2026-09-24"},
        ]:
            with self.subTest(params=params):
                data, external = self.request(**params)
                self.assertEqual([row["source"] for row in data["agents"]], ["claude_code"])
                external.assert_not_called()

    def test_empty_public_period_returns_no_agents_even_with_private_usage(self):
        self.public_rows.values.return_value.annotate.return_value = []
        data, _ = self.request(scope="mlai")
        self.assertEqual(data["agents"], [])

