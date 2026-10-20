"""Endpoint tests. The committee check is patched so these need no database."""

from __future__ import annotations

import hashlib
from datetime import date
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from integrations.api_views_connectors import NotionAgendaItemView, NotionAgendaReceiptView
from integrations.services.notion_agenda_client import NotionAgendaError
from integrations.tests_notion_agenda import ITEM, TITLE, _FakeClient, _template

SERVICE = "integrations.services.notion_agenda"
TODAY = date(2026, 10, 26)


class _LaggingSearchClient(_FakeClient):
    """Notion search that never shows pages created during the test."""

    def find_pages_by_title(self, title):
        self.calls.append(("find", title))
        return []


def _payload(**overrides):
    payload = {
        "idempotency_key": "slack:T1:C1:1761400000.000100",
        "requester_slack_id": "U-COMMITTEE",
        "owner_name": "Alex",
        "meeting_date": "2026-10-27",
        "item": ITEM,
    }
    payload.update(overrides)
    return payload


@override_settings(
    NOTION_AGENDA_ENABLED=True,
    NOTION_AGENDA_DATA_SOURCE_ID="ds-1",
    NOTION_AGENDA_TEMPLATE_POLL_ATTEMPTS=1,
    ROO_API_KEY="roo-api-key",
    INTERNAL_API_KEY="",
    MLAI_API_KEY="",
)
class NotionAgendaEndpointTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.client_fake = _FakeClient({"page-1": _template()})
        self.committee = {"U-COMMITTEE"}
        patches = [
            patch(f"{SERVICE}.NotionAgendaClient", side_effect=lambda: self.client_fake),
            patch(f"{SERVICE}.can_add_meeting_agenda_items", side_effect=lambda slack_id: slack_id in self.committee),
            patch(f"{SERVICE}._melbourne_today", return_value=TODAY),
            patch(f"{SERVICE}.time.sleep"),
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

    def _post(self, payload, api_key="roo-api-key"):
        request = self.factory.post("/api/v1/integrations/notion/agenda-items", payload, format="json", HTTP_X_API_KEY=api_key)
        return NotionAgendaItemView.as_view()(request)

    def _receipt(self, key):
        request = self.factory.get(f"/api/v1/integrations/notion/agenda-items/receipts/{key}", HTTP_X_API_KEY="roo-api-key")
        return NotionAgendaReceiptView.as_view()(request, idempotency_key=key)

    def test_added_item_returns_201_with_link_and_block(self):
        response = self._post(_payload())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "added")
        self.assertEqual(response.data["page_url"], "https://notion.test/page-1")
        self.assertEqual(response.data["block_id"], "inserted-1")
        self.assertFalse(response.data["replayed"])

    @override_settings(NOTION_AGENDA_ENABLED=False)
    def test_disabled_flag_refuses_before_notion(self):
        response = self._post(_payload())

        self.assertEqual((response.status_code, response.data["code"]), (403, "notion_agenda_disabled"))
        self.assertEqual(self.client_fake.calls, [])

    def test_internal_api_key_is_rejected(self):
        with override_settings(INTERNAL_API_KEY="internal-key"):
            response = self._post(_payload(), api_key="internal-key")
        # DRF answers 401 because no authenticator accepted the request.
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client_fake.calls, [])

    def test_non_committee_requester_is_refused_without_notion_calls(self):
        response = self._post(_payload(requester_slack_id="U-MEMBER"))

        self.assertEqual((response.status_code, response.data["code"]), (403, "agenda_requester_not_allowed"))
        self.assertEqual(self.client_fake.calls, [])

    def test_same_key_retry_replays_without_calling_notion(self):
        self._post(_payload())
        calls_after_first = len(self.client_fake.calls)

        response = self._post(_payload())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["replayed"])
        self.assertEqual(response.data["block_id"], "inserted-1")
        self.assertEqual(len(self.client_fake.calls), calls_after_first)

    def test_same_key_with_reworded_item_still_replays(self):
        self._post(_payload())

        response = self._post(_payload(item="monthly check ins for presidents and committee"))

        self.assertTrue(response.data["replayed"])
        self.assertEqual(len(self.client_fake.writes()), 1)

    def test_same_key_for_another_date_is_rejected(self):
        self._post(_payload())

        response = self._post(_payload(meeting_date="2026-11-03"))

        self.assertEqual((response.status_code, response.data["code"]), (409, "idempotency_key_reused"))

    def test_invalid_requests_are_400(self):
        cases = [
            (_payload(extra="x"), "invalid_request"),
            (_payload(meeting_date="27/10/2026"), "invalid_request"),
            (_payload(meeting_date="2027-12-01"), "invalid_meeting_date"),
            (_payload(meeting_date="2026-10-24"), "invalid_meeting_date"),
            (_payload(idempotency_key="short"), "invalid_request"),
            (_payload(item="   "), "invalid_request"),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload):
                response = self._post(payload)
                self.assertEqual((response.status_code, response.data["code"]), (400, code))
        self.assertEqual(self.client_fake.writes(), [])

    def test_ambiguous_date_lets_the_follow_up_reuse_the_key(self):
        self.client_fake = _FakeClient({"page-1": _template(), "page-2": _template()})

        first = self._post(_payload())
        second = self._post(_payload(page_id="page-2"))

        self.assertEqual((first.status_code, first.data["status"]), (200, "ambiguous"))
        self.assertEqual((second.status_code, second.data["status"]), (201, "added"))
        self.assertEqual(self.client_fake.writes(), [("insert", "page-2", "h-agenda")])

    def test_held_date_lock_returns_busy(self):
        digest = hashlib.sha256(b"ds-1:2026-10-27").hexdigest()
        cache.add(f"notion-agenda-lock:{digest}", "someone-else", timeout=60)

        response = self._post(_payload())

        self.assertEqual((response.status_code, response.data["code"]), (409, "agenda_busy"))
        self.assertEqual(self.client_fake.calls, [])

    def test_uncertain_write_then_same_key_retry_reports_duplicate(self):
        self.client_fake.insert_error = NotionAgendaError("notion_unavailable", "Notion is unavailable.", uncertain=True)
        first = self._post(_payload())
        self.client_fake.insert_error = None

        second = self._post(_payload())

        self.assertEqual(first.status_code, 502)
        self.assertTrue(first.data["uncertain"])
        self.assertEqual((second.status_code, second.data["status"]), (200, "duplicate"))
        self.assertEqual(len(self.client_fake.writes()), 1)

    def test_rate_limit_is_503_with_retry_after(self):
        self.client_fake.insert_error = NotionAgendaError("notion_rate_limited", "Notion is rate limiting requests.", retry_after=7)

        response = self._post(_payload())

        self.assertEqual((response.status_code, response.data["code"]), (503, "notion_rate_limited"))
        self.assertEqual(response["Retry-After"], "7")

    def test_page_created_moments_ago_is_reused_when_search_lags(self):
        self.client_fake = _LaggingSearchClient()

        first = self._post(_payload())
        second = self._post(_payload(idempotency_key="slack:T1:C1:1761400999.000200", item="Budget review"))

        self.assertTrue(first.data["created_page"])
        self.assertEqual((second.data["status"], second.data["page_id"]), ("added", "new-page"))
        self.assertEqual([call for call in self.client_fake.writes() if call[0] == "create"], [("create", TITLE)])

    def test_receipt_lookup(self):
        key = _payload()["idempotency_key"]
        self.assertEqual(self._receipt(key).status_code, 404)

        self._post(_payload())
        response = self._receipt(key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "completed")
        self.assertEqual(response.data["result"]["block_id"], "inserted-1")
        self.assertEqual(self._receipt("bad key!").status_code, 400)
