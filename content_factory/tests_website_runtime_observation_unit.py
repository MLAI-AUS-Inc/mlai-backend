"""Actual scoped API and HTTP transport; database/network boundaries are denied."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.http import QueryDict
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
import requests
from rest_framework.test import APIRequestFactory, force_authenticate

from . import website_runtime_observation as runtime, website_goal as goals
from . import tests_website_zoo_scope_unit as scope_fixtures
from .tests_website_zoo_scope_unit import COMPANY, CONNECTION, GOAL, SECRET, PRIVATE_PATH

FIXTURE = Path(__file__).parent / "testdata/runtime-observation/cf-processes-v1.json"


def query():
    result = QueryDict(mutable=True)
    result.update(company_id=COMPANY, connection_id=CONNECTION, connection_generation="2", goal_id=GOAL, target="ready")
    return result


def cf_fixture():
    value = json.loads(FIXTURE.read_text())
    now = goals.stamp(value["observed_at"]) + timedelta(seconds=1)
    return value, now


def response(value, *, status=200):
    result = requests.Response()
    result.status_code = status
    result.headers = {"Content-Type": "application/json"}
    result.raw = BytesIO(json.dumps(value).encode())
    return result


@override_settings(INTERNAL_API_KEY="synthetic-internal-observer-key", CONTENT_FACTORY_API_KEY="synthetic-cf-observer-key",
    CONTENT_FACTORY_URL="https://cf.example.test")
class RuntimeObservationTests(SimpleTestCase):
    def setUp(self):
        self.scope = scope_fixtures.ZooScopeTests(); self.scope.setUp()
        self.factory = APIRequestFactory()
        self.remote, self.now = cf_fixture()

    def request(self, selection=None, *, key="synthetic-internal-observer-key"):
        request = self.factory.get(reverse("articles_runtime_observation"), data=selection or query(),
            HTTP_X_API_KEY=key)
        return runtime.WebsiteRuntimeObservationView.as_view()(request)

    def database_boundary(self):
        db = Mock(vendor="postgresql", in_atomic_block=False, connection=None)
        db.cursor.return_value = nullcontext(Mock())
        return patch.object(runtime, "database", db)

    def test_missing_wrong_and_owner_auth_cannot_read_scope_or_call_cf(self):
        with patch.object(runtime, "read_original") as read, patch.object(runtime, "fetch_cf_processes") as fetch:
            for key in ("", "wrong"):
                self.assertEqual(self.request(key=key).status_code, 403)
            with override_settings(INTERNAL_API_KEY="", DEBUG=True):
                self.assertEqual(self.request().status_code, 403)
            owner = self.factory.get(reverse("articles_runtime_observation"), data=query())
            force_authenticate(owner, user=SimpleNamespace(is_authenticated=True, is_superuser=True))
            self.assertEqual(runtime.WebsiteRuntimeObservationView.as_view()(owner).status_code, 403)
        read.assert_not_called(); fetch.assert_not_called()

    def test_malformed_duplicate_or_caller_observation_fields_reject_before_queries(self):
        invalid = []
        for key, value in (("company_id", "bad"), ("connection_generation", "true"), ("target", "latest"),
                           ("goal_id", ""), ("flags", "true")):
            selected = query(); selected[key] = value; invalid.append(selected)
        repeated = query(); repeated.appendlist("company_id", COMPANY); invalid.append(repeated)
        with patch.object(runtime, "read_original") as read:
            for selected in invalid:
                with self.subTest(query=selected), self.assertRaises(runtime.ObservationUnavailable):
                    runtime.select_original(selected)
        read.assert_not_called()

    def test_actual_scope_and_route_are_readonly_and_cf_stays_unbound(self):
        before = deepcopy((self.scope.goal, self.scope.run.run_request))
        with self.scope.boundaries(), self.database_boundary() as db, patch.object(runtime.transaction, "atomic", return_value=nullcontext()), \
                patch.object(runtime.timezone, "now", return_value=self.now), \
                patch.object(requests.Session, "send", return_value=response(self.remote)) as send:
            value = self.request()
        self.assertEqual(value.status_code, 200, value.data)
        self.assertEqual(value["Cache-Control"], "no-store")
        self.assertEqual(value.data["scope_evidence"]["original_goal_id"], GOAL)
        self.assertEqual(value.data["scope_evidence"]["organization_id"], "7")
        self.assertEqual(value.data["content_factory_processes"]["goal_binding"], "unbound")
        self.assertEqual(value.data["content_factory_processes"]["state"], "observed")
        self.assertIsNone(value.data["effective_rollout"])
        self.assertIsNone(value.data["original_execution_eligibility"])
        self.assertEqual(value.data["coverage"], "partial")
        self.assertEqual(before, (self.scope.goal, self.scope.run.run_request))
        text = json.dumps(value.data)
        for private in (SECRET, PRIVATE_PATH, "synthetic-internal-observer-key", "synthetic-cf-observer-key"):
            self.assertNotIn(private, text)
        prepared = send.call_args.args[0]
        self.assertEqual(prepared.url, "https://cf.example.test/api/internal/articles/process-observations")
        self.assertEqual(prepared.headers["X-API-Key"], "synthetic-cf-observer-key")
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        self.assertTrue(send.call_args.kwargs["verify"])
        self.assertEqual(send.call_args.kwargs["proxies"], {})
        self.assertEqual(send.call_args.kwargs["timeout"], (3, 8))
        self.assertEqual(db.cursor.call_count, 2)

    def test_readonly_fence_precedes_saved_scope_queries(self):
        self.scope.order.clear()
        cursor = Mock()
        cursor.execute.side_effect = lambda sql: self.scope.order.append(sql)
        db = Mock(vendor="postgresql", in_atomic_block=False, connection=None)
        db.cursor.return_value = nullcontext(cursor)
        with self.scope.boundaries(), patch.object(runtime, "database", db), patch.object(runtime.transaction, "atomic", return_value=nullcontext()):
            row, target = runtime.select_original(query()); runtime.read_original(row, target)
        self.assertEqual(self.scope.order[0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for db in (Mock(vendor="sqlite", in_atomic_block=False), Mock(vendor="postgresql", in_atomic_block=True)):
            with patch.object(runtime, "database", db), patch.object(runtime.scope, "resolve_case") as resolve:
                with self.assertRaises(runtime.ObservationUnavailable): runtime.read_original(row, target)
            resolve.assert_not_called()

    def test_foreign_original_goal_or_target_never_calls_cf(self):
        self.scope.operation.payload["company_id"] = "another-company"
        with self.scope.boundaries(), self.database_boundary(), patch.object(runtime.transaction, "atomic", return_value=nullcontext()), \
                patch.object(runtime, "fetch_cf_processes") as fetch:
            result = self.request()
        self.assertEqual(result.status_code, 409); fetch.assert_not_called()

    def test_ready_cannot_be_relabelled_as_first_article(self):
        selected = query(); selected["target"] = "first_article_live"
        with self.scope.boundaries(), self.database_boundary(), patch.object(runtime.transaction, "atomic", return_value=nullcontext()), \
                patch.object(runtime, "fetch_cf_processes") as fetch:
            result = self.request(selected)
        self.assertEqual(result.status_code, 409); fetch.assert_not_called()

    def test_changed_original_scope_during_remote_read_is_not_relabelled(self):
        def remote():
            self.scope.connection.generation += 1
            return {"state": "unknown"}
        with self.scope.boundaries(), self.database_boundary(), patch.object(runtime.transaction, "atomic", return_value=nullcontext()), \
                patch.object(runtime, "fetch_cf_processes", side_effect=remote):
            result = self.request()
        self.assertEqual(result.status_code, 409)
        self.assertNotIn("backend_process", result.data)

    def test_changed_backend_configuration_during_remote_read_is_rejected(self):
        from django.conf import settings
        def remote():
            settings.ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED = True
            return {"state": "unknown"}
        with override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=False), self.scope.boundaries(), \
                self.database_boundary(), patch.object(runtime.transaction, "atomic", return_value=nullcontext()), \
                patch.object(runtime, "fetch_cf_processes", side_effect=remote):
            result = self.request()
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["code"], "original_observation_changed")

    def test_backend_revision_flags_and_process_change_remain_explicit(self):
        with patch.dict(os.environ, {"APP_RELEASE": "a" * 40}), patch.object(runtime.os, "getpid", return_value=100):
            first = runtime.backend_process("7")
            with override_settings(ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=not first["flags"]["ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED"]):
                changed = runtime.backend_process("7")
        self.assertEqual(first["revision"], "a" * 40)
        self.assertEqual(first["instance_id"], changed["instance_id"])
        self.assertNotEqual(first["configuration_digest"], changed["configuration_digest"])
        with patch.dict(os.environ, {"APP_RELEASE": "main"}), patch.object(runtime.os, "getpid", return_value=101):
            child = runtime.backend_process("7")
        self.assertNotEqual(first["instance_id"], child["instance_id"])
        self.assertIsNone(child["revision"])
        self.assertIsNone(child["original_execution_eligibility"])

    def test_actual_cf_producer_fixture_is_closed_and_never_eligible(self):
        value, now = cf_fixture()
        value["private"] = SECRET
        value["observations"][0]["private"] = SECRET
        projected = runtime.project_cf_processes(value, now=now)
        self.assertEqual(len(projected["observations"]), 2)
        self.assertEqual(projected["goal_binding"], "unbound")
        self.assertNotIn(SECRET, json.dumps(projected))
        self.assertIsNone(projected["original_execution_eligibility"])

    def test_cf_stale_future_malformed_mixed_or_promoted_config_stays_unknown(self):
        for field, bad in (("role", "scheduler"), ("configuration_digest", "a" * 64),
                           ("run_eligibility", True), ("revision", SECRET), ("sequence", True),
                           ("schema_version", True), ("schema_version", 2), ("event", {}), ("configuration", [])):
            value, now = cf_fixture(); value["observations"][0][field] = bad
            with self.subTest(field=field), self.assertRaises(runtime.ObservationUnavailable):
                runtime.project_cf_processes(value, now=now)
        value, now = cf_fixture()
        for when in (now - timedelta(seconds=10), now + timedelta(seconds=100)):
            with self.assertRaises(runtime.ObservationUnavailable): runtime.project_cf_processes(value, now=when)
        duplicate, now = cf_fixture(); duplicate["observations"].append(duplicate["observations"][0])
        with self.assertRaises(runtime.ObservationUnavailable): runtime.project_cf_processes(duplicate, now=now)

    def test_missing_key_insecure_url_and_redirect_cannot_supply_authenticated_diagnostics(self):
        for values in ({"CONTENT_FACTORY_API_KEY": ""}, {"CONTENT_FACTORY_URL": "http://cf.example.test"},
                       {"CONTENT_FACTORY_URL": "https://user:password@cf.example.test"}):
            with override_settings(**values), patch.object(requests.Session, "send") as send:
                result = runtime.fetch_cf_processes()
            send.assert_not_called(); self.assertEqual(result["state"], "unknown")
        with patch.object(requests.Session, "send", return_value=response({}, status=302)) as send:
            result = runtime.fetch_cf_processes()
        self.assertEqual(send.call_count, 1); self.assertEqual(result["state"], "unknown")

    def test_actual_streaming_bounds_and_remote_failures_do_not_echo_payloads(self):
        for remote in (response({"private": SECRET}), response({"private": "x" * (runtime.MAX_CF_BYTES + 1)})):
            with patch.object(requests.Session, "send", return_value=remote):
                result = runtime.fetch_cf_processes()
            self.assertEqual(result["state"], "unknown"); self.assertNotIn(SECRET, json.dumps(result))
        with patch.object(requests.Session, "send", side_effect=RuntimeError(SECRET)):
            result = runtime.fetch_cf_processes()
        self.assertEqual(result["state"], "unknown"); self.assertNotIn(SECRET, json.dumps(result))
