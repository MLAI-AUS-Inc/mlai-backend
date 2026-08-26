from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from community_chat.tokenmaxer_federation import (
    _fold_source_payloads,
    fetch_public_tokenmaxer_entries,
)


class TokenmaxerFederationTests(SimpleTestCase):
    def tearDown(self):
        cache.clear()

    @override_settings(TOKENMAXER_PUBLIC_API_BASE="https://tokens.example")
    def test_folds_sources_and_normalizes_inclusive_cache(self):
        entries = _fold_source_payloads(
            {
                "codex": {
                    "entries": [
                        {
                            "username": "Jack",
                            "sessions": 2,
                            "input_tokens": 1_000,
                            "output_tokens": 100,
                            "cache_read_tokens": 800,
                            "cache_creation_tokens": 0,
                            "reasoning_tokens": 20,
                        }
                    ]
                },
                "claude_code": {
                    "entries": [
                        {
                            "username": "jack",
                            "sessions": 1,
                            "input_tokens": 50,
                            "output_tokens": 10,
                            "cache_read_tokens": 200,
                            "cache_creation_tokens": 30,
                            "reasoning_tokens": 0,
                        }
                    ]
                },
            }
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["sessions"], 3)
        self.assertEqual(entries[0]["grand_total"], 1_410)
        self.assertEqual(entries[0]["cache_read_tokens"], 1_000)
        self.assertEqual(entries[0]["external_id"], "tokenmaxer:jack")

    @override_settings(
        TOKENMAXER_FEDERATION_ENABLED=True,
        TOKENMAXER_PUBLIC_API_BASE="https://tokens.example",
        TOKENMAXER_FEDERATION_CACHE_SECONDS=60,
        TOKENMAXER_FEDERATION_TIMEOUT_SECONDS=1,
    )
    @patch("community_chat.tokenmaxer_federation.requests.get")
    def test_fetches_each_source_and_reuses_cache(self, get):
        response = Mock()
        response.json.return_value = {"entries": []}
        response.raise_for_status.return_value = None
        get.return_value = response

        self.assertEqual(fetch_public_tokenmaxer_entries("all"), [])
        self.assertEqual(fetch_public_tokenmaxer_entries("all"), [])

        self.assertEqual(get.call_count, 5)

    @override_settings(
        TOKENMAXER_FEDERATION_ENABLED=True,
        TOKENMAXER_PUBLIC_API_BASE="https://tokens.example",
        TOKENMAXER_FEDERATION_CACHE_SECONDS=60,
        TOKENMAXER_FEDERATION_TIMEOUT_SECONDS=1,
    )
    @patch("community_chat.tokenmaxer_federation.requests.get")
    def test_upstream_failure_uses_last_good_snapshot(self, get):
        cache.set(
            "community-chat:tokenmaxer:today:stale:v1",
            [{"external_id": "tokenmaxer:dave"}],
            timeout=60,
        )
        get.side_effect = requests.ConnectionError("network down")

        self.assertEqual(
            fetch_public_tokenmaxer_entries("today"),
            [{"external_id": "tokenmaxer:dave"}],
        )
