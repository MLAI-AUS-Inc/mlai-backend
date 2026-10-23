"""Database-free checks for the listmonk client: requests, errors and query quoting.

HTTP never leaves the process: ``integrations.http_client.request`` is mocked.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from crm import listmonk
from crm.listmonk import ListmonkError

LISTMONK = {
    "LISTMONK_BASE_URL": "http://listmonk:9000",
    "LISTMONK_API_USER": "mlai-backend",
    "LISTMONK_API_TOKEN": "synthetic-token",
}


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok(data):
    return FakeResponse(200, {"data": data})


@override_settings(**LISTMONK)
class ListmonkClientTests(SimpleTestCase):
    def call(self, responses, fn, *args, **kwargs):
        with patch("crm.listmonk.http_client.request", side_effect=responses) as request:
            result = fn(*args, **kwargs)
        return result, request.call_args_list

    def test_requests_use_basic_auth_and_the_configured_host(self):
        list_id, calls = self.call([ok({"id": 12})], listmonk.create_list, "crm-blast-1", tags=["crm"])
        self.assertEqual(list_id, 12)
        [(args, kwargs)] = [(call.args, call.kwargs) for call in calls]
        self.assertEqual(args, ("POST", "http://listmonk:9000/api/lists"))
        self.assertEqual(kwargs["auth"], ("mlai-backend", "synthetic-token"))
        self.assertEqual(
            kwargs["json"],
            {"name": "crm-blast-1", "type": "private", "optin": "single", "tags": ["crm"], "description": ""},
        )
        self.assertEqual(kwargs["timeout"], listmonk.TIMEOUT)

    def test_find_by_exact_name(self):
        found, _ = self.call(
            [ok({"results": [{"id": 3, "name": "crm-blast-10"}, {"id": 4, "name": "crm-blast-1"}], "total": 2})],
            listmonk.find_list,
            "crm-blast-1",
        )
        self.assertEqual(found, 4)
        missing, _ = self.call([ok({"results": [], "total": 0})], listmonk.find_campaign, "crm-blast-1")
        self.assertIsNone(missing)
        campaign, calls = self.call(
            [ok({"results": [{"id": 9, "name": "crm-blast-1", "status": "draft"}]})],
            listmonk.find_campaign,
            "crm-blast-1",
        )
        self.assertEqual(campaign["id"], 9)
        self.assertEqual(calls[0].kwargs["params"]["query"], "crm-blast-1")

    def test_subscriber_queries_page_until_done(self):
        page = lambda ids: ok({"results": [{"id": value, "email": f"p{value}@example.com"} for value in ids], "total": 3})
        found, calls = self.call(
            [page([1, 2]), page([3])], listmonk.query_subscribers, "subscribers.id > 0", per_page=2
        )
        self.assertEqual([item["id"] for item in found], [1, 2, 3])
        self.assertEqual([call.kwargs["params"]["page"] for call in calls], [1, 2])
        self.assertEqual(calls[0].kwargs["params"]["query"], "subscribers.id > 0")
        with self.assertRaises(ListmonkError) as caught:
            self.call([page([1, 2])] * 3, listmonk.query_subscribers, "x", per_page=2, max_pages=1)
        self.assertEqual(caught.exception.code, "too_many_pages")

    def test_subscriber_and_campaign_writes(self):
        _, calls = self.call(
            [ok({"id": 5, "email": "ada@example.com"})],
            listmonk.create_subscriber,
            "ada@example.com",
            "Ada",
            lists=[12],
            attribs={"crm_contact_id": "c"},
        )
        self.assertEqual(
            calls[0].kwargs["json"],
            {
                "email": "ada@example.com",
                "name": "Ada",
                "status": "enabled",
                "lists": [12],
                "attribs": {"crm_contact_id": "c"},
                "preconfirm_subscriptions": True,
            },
        )
        _, calls = self.call([ok(True)], listmonk.add_subscribers_to_list, [5, 3, 5], 12)
        self.assertEqual(
            (calls[0].args, calls[0].kwargs["json"]),
            (
                ("PUT", "http://listmonk:9000/api/subscribers/lists"),
                {"ids": [3, 5], "action": "add", "target_list_ids": [12], "status": "confirmed"},
            ),
        )
        _, calls = self.call([], listmonk.add_subscribers_to_list, [], 12)
        self.assertEqual(calls, [])
        _, calls = self.call([ok(True)], listmonk.blocklist_subscribers, [7])
        self.assertEqual((calls[0].args[1], calls[0].kwargs["json"]), ("http://listmonk:9000/api/subscribers/blocklist", {"ids": [7]}))
        _, calls = self.call([ok({"id": 9})], listmonk.set_campaign_status, 9, "running")
        self.assertEqual((calls[0].args, calls[0].kwargs["json"]), (("PUT", "http://listmonk:9000/api/campaigns/9/status"), {"status": "running"}))
        _, calls = self.call([ok(True)], listmonk.test_campaign, 9, {"name": "crm-blast-1"}, ["sam@mlai.au"])
        self.assertEqual(calls[0].kwargs["json"], {"name": "crm-blast-1", "subscribers": ["sam@mlai.au"]})
        html, calls = self.call([FakeResponse(200, None, text="<h1>Hi</h1>")], listmonk.preview_campaign, 9)
        self.assertEqual((html, calls[0].args[1]), ("<h1>Hi</h1>", "http://listmonk:9000/api/campaigns/9/preview"))

    def test_errors_tell_outages_from_refusals(self):
        cases = [
            (requests.exceptions.Timeout("slow"), "timeout", True, False),
            (requests.exceptions.ConnectionError("down"), "connection_error", True, False),
            (FakeResponse(503, {"message": "down"}), "http_503", True, False),
            (FakeResponse(429, {"message": "slow down"}), "http_429", True, False),
            (FakeResponse(401, {"message": "nope"}), "http_401", False, True),
            (FakeResponse(400, {"message": "bad"}), "http_400", False, False),
            (FakeResponse(200, None, text="<html>"), "unreadable_response", True, False),
            (ok({"no_id": True}), "unreadable_response", True, False),
        ]
        for response, code, transient, rejected in cases:
            with self.subTest(code=code):
                with self.assertRaises(ListmonkError) as caught:
                    self.call([response], listmonk.create_list, "crm-blast-1")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.transient, transient)
                self.assertEqual(caught.exception.credentials_rejected, rejected)
        with self.assertRaises(ListmonkError) as caught:
            self.call([FakeResponse(409, {"message": "E-mail already exists."})], listmonk.create_subscriber, "a@b.c", "A")
        self.assertTrue(caught.exception.conflict)

    @override_settings(LISTMONK_API_TOKEN="")
    def test_nothing_is_sent_without_credentials(self):
        self.assertFalse(listmonk.configured())
        with patch("crm.listmonk.http_client.request") as request, self.assertRaises(ListmonkError) as caught:
            listmonk.get_campaign(1)
        self.assertEqual(caught.exception.code, "not_configured")
        request.assert_not_called()

    def test_template_id(self):
        with override_settings(LISTMONK_TEMPLATE_ID=4):
            self.assertEqual(listmonk.template_id(), 4)
        with override_settings(LISTMONK_TEMPLATE_ID=None):
            self.assertIsNone(listmonk.template_id())


class QueryValueTests(SimpleTestCase):
    def test_strings_are_quoted_and_unsafe_values_refused(self):
        self.assertEqual(listmonk.sql_string("o'brien@example.com"), "'o''brien@example.com'")
        for unsafe in ("a\\'@b.c", "a\n@b.c", "a\x00@b.c"):
            with self.subTest(value=repr(unsafe)), self.assertRaises(ListmonkError):
                listmonk.sql_string(unsafe)

    def test_timestamps_and_ids(self):
        self.assertEqual(
            listmonk.sql_timestamp(datetime(2026, 10, 23, 1, 2, 3, tzinfo=timezone.utc)),
            "'2026-10-23 01:02:03+00'::timestamptz",
        )
        self.assertEqual(listmonk.sql_timestamp(datetime(2026, 10, 23)), "'2026-10-23 00:00:00+00'::timestamptz")
        self.assertEqual(listmonk.sql_ids([3, 1, 3]), "(1,3)")
        with self.assertRaises(ValueError):
            listmonk.sql_ids(["1; DROP"])
        with self.assertRaises(ListmonkError):
            listmonk.sql_ids([])
