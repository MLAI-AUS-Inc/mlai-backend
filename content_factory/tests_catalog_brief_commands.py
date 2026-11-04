"""Real bearer/PG at-most-once suggestion claims; only external HTTP is faked."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
import requests

from founder_tools import startup_team
from . import tests_catalog_commands as catalog_tests
from .catalog_commands import KEY, MAX_COMMANDS
from .catalog_brief_commands import MAX_RESPONSE_BYTES, MAX_SUGGESTIONS, _read_response
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .models import OrganizationContentConfig


class RawBody:
    def __init__(self, body): self.body, self.closed, self.released = body, False, False
    def stream(self, amt, decode_content=True):
        if amt != 1: raise AssertionError("The reader must check its elapsed bound per byte")
        yield from (self.body[i:i+amt] for i in range(len(self.body)))
    def close(self): self.closed = True
    def release_conn(self): self.released = True


def remote_response(body, status=200):
    value = requests.Response(); value.status_code = status
    value.raw = RawBody(json.dumps(body).encode() if not isinstance(body, bytes) else body)
    return value


@override_settings(**ALL_P1_FLAGS_ON, CONTENT_FACTORY_URL="https://cf.fixture.invalid", CONTENT_FACTORY_API_KEY="synthetic-local-only")
class CatalogBriefCommandTests(TransactionTestCase):
    setUp = catalog_tests.CatalogCommandTests.setUp
    strategy = catalog_tests.CatalogCommandTests.strategy
    initial = catalog_tests.CatalogCommandTests.initial
    current = catalog_tests.CatalogCommandTests.current
    url = catalog_tests.CatalogCommandTests.url
    send = catalog_tests.CatalogCommandTests.send
    assert_no_spend = catalog_tests.CatalogCommandTests.assert_no_spend

    def body(self):
        return {"topic": "Choose a bounded workflow", "country": "AU", "audience_id": None,
            "expected_editorial_catalog_version": self.current()["editorial_catalog_version"]}

    def suggestion(self, body, brief=None):
        return {"topic": body["topic"].strip(), "editorial_catalog_version": body["expected_editorial_catalog_version"],
            "brief": brief, "rationale": "Review the current audience before starting an article."}

    def post(self, body, key="retained-brief-command", client=None):
        return (client or self.client).post(f"{self.base}suggest-brief/?company_id={self.company.pk}", body,
            format="json", **({"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}))

    def test_known_ack_and_lost_client_ack_replay_one_external_call_original_suggestion(self):
        body = self.body(); output = self.suggestion(body)
        def remote(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(next(iter(self.strategy()[KEY].values()))["state"], "pending")
            self.assertEqual(kwargs["json"]["domain"], self.org.domain)
            self.assertFalse(kwargs["allow_redirects"])
            return remote_response(output)
        with patch("requests.post", side_effect=remote) as outbound:
            first = self.post(body); cache.clear(); before = self.strategy(); replay = self.post(body)
        self.assertEqual(first.status_code, 200, first.data); self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data, output); self.assertEqual(replay.data, output)
        self.assertEqual(replay["Idempotent-Replayed"], "true"); self.assertEqual(self.strategy(), before)
        self.assertEqual(outbound.call_count, 1); self.assert_no_spend()

    def test_unknown_remote_ack_is_permanently_held_without_a_second_call(self):
        body = self.body()
        with patch("requests.post", side_effect=requests.Timeout("synthetic lost ACK")) as outbound:
            first = self.post(body); replay = self.post(body)
        self.assertEqual([first.status_code, replay.status_code], [409, 409])
        self.assertEqual(outbound.call_count, 1)
        self.assertEqual(next(iter(self.strategy()[KEY].values()))["state"], "held")
        self.assert_no_spend()

    def test_crash_after_committed_claim_before_http_never_retries_model(self):
        body = self.body()
        with patch("requests.post", side_effect=KeyboardInterrupt("synthetic worker crash")):
            with self.assertRaises(KeyboardInterrupt): self.post(body)
        before = self.strategy(); self.assertEqual(next(iter(before[KEY].values()))["state"], "pending")
        with patch("requests.post", side_effect=AssertionError("No second model work")):
            replay = self.post(body)
        self.assertEqual(replay.status_code, 409); self.assertEqual(self.strategy(), before)

    def test_concurrent_same_key_has_one_remote_effect_and_pending_second_response(self):
        body = self.body(); started, release = Event(), Event(); responses, errors = [], []
        auth = dict(self.client._credentials)
        def remote(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block); started.set()
            if not release.wait(10): raise AssertionError("Main request failed to release remote")
            return remote_response(self.suggestion(body))
        def worker():
            try:
                from rest_framework.test import APIClient
                client = APIClient(); client.credentials(**auth)
                result = self.post(body, client=client); responses.append(result.status_code)
            except BaseException as exc: errors.append(repr(exc))
            finally: connections.close_all()
        with patch("requests.post", side_effect=remote) as outbound:
            thread = Thread(target=worker); thread.start()
            try:
                self.assertTrue(started.wait(10)); second = self.post(body)
                self.assertEqual(second.status_code, 409, second.data)
            finally: release.set(); thread.join(15)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, []); self.assertEqual(responses, [200])
        self.assertEqual(outbound.call_count, 1)

    def test_changed_body_or_action_conflicts_without_remote_and_current_catalog_change_refuses_replay(self):
        body = self.body()
        with patch("requests.post", return_value=remote_response(self.suggestion(body))) as outbound:
            self.assertEqual(self.post(body).status_code, 200)
            self.assertEqual(self.post({**body, "topic": "Different retained topic"}).status_code, 409)
            local = self.send(self.initial(), key="retained-brief-command")
            self.assertEqual(local.status_code, 409, local.data)
            self.assertEqual(self.send(self.initial(), key="different-edit").status_code, 200)
            self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual(outbound.call_count, 1)

    def test_frozen_or_invalid_roster_never_starts_external_work(self):
        body = self.body()
        for roster in ({"bad": True}, {"schema_version": 1, "frozen": True}):
            config = OrganizationContentConfig.objects.get(organization=self.org)
            config.pillar_strategy = {**config.pillar_strategy, startup_team.KEY: roster}; config.save()
            with patch("requests.post") as outbound:
                result = self.post(body); self.assertIn(result.status_code, (403, 404)); outbound.assert_not_called()

    def test_owner_deactivated_during_external_request_cannot_receive_result_or_repeat_work(self):
        body = self.body()
        def remote(*args, **kwargs):
            type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
            return remote_response(self.suggestion(body))
        with patch("requests.post", side_effect=remote) as outbound:
            result = self.post(body); retry = self.post(body)
        self.assertEqual(result.status_code, 403); self.assertEqual(retry.status_code, 401)
        self.assertEqual(outbound.call_count, 1)
        self.assertEqual(next(iter(self.strategy()[KEY].values()))["state"], "pending")

    def test_catalog_changed_during_http_holds_result_and_does_not_restore_old_strategy(self):
        body = self.body()
        def remote(*args, **kwargs):
            self.assertEqual(self.send(self.initial(), key="edit-during-model").status_code, 200)
            return remote_response(self.suggestion(body))
        with patch("requests.post", side_effect=remote) as outbound:
            result = self.post(body); before = self.strategy(); retry = self.post(body)
        self.assertEqual([result.status_code, retry.status_code], [409, 409]); self.assertEqual(outbound.call_count, 1)
        self.assertEqual(self.strategy(), before); self.assertIn("editorial_catalog", before)

    def test_malformed_oversized_and_refused_remote_results_hold_one_claim_each(self):
        body = self.body()
        variants = [remote_response({"rationale": "Incomplete"}), remote_response(b"x" * (MAX_RESPONSE_BYTES + 1)),
            remote_response(b"\xff"), remote_response(self.suggestion(body), status=503),
            remote_response({**self.suggestion(body), "editorial_catalog_version": True})]
        for i, response in enumerate(variants):
            with patch("requests.post", return_value=response) as outbound:
                key = f"bad-result-{i}"; first = self.post(body, key); replay = self.post(body, key)
            self.assertEqual([first.status_code, replay.status_code], [409, 409]); self.assertEqual(outbound.call_count, 1)
            self.assertTrue(response.raw.closed or response.raw.released)

    def test_exact_accepted_brief_resolves_current_approved_audience_and_schema_export(self):
        first = self.send({"description": "Builders testing their first workflow", "expected_editorial_catalog_version": 0}, action="first_audience", key="first-audience")
        self.assertEqual(first.status_code, 201, first.data)
        body = self.body(); body["audience_id"] = first.data["audience_id"]
        output = self.suggestion(body, first.data["editorial_brief"])
        with patch("requests.post", return_value=remote_response(output)) as outbound:
            response = self.post(body); replay = self.post(body)
        self.assertEqual(response.status_code, 200, response.data); self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(outbound.call_count, 1)
        from .founder_journey_contract import _validate
        schema = json.loads((Path(__file__).resolve().parent.parent / "contracts/founder-article-v2.schema.json").read_text())
        definitions = {name: catalog_tests.wire_rule(rule) for name, rule in schema["$defs"].items()}
        _validate(body, definitions["FounderEditorialBriefSuggestionRequest"], definitions)
        projected = catalog_tests.projection(response.data, definitions["FounderEditorialBriefSuggestionResponse"], definitions)
        _validate(projected, definitions["FounderEditorialBriefSuggestionResponse"], definitions)
        target = os.environ.get("FOUNDER_EXPORT_CATALOG_BRIEF")
        if target: Path(target).write_text(json.dumps({"source": "Actual bearer/My startup HTTP/UTF8 PG; only external CF suggestion HTTP faked with actual CF four-field response schema", "company_id": str(self.company.pk), "request": body, "response": response.data, "status": response.status_code, "replay_status": replay.status_code, "external_calls": outbound.call_count}, indent=2) + "\n")

    def test_wrong_audience_or_country_response_is_not_exposed_or_retried(self):
        first = self.send({"description": "Builders testing a small workflow", "expected_editorial_catalog_version": 0}, action="first_audience", key="first")
        body = self.body(); output = self.suggestion(body, {**first.data["editorial_brief"], "country": "NZ"})
        with patch("requests.post", return_value=remote_response(output)) as outbound:
            self.assertEqual(self.post(body).status_code, 409); self.assertEqual(self.post(body).status_code, 409)
        self.assertEqual(outbound.call_count, 1)

    def test_existing_transaction_or_invalid_request_starts_no_http_and_no_claim(self):
        body = self.body()
        with patch("requests.post") as outbound:
            with transaction.atomic(): self.assertEqual(self.post(body).status_code, 503)
            self.assertEqual(self.post({**body, "extra": "unsafe"}).status_code, 400)
            self.assertEqual(self.post({**body, "expected_editorial_catalog_version": True}).status_code, 400)
            outbound.assert_not_called()
        self.assertNotIn(KEY, self.strategy())

    def test_retained_result_replays_with_admission_flag_disabled_and_cap_refuses_new_work(self):
        body = self.body()
        with patch("requests.post", return_value=remote_response(self.suggestion(body))) as outbound:
            self.assertEqual(self.post(body).status_code, 200)
            from .catalog_commands import _digest
            config = OrganizationContentConfig.objects.get(organization=self.org); strategy = config.pillar_strategy
            template = next(iter(strategy[KEY].values()))
            for i in range(MAX_SUGGESTIONS - 1): strategy[KEY][_digest({"synthetic": i})] = deepcopy(template)
            config.pillar_strategy = strategy; config.save()
            self.assertEqual(self.post(body, key="new-at-cap").status_code, 409)
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                self.assertEqual(self.post(body).status_code, 200)
                self.assertEqual(self.post(body, key="disabled-new-suggestion").status_code, 409)
        self.assertEqual(outbound.call_count, 1)

    def test_legacy_no_key_still_uses_original_response_and_twelve_per_hour_throttle(self):
        body = self.body(); output = self.suggestion(body)
        legacy = remote_response(output); legacy._content = json.dumps(output).encode(); legacy._content_consumed = True
        with patch("requests.post", return_value=legacy) as outbound:
            result = self.post(body, key=None)
        self.assertEqual(result.status_code, 200, result.data); self.assertEqual(result.data, output)
        self.assertEqual(outbound.call_args.kwargs["timeout"], 100)
        self.assertNotIn(KEY, self.strategy())
        from .editorial_views import EditorialSuggestionThrottle
        self.assertEqual(EditorialSuggestionThrottle.rate, "12/hour")

    def test_raw_stream_elapsed_byte_deadline_closes_response(self):
        response = remote_response(self.suggestion(self.body()))
        with patch("content_factory.catalog_brief_commands.time.monotonic", side_effect=range(100000)):
            with self.assertRaises(ValueError): _read_response(response)
        self.assertTrue(response.raw.closed)

    def test_response_digest_corruption_is_held_without_repair_or_external_call(self):
        body = self.body()
        with patch("requests.post", return_value=remote_response(self.suggestion(body))):
            self.assertEqual(self.post(body).status_code, 200)
        config = OrganizationContentConfig.objects.get(organization=self.org)
        receipt = next(iter(config.pillar_strategy[KEY].values()))
        receipt["response"]["rationale"] = "An unverified replacement."
        config.save(); before = self.strategy()
        with patch("requests.post") as outbound:
            result = self.post(body)
        self.assertEqual(result.status_code, 409); outbound.assert_not_called()
        self.assertEqual(self.strategy(), before)

    def test_freeze_during_model_read_exposes_no_result_and_keeps_pending_claim(self):
        body = self.body()
        def remote(*args, **kwargs):
            config = OrganizationContentConfig.objects.get(organization=self.org)
            config.pillar_strategy = {**config.pillar_strategy, startup_team.KEY: {"schema_version": 1, "frozen": True}}
            config.save(); return remote_response(self.suggestion(body))
        with patch("requests.post", side_effect=remote) as outbound:
            first = self.post(body); replay = self.post(body)
        self.assertIn(first.status_code, (403, 404)); self.assertIn(replay.status_code, (403, 404))
        self.assertEqual(outbound.call_count, 1)
        self.assertEqual(next(iter(self.strategy()[KEY].values()))["state"], "pending")
