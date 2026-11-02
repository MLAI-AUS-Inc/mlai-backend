"""Real scope controllers retain original inputs while issuing current tickets."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .self_heal_testing import ALL_FLAGS_ON, make_p2_org
from .run_ownership import (WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, OWNER_EXECUTION_KEY,
    bind_admitted_run, current_execution_ticket, reserve_admitted_run, user_principal)
from .run_execution_control import (BUDGET_KEY, ExecutionControlDenied, current_execution_request)
from .source_repin import repin_source
from .website_contract import WebsiteAuthorityError
from .website_connections import contract_for, scoped_run_contract
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_operations import advance_workflow_attempt, validate_operation
from .website_current import current_run_binding
from .tests_run_event_authority import batch

FLAGS = {**ALL_FLAGS_ON, "ARTICLES_RUN_OWNERSHIP_ENABLED": True,
    "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": True, "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": True,
    "ARTICLES_RUN_CONTROL_MODEL_USD": "5.00", "ARTICLES_RUN_CONTROL_MODEL_TOKENS": 50000,
    "ARTICLES_RUN_SDK_CONTROL_ENABLED": True,
    "CONTENT_FACTORY_URL": "https://worker.fixture.test", "CONTENT_FACTORY_API_KEY": "synthetic-key"}


@override_settings(**FLAGS)
class SourceOperationControlTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.user = self.fixture.organization, self.fixture.owner
        self.website = WebsiteConnection.objects.create(organization=self.org, github_repo="example/site",
            repository_id=123, installation_id="45", branch="main", site_url=f"https://{self.org.domain}",
            state="connected", verified_sha="a" * 40)
        self.fixture.config.website_connection = self.website
        self.fixture.config.github_repo = self.website.github_repo
        self.fixture.config.save(update_fields=["website_connection", "github_repo"])
        self.operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1,
            action="workflow", state="running", idempotency_key="synthetic-source-control-operation",
            payload={"run_id": "source-control-worker", "attempt": 1, "deletion_epoch": 0})
        self.sequence = 0
        self.make_run()

    def make_run(self, workflow="article_system_setup", *, suffix=""):
        if suffix:
            self.operation = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1,
                action="workflow", state="running", idempotency_key="synthetic-source-control-operation" + suffix,
                payload={"run_id": "source-control-worker" + suffix, "attempt": 1, "deletion_epoch": 0})
        payload = {"domain": self.org.domain, "github_repo": self.website.github_repo,
            "client_request_id": "source-control-original" + suffix, "expected_source_sha": "a" * 40,
            "github_installation_id": "45", "configuration_revision": 1,
            "approved_body_sha256": "b" * 64, "article_text": "Keep this approved body",
            **contract_for(self.website), "operation_id": str(self.operation.pk), "operation_attempt": 1, "deletion_epoch": 0}
        run = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"),
            workflow=workflow, payload=payload)
        self.run = bind_admitted_run(run, "source-control-worker" + suffix)
        self.original = ContentFactoryRun.objects.values().get(pk=self.run.pk)

    def response(self, value):
        return Mock(status_code=200, iter_content=Mock(return_value=[json.dumps(value).encode()]))

    def remote(self, url, **kwargs):
        self.assertFalse(connection.in_atomic_block, url)
        self.run.refresh_from_db()
        if url.endswith("/execution-budget"):
            return self.response({"schema_version": 1, "run_id": self.run.run_id,
                WIRE_REQUEST_KEY: self.run.run_request[WIRE_REQUEST_KEY], WIRE_EXECUTION_KEY: current_execution_ticket(self.run),
                BUDGET_KEY: self.run.run_request[BUDGET_KEY], "event_cursor": self.sequence + 1,
                "used_model_usd_upper_bound": "0.25", "used_model_tokens": 100, "uncertain_calls": 0})
        self.assertTrue(url.endswith("/events"), url)
        self.sequence += 1
        envelope = batch(self.run, [{"seq": self.sequence, "state_version": self.sequence, "status": "running"}])
        return self.response({"schema_version": 1, "run_id": self.run.run_id, "batches": [envelope],
                              "next_cursor": self.sequence, "has_more": False})

    def apply(self, before="a", after="c", ordinal=1, *, echo=None):
        self.run.refresh_from_db()
        request = current_execution_request(self.run)
        payload = {**request, "from_sha": before * 40, "to_sha": after * 40,
                   "reason": "source_changed_mid_run", "repin_key": f"{self.run.run_id}:{ordinal}"}
        if echo is not None:
            payload[WIRE_EXECUTION_KEY] = echo
        return repin_source(self.run.run_id, payload)

    def provider_head(self, website, expected):
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(website.pk, self.website.pk)
        return expected

    def guards(self):
        return (patch("requests.get", side_effect=self.remote),
            patch("content_factory.website_connections.verify_repository_native_target"),
            patch("content_factory.website_connections.verify_repository_head", side_effect=self.provider_head))

    def test_third_priced_sdk_repin_keeps_original_request_charge_clocks_and_all_tickets(self):
        get, native, head = self.guards()
        ledgers = list(Ledger.objects.values())
        with get, native, head:
            for index, (before, after) in enumerate((("a", "c"), ("c", "d"), ("d", "e")), 1):
                result = self.apply(before, after, index)
                self.assertEqual(result["repins"], index)
                self.assertEqual(result["max_repins"], 1024)
                self.assertEqual(result[WIRE_EXECUTION_KEY]["generation"], 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request, self.original["run_request"])
        self.assertEqual(self.run.updated_at, self.original["updated_at"])
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 4)
        self.assertEqual(current_execution_request(self.run)["expected_source_sha"], "e" * 40)
        self.assertEqual(self.run.result["re_pin_v1"]["repins"], 3)
        self.assertEqual(list(Ledger.objects.values()), ledgers)

    def test_lost_ack_after_cf_pair_commit_replays_with_old_or_new_ticket_once(self):
        initial = current_execution_ticket(self.run)
        get, native, head = self.guards()
        with get as calls, native, head:
            first = self.apply()
            after_first = calls.call_count
            replay_old = self.apply(echo=initial)
            replay_new = self.apply(echo=first[WIRE_EXECUTION_KEY])
            self.assertEqual(calls.call_count, after_first)
        self.assertEqual(first[WIRE_EXECUTION_KEY], replay_old[WIRE_EXECUTION_KEY])
        self.assertEqual(first[WIRE_EXECUTION_KEY], replay_new[WIRE_EXECUTION_KEY])
        self.assertEqual((replay_old["repins"], replay_new["repins"]), (1, 1))

    def test_current_requester_loss_refuses_before_any_provider_or_grant(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        with patch("requests.get") as get, self.assertRaises(ExecutionControlDenied):
            self.apply()
        get.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    def test_revoke_during_independent_source_proof_cannot_issue_a_ticket(self):
        def revoke(website, expected):
            self.provider_head(website, expected)
            WebsiteConnection.objects.filter(pk=website.pk).update(state="disconnected")
            return expected
        with patch("requests.get", side_effect=self.remote), \
                patch("content_factory.website_connections.verify_repository_native_target"), \
                patch("content_factory.website_connections.verify_repository_head", side_effect=revoke), \
                self.assertRaises(WebsiteAuthorityError):
            self.apply()
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 1)
        self.assertNotIn("re_pin_v1", self.run.result)
        self.assertEqual(self.run.run_request, self.original["run_request"])

    def test_authorized_operation_resume_moves_ticket_scope_once_not_original_request(self):
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="failed")
        self.operation.state = "failed"
        self.operation.save(update_fields=["state"])
        self.run.refresh_from_db()
        with patch("requests.get") as network:
            first = advance_workflow_attempt(self.run, preserve_run_clock=True)
            second = advance_workflow_attempt(self.run, preserve_run_clock=True)
        network.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(first["operation_attempt"], 2)
        self.assertEqual(first[WIRE_EXECUTION_KEY]["generation"], 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request, self.original["run_request"])
        self.assertEqual(self.run.updated_at, self.original["updated_at"])
        current = scoped_run_contract(self.run)
        self.assertEqual(current["operation_attempt"], 2)
        validate_operation(self.website, {**current, "run_id": self.run.run_id})

    def test_original_charge_release_during_source_proof_cannot_issue_a_ticket(self):
        def release(website, expected):
            self.provider_head(website, expected)
            current = ContentFactoryRun.objects.get(pk=self.run.pk)
            ContentFactoryRun.objects.filter(pk=current.pk).update(result={**current.result,
                "points_v1": {"state": "released"}})
            return expected
        with patch("requests.get", side_effect=self.remote), \
                patch("content_factory.website_connections.verify_repository_native_target"), \
                patch("content_factory.website_connections.verify_repository_head", side_effect=release), \
                self.assertRaises(WebsiteAuthorityError):
            self.apply()
        self.run.refresh_from_db()
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 1)
        self.assertNotIn("re_pin_v1", self.run.result)
        self.assertEqual(self.run.result["points_v1"], {"state": "released"})

    def test_non_sdk_article_retains_the_two_repin_limit(self):
        self.make_run("article_generation", suffix="-article")
        get, native, head = self.guards()
        with get, native, head:
            self.assertEqual(self.apply()["max_repins"], 2)
            self.apply("c", "d", 2)
            with self.assertRaises(WebsiteAuthorityError) as error:
                self.apply("d", "e", 3)
        self.assertEqual(error.exception.authority_code, "source_repin_limit_reached")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["re_pin_v1"]["repins"], 2)

    def test_owned_current_binding_rebinds_ticket_and_context_not_original_inputs(self):
        WebsiteConnection.objects.filter(pk=self.website.pk).update(generation=2, configuration_version=2)
        first = current_run_binding(run_id=self.run.run_id, domain=self.run.domain, repository_id=123)
        second = current_run_binding(run_id=self.run.run_id, domain=self.run.domain, repository_id=123)
        self.assertEqual(first, second)
        self.assertEqual(first[WIRE_EXECUTION_KEY]["generation"], 0)
        self.assertEqual(first[WIRE_EXECUTION_KEY]["scope"]["connection_generation"], 2)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request, self.original["run_request"])
        self.assertEqual(self.run.updated_at, self.original["updated_at"])
        self.assertEqual(len(self.run.result[OWNER_EXECUTION_KEY]["tickets"]), 2)
        current = current_execution_request(self.run)
        self.assertEqual(current["configuration_revision"], 2)
        self.assertEqual(current["connection_generation"], 2)
        self.assertEqual(current["github_installation_id"], "45")
        validate_operation(WebsiteConnection.objects.get(pk=self.website.pk), {**current, "run_id": self.run.run_id})
        if os.environ.get("FOUNDER_R3_FIXTURE_DIR"):
            target = Path(os.environ["FOUNDER_R3_FIXTURE_DIR"]) / "R3_BACKEND_CURRENT_BINDING.fixture.json"
            target.write_text(json.dumps({"schema_version": 1, "original_request": self.original["run_request"],
                "response": first, "effective_current_request": current}, indent=2) + "\n")

    def test_owned_current_binding_lost_authority_does_not_silently_change_delivery(self):
        WebsiteConnection.objects.filter(pk=self.website.pk).update(state="disconnected")
        with self.assertRaises(WebsiteAuthorityError):
            current_run_binding(run_id=self.run.run_id, domain=self.run.domain, repository_id=123)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    def test_current_setup_queries_use_retained_ticket_scope_and_source(self):
        from .website_connections import open_setup_pr_run, settled_setup_run
        get, native, head = self.guards()
        with get, native, head:
            self.apply()
        WebsiteConnection.objects.filter(pk=self.website.pk).update(generation=2, configuration_version=2)
        current_run_binding(run_id=self.run.run_id, domain=self.run.domain, repository_id=123)
        self.run.refresh_from_db()
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(status="completed", approval_state="approved",
            result={**self.run.result, "status": "pr_created", "article_system_setup": {"status": "pr_created"},
                    "pr_url": "https://github.com/example/site/pull/1"})
        self.website.refresh_from_db()
        found = open_setup_pr_run(self.website, source_sha="c" * 40)
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.run.pk)
        self.assertIsNone(open_setup_pr_run(self.website, source_sha="a" * 40))
        self.run.refresh_from_db()
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result,
            "adopted": True, "pr_url": "", "article_system_setup": {},
            "adoption": {"kit_paths": [".github/workflows/articles.yml"]}})
        found = settled_setup_run(self.website, source_sha="c" * 40)
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.run.pk)
        self.assertIsNone(settled_setup_run(self.website, source_sha="a" * 40))

