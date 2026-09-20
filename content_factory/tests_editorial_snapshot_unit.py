"""Actual snapshot/binding control flow with in-memory seams, not SQL tests."""
import ast
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
import traceback
from unittest.mock import Mock

from .tests_editorial_dispatch_unit import selected_brief  # Isolated dummy settings, no app/DB setup.
from .editorial_run_state import BRIEF_KEYS, EditorialRunConflict, merge_editorial_run_snapshot
from .run_state import ACTIVE_RUN_STATUSES, ARTICLE_WORKFLOWS, active_retry_signal, clear_obsolete_active_run_blockers, stale_execution_event, merge_reliability_fields
from django.db import OperationalError
from rest_framework import status
from rest_framework.response import Response
from workflow_runs.serializers import ContentFactoryRunSyncSerializer


def admitted_snapshot():
    import hashlib
    import json
    from .tests_editorial_catalog_unit import approved_catalog
    from .editorial_catalog import catalog_payload
    current = catalog_payload(approved_catalog())
    result = snapshot()
    selected = {"brief": selected_brief(), "audience": current["audience_options"][0], "offer": current["cta_options"][0]}
    result["run_request"]["editorial_admission"] = {
        "schema_version": "2026-09-11.1", "checked_at": "2026-09-11T00:00:00+00:00",
        "domain": "example.test", "github_repo": "fixture/site", **selected,
        "selection_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
    }
    return result


class EditorialAdmissionSnapshotTests(unittest.TestCase):
    def test_sparse_snapshot_preserves_exact_admission_without_reapproval(self):
        original = admitted_snapshot()
        merged = merge_editorial_run_snapshot(original, {"workflow": "article_generation", "run_request": {}})
        self.assertEqual(merged["run_request"]["editorial_admission"], original["run_request"]["editorial_admission"])

    def test_admission_cannot_be_cleared_changed_or_detached_from_its_brief(self):
        for change in (None, {}, "invalid", {"checked_at": "2026-09-11T01:00:00+00:00"},
                       {"domain": "other.example"}, {"selection_sha256": "0" * 64}):
            with self.subTest(change=change):
                original = admitted_snapshot()
                incoming = deepcopy(original)
                value = incoming["run_request"]["editorial_admission"]
                incoming["run_request"]["editorial_admission"] = {**value, **change} if isinstance(change, dict) and change else change
                with self.assertRaises(EditorialRunConflict):
                    merge_editorial_run_snapshot(original, incoming)

    def test_first_seen_observation_requires_a_matching_brief_and_domain(self):
        for where in ("brief", "domain", "repository"):
            with self.subTest(where=where):
                incoming = admitted_snapshot()
                if where == "brief":
                    incoming["run_request"].pop("editorial_brief")
                elif where == "domain":
                    incoming["domain"] = "other.example"
                else:
                    incoming["run_request"]["github_repo"] = "fixture/other"
                with self.assertRaises(EditorialRunConflict):
                    merge_editorial_run_snapshot(None, incoming)

    def test_valid_first_observation_and_later_retirement_failure_retain_history(self):
        original = admitted_snapshot()
        self.assertEqual(merge_editorial_run_snapshot(None, original), original)
        merged = merge_editorial_run_snapshot(original, {"workflow": "article_generation", "status": "blocked", "error": "Offer retired"})
        self.assertEqual(merged["error"], "Offer retired")
        self.assertEqual(merged["run_request"]["editorial_admission"], original["run_request"]["editorial_admission"])


def snapshot(brief=True):
    return {"workflow": "article_generation", "domain": "example.test", "status": "running",
            "run_request": {"client_request_id": "dispatch-key", **({"editorial_brief": selected_brief()} if brief else {})}}


class EditorialSnapshotContractTests(unittest.TestCase):
    def test_sparse_snapshot_preserves_original_brief_and_key_without_mutating_inputs(self):
        existing, incoming = snapshot(), {"workflow": "confirmed_topic", "status": "running", "run_request": {"worker_only": 1}}
        originals = deepcopy((existing, incoming))
        merged = merge_editorial_run_snapshot(existing, incoming)
        self.assertEqual(merged["run_request"], {"editorial_brief": selected_brief(), "client_request_id": "dispatch-key", "worker_only": 1})
        self.assertEqual(merged["domain"], "example.test")
        self.assertEqual((existing, incoming), originals)

    def test_known_brief_cannot_change_reader_task_offer_country_or_no_offer(self):
        for change in ({"reader_task": "A different task"}, {"offer_id": "other"}, {"country": "NZ"},
                       {"acceptance_criteria": ["Different test"]}, {"distinct_contribution": "A different contribution"},
                       {"audience_version": 2}, {"conversion_intent": "none", "offer_id": None, "offer_version": None, "no_offer_reason": "Changed"}):
            with self.subTest(change=change), self.assertRaises(EditorialRunConflict):
                incoming = snapshot()
                incoming["run_request"]["editorial_brief"].update(change)
                merge_editorial_run_snapshot(snapshot(), incoming)

    def test_known_brief_cannot_be_cleared_or_replaced_by_ambiguous_aliases(self):
        for value in (None, {}, [], "", False):
            with self.subTest(value=value), self.assertRaises(EditorialRunConflict):
                merge_editorial_run_snapshot(snapshot(), {**snapshot(), "run_request": {"editorialBrief": value}})
        with self.assertRaises(EditorialRunConflict):
            merge_editorial_run_snapshot(snapshot(), {**snapshot(), "run_request": {"editorialBrief": None, "editorial_brief": selected_brief()}})

    def test_identical_camel_case_is_canonicalized_and_no_offer_remains_explicit(self):
        old = snapshot()
        brief = old["run_request"]["editorial_brief"]
        brief.update(conversion_intent="none", offer_id=None, offer_version=None, no_offer_reason="Learning")
        incoming = {**snapshot(), "run_request": {"editorialBrief": deepcopy(brief)}}
        merged = merge_editorial_run_snapshot(old, incoming)
        self.assertEqual(merged["run_request"]["editorial_brief"], brief)
        self.assertNotIn("editorialBrief", merged["run_request"])

    def test_tenant_workflow_and_dispatch_key_cannot_be_retargeted(self):
        for change in ({"domain": "other.test"}, {"workflow": "repo_scan"},
                       {"run_request": {"domain": "other.test"}}, {"run_request": {"client_request_id": "other-key"}}):
            with self.subTest(change=change), self.assertRaises(EditorialRunConflict):
                merge_editorial_run_snapshot(snapshot(), {**snapshot(), **change})

    def test_first_seen_brief_is_structural_observation_not_generated_approval(self):
        incoming = snapshot()
        merged = merge_editorial_run_snapshot(None, incoming)
        self.assertEqual(merged, incoming)
        self.assertNotIn("approved_by", merged["run_request"])
        self.assertNotIn("editorial_brief", merge_editorial_run_snapshot(snapshot(False), {"workflow": "article_generation", "run_request": {}})["run_request"])

    def test_history_can_record_a_failure_without_revalidating_a_retired_offer(self):
        result = merge_editorial_run_snapshot(snapshot(), {"workflow": "article_generation", "status": "blocked", "error": "Offer retired", "run_request": {}})
        self.assertEqual(result["error"], "Offer retired")
        self.assertEqual(result["run_request"]["editorial_brief"], selected_brief())

    def test_malformed_stored_decision_is_not_overwritten_as_repair(self):
        old = snapshot()
        old["run_request"]["editorial_brief"] = {"audience_id": "BUILDER"}
        with self.assertRaisesRegex(EditorialRunConflict, "Stored"):
            merge_editorial_run_snapshot(old, snapshot())

    def test_catalog_free_null_and_non_article_requests_keep_legacy_shape(self):
        for incoming in ({"workflow": "repo_scan", "run_request": {}},
                         {"workflow": "article_generation", "run_request": {"editorial_brief": None}},
                         {"workflow": "repo_scan", "run_request": None}):
            with self.subTest(incoming=incoming):
                self.assertEqual(merge_editorial_run_snapshot(None, incoming), incoming)

    def test_validation_error_traceback_does_not_include_private_brief_input(self):
        incoming = snapshot()
        incoming["run_request"]["editorial_brief"]["reader_task"] = {"secret": "private-fixture-do-not-log"}
        try:
            merge_editorial_run_snapshot(None, incoming)
        except EditorialRunConflict:
            self.assertNotIn("private-fixture-do-not-log", traceback.format_exc())
        else:
            self.fail("Malformed brief was accepted")

    def test_stored_embedded_domain_cannot_disagree_with_the_run(self):
        existing = snapshot()
        existing["run_request"]["domain"] = "other.test"
        with self.assertRaises(EditorialRunConflict):
            merge_editorial_run_snapshot(existing, {"workflow": "article_generation", "run_request": {}})

    def test_every_article_workflow_alias_keeps_the_same_decision(self):
        for workflow in ARTICLE_WORKFLOWS:
            with self.subTest(workflow=workflow):
                self.assertEqual(merge_editorial_run_snapshot(snapshot(), {"workflow": workflow})["run_request"]["editorial_brief"], selected_brief())


class EditorialSnapshotPersistenceSeamTests(unittest.TestCase):
    def test_actual_put_preserves_admission_and_rejects_clear_before_step_writes(self):
        original = admitted_snapshot()["run_request"]
        self.existing.run_request = deepcopy(original)
        response = self.put({"workflow": "article_generation", "status": "running"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.existing.run_request["editorial_admission"], original["editorial_admission"])
        writes = self.ns["ContentFactoryRunStep"].objects.update_or_create.call_count
        response = self.put({"workflow": "article_generation", "status": "running", "run_request": {"editorial_admission": None},
                             "step_states": {"research": {"status": "running"}}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.ns["ContentFactoryRunStep"].objects.update_or_create.call_count, writes)
        self.assertEqual(self.existing.run_request["editorial_admission"], original["editorial_admission"])

    def test_actual_binding_preserves_admission_in_partial_callback_row(self):
        original = admitted_snapshot()
        token = self.add_row("dispatch-key", original)
        real = self.add_row("remote-run", {**snapshot(False), "run_request": {"worker_only": 1}, "status": "running"})
        result = self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run")
        self.assertIs(result, real)
        self.assertEqual(real.run_request["editorial_admission"], original["run_request"]["editorial_admission"])
        token.delete.assert_called_once()

    def setUp(self):
        self.in_transaction = False
        self.calls = []
        self.rows = {}
        self.model = SimpleNamespace(objects=Mock())
        self.steps = SimpleNamespace(objects=Mock())
        self.attempts = SimpleNamespace(objects=Mock())
        self.model.objects.select_for_update.return_value.prefetch_related.return_value.filter.side_effect = self.locked_query
        self.model.objects.select_for_update.return_value.prefetch_related.return_value.get_or_create.side_effect = self.get_or_create
        self.model.objects.select_for_update.return_value.filter.side_effect = self.locked_query
        self.model.objects.filter.side_effect = self.query
        self.model.objects.update_or_create.side_effect = self.save_snapshot
        self.ns = {
            "__name__": "content_factory.service_views", "__package__": "content_factory",
            "transaction": SimpleNamespace(atomic=self.atomic), "ContentFactoryRun": self.model,
            "ContentFactoryRunStep": self.steps, "ContentFactoryRunStepAttempt": self.attempts,
            "ContentFactoryRunStatus": SimpleNamespace(QUEUED="queued", BLOCKED="blocked", FAILED="failed", DENIED="denied", CANCELLED="cancelled", COMPLETED="completed"),
            "ContentFactoryApprovalState": SimpleNamespace(NOT_REQUIRED="not_required"),
            "DURABLE_ACTIVE_RUN_STATUSES": ACTIVE_RUN_STATUSES, "sanitize_json_for_postgres": deepcopy,
            "EditorialRunConflict": EditorialRunConflict, "merge_editorial_run_snapshot": merge_editorial_run_snapshot,
            "BRIEF_KEYS": BRIEF_KEYS, "deepcopy": deepcopy,
            "clear_obsolete_active_run_blockers": clear_obsolete_active_run_blockers,
            "logger": Mock(), "_rebind_billing_job": Mock(), "status": status, "Response": Response,
            "ContentFactoryRunSyncSerializer": ContentFactoryRunSyncSerializer,
            "ARTICLE_WORKFLOWS": ARTICLE_WORKFLOWS, "active_retry_signal": active_retry_signal,
            "stale_execution_event": stale_execution_event, "merge_reliability_fields": merge_reliability_fields,
            "OperationalError": OperationalError, "connection": SimpleNamespace(vendor="sqlite"),
            "_is_retryable_sqlite_lock": lambda exc: "locked" in str(exc), "time": SimpleNamespace(sleep=Mock()),
            "_is_terminal_run_status": lambda value: value in {"completed", "cancelled", "failed", "denied", "blocked"},
            "_article_system_setup_snapshot_is_current_retry": lambda **kwargs: False,
            "_serialize_content_factory_run": lambda run: {"run_id": run.run_id, "run_request": deepcopy(run.run_request)},
        }
        import sys
        from types import ModuleType
        from unittest.mock import patch
        island = ModuleType("content_factory.island_research")
        island.refund_empty_or_failed_research = Mock()
        seam = patch.dict(sys.modules, {"content_factory.island_research": island})
        seam.start()
        self.addCleanup(seam.stop)
        root = Path(__file__).resolve().parent
        names = {"_sync_content_factory_run_snapshot", "_content_factory_run_snapshot_unchanged", "_merge_django_owned_run_result"}
        tree = ast.parse((root / "service_views.py").read_text())
        nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names) or
                 (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("_DJANGO_OWNED_RUN_RESULT_") for t in n.targets))]
        view = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ContentFactoryRunView")
        put = next(n for n in view.body if isinstance(n, ast.FunctionDef) and n.name == "put")
        put.name = "_snapshot_put_under_test"
        nodes.append(put)
        bind_tree = ast.parse((root / "dispatch_binding.py").read_text())
        nodes.extend(n for n in bind_tree.body if isinstance(n, ast.FunctionDef) and n.name in {"bind_dispatch_token_run", "run_is_dispatch_token_keyed", "_merge_provisional_into_real"})
        nodes.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "snapshot-functions-under-test", "exec"), self.ns)
        self.existing = self.add_row("run-1", snapshot())

    @contextmanager
    def atomic(self):
        self.assertFalse(self.in_transaction)
        self.in_transaction = True
        self.calls.append("transaction")
        try:
            yield
        finally:
            self.in_transaction = False

    def add_row(self, run_id, values):
        base = dict(workflow="article_generation", domain="example.test", github_repo="", slack_user_id="", status="running",
                    current_step="", approval_state="not_required", artifact_root="", step_order=[], acceptance_summary={},
                    verification_summary={}, run_request={}, result={}, error="", resume_available=False)
        base.update(deepcopy(values))
        row = SimpleNamespace(run_id=run_id, pk=len(self.rows) + 1, **base)
        row.steps = SimpleNamespace(all=lambda: [])
        row.save = Mock(side_effect=lambda **kwargs: self.assertTrue(self.in_transaction))
        row.delete = Mock(side_effect=lambda: self.rows.pop(row.run_id))
        self.rows[run_id] = row
        return row

    def query(self, **kwargs):
        return SimpleNamespace(first=lambda: self.rows.get(kwargs["run_id"]))

    def locked_query(self, **kwargs):
        self.assertTrue(self.in_transaction)
        self.calls.append(("lock", kwargs["run_id"]))
        return self.query(**kwargs)

    def save_snapshot(self, *, run_id, defaults):
        self.assertTrue(self.in_transaction)
        self.calls.append("write")
        created = run_id not in self.rows
        row = self.rows.get(run_id) or self.add_row(run_id, {})
        for key, value in deepcopy(defaults).items():
            setattr(row, key, value)
        return row, created

    def get_or_create(self, *, run_id, defaults):
        self.assertTrue(self.in_transaction)
        self.calls.append(("get_or_create", run_id))
        if run_id in self.rows:
            return self.rows[run_id], False
        return self.add_row(run_id, defaults), True

    def sync(self, incoming):
        return self.ns["_sync_content_factory_run_snapshot"](run_id="run-1", data=incoming, step_states={})

    def put(self, payload, run_id="run-1"):
        return self.ns["_snapshot_put_under_test"](None, SimpleNamespace(data=payload), run_id)

    def test_actual_snapshot_preserves_the_brief_and_exact_retry_is_a_noop(self):
        incoming = {"workflow": "article_generation", "domain": "example.test", "status": "running", "run_request": {"worker_only": 1}}
        result, _ = self.sync(incoming)
        self.assertEqual(result.run_request["editorial_brief"], selected_brief())
        self.assertEqual(result.run_request["client_request_id"], "dispatch-key")
        self.model.objects.update_or_create.reset_mock()
        result, _ = self.sync(incoming)
        self.model.objects.update_or_create.assert_not_called()
        self.assertTrue(result._content_factory_sync_unchanged)

    def test_conflicting_snapshot_is_rejected_before_run_or_step_writes(self):
        incoming = snapshot()
        incoming["run_request"]["editorial_brief"]["country"] = "NZ"
        with self.assertRaises(EditorialRunConflict):
            self.sync(incoming)
        self.model.objects.update_or_create.assert_not_called()
        self.steps.objects.update_or_create.assert_not_called()
        self.assertEqual(self.existing.run_request["editorial_brief"], selected_brief())

    def test_active_callback_keeps_brief_and_clears_only_obsolete_blockers(self):
        self.existing.result = {"error": "Old error", "publish_child_run_id": "child"}
        result, _ = self.sync({"workflow": "article_generation", "domain": "example.test", "status": "running", "run_request": {}, "result": {"error": "Old error"}})
        self.assertEqual(result.run_request["editorial_brief"], selected_brief())
        self.assertNotIn("error", result.result)
        self.assertEqual(result.result["publish_child_run_id"], "child")

    def test_binding_keeps_brief_when_callback_record_is_already_nonempty(self):
        token = self.add_row("dispatch-key", snapshot())
        real = self.add_row("remote-run", {**snapshot(False), "run_request": {"worker_only": 1}, "status": "running"})
        result = self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run")
        self.assertIs(result, real)
        self.assertEqual(real.run_request["editorial_brief"], selected_brief())
        self.assertEqual(real.run_request["worker_only"], 1)
        self.assertEqual(real.run_request["client_request_id"], "dispatch-key")
        token.delete.assert_called_once()
        self.assertIn(("lock", "remote-run"), self.calls)

    def test_binding_conflict_preserves_both_rows_without_billing_rebind(self):
        token = self.add_row("dispatch-key", snapshot())
        changed = snapshot()
        changed["run_request"]["editorial_brief"]["country"] = "NZ"
        real = self.add_row("remote-run", changed)
        self.assertIsNone(self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run"))
        token.delete.assert_not_called()
        real.save.assert_not_called()
        self.ns["_rebind_billing_job"].assert_not_called()
        self.assertEqual(real.run_request["editorial_brief"]["country"], "NZ")

    def test_actual_put_returns_explicit_conflict_without_retry_or_writes(self):
        incoming = snapshot()
        incoming["run_request"]["editorial_brief"]["reader_task"] = "Private conflicting task"
        response = self.put(incoming)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"], "editorial_run_conflict")
        self.assertNotIn("Private conflicting task", str(response.data))
        self.model.objects.update_or_create.assert_not_called()
        self.ns["time"].sleep.assert_not_called()

    def test_actual_serializer_defaults_do_not_clear_brief_or_domain(self):
        for request in ({}, {"run_request": {}}, {"run_request": None}):
            with self.subTest(request=request):
                response = self.put({"workflow": "article_generation", "status": "running", **request})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["run_request"]["editorial_brief"], selected_brief())
                self.assertEqual(self.existing.domain, "example.test")

    def test_first_seen_and_legacy_rows_keep_created_response_and_exact_retry(self):
        for run_id, payload in (("new-editorial", snapshot()), ("new-legacy", {"workflow": "repo_scan", "status": "running"})):
            with self.subTest(run_id=run_id):
                response = self.put(payload, run_id)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.data["sync_status"], "created")
                response = self.put(payload, run_id)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["sync_status"], "unchanged")

    def test_invalid_first_seen_brief_is_rejected_before_row_creation(self):
        incoming = snapshot()
        incoming["run_request"]["editorial_brief"] = {"country": "AU"}
        response = self.put(incoming, "new-invalid")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("new-invalid", self.rows)
        self.model.objects.select_for_update.return_value.prefetch_related.return_value.get_or_create.assert_not_called()

    def test_creation_race_rechecks_the_winning_rows_brief(self):
        def another_callback_wins(*, run_id, defaults):
            changed = snapshot()
            changed["run_request"]["editorial_brief"]["country"] = "NZ"
            return self.add_row(run_id, changed), False

        self.model.objects.select_for_update.return_value.prefetch_related.return_value.get_or_create.side_effect = another_callback_wins
        response = self.put(snapshot(), "race-run")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.rows["race-run"].run_request["editorial_brief"]["country"], "NZ")
        self.model.objects.update_or_create.assert_not_called()

    def test_terminal_and_cancelled_callbacks_keep_existing_response_contract(self):
        incoming = {"workflow": "article_generation", "status": "running"}
        self.existing.status = "cancelled"
        self.assertEqual(self.put(incoming).data["error"], "run_cancelled")
        self.existing.status = "completed"
        self.assertEqual(self.put(incoming).data["sync_status"], "ignored_terminal_state")
        self.assertNotIn("transaction", self.calls)

    def test_sqlite_retry_rereads_before_sync_and_preserves_brief(self):
        calls = 0

        def temporarily_locked(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("database is locked")
            return self.locked_query(**kwargs)

        self.model.objects.select_for_update.return_value.prefetch_related.return_value.filter.side_effect = temporarily_locked
        response = self.put({"workflow": "article_generation", "status": "running"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["run_request"]["editorial_brief"], selected_brief())
        self.assertEqual(calls, 2)
        self.ns["time"].sleep.assert_called_once_with(0.25)

    def test_binding_keeps_all_icps_no_offer_and_token_only_context(self):
        for audience in ("SMB", "BUILDER", "FOUNDER_BUILDER", "COMMUNITY", "OUTSIDE"):
            with self.subTest(audience=audience):
                request = snapshot()
                brief = request["run_request"].pop("editorial_brief")
                brief["audience_id"] = audience
                if audience == "OUTSIDE":
                    brief.update(conversion_intent="none", offer_id=None, offer_version=None, no_offer_reason="Intentional learning")
                request["run_request"].update(editorialBrief=brief, billing_context={"source": "fixture"})
                token = self.add_row("dispatch-key", request)
                real = self.add_row("remote-run", {"workflow": "", "domain": "", "run_request": {"worker_only": True}})
                self.assertIs(self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run"), real)
                self.assertEqual(real.run_request["editorial_brief"], brief)
                self.assertNotIn("editorialBrief", real.run_request)
                self.assertEqual(real.run_request["billing_context"], {"source": "fixture"})
                self.assertTrue(real.run_request["worker_only"])
                self.assertEqual(real.domain, "example.test")
                self.assertEqual(real.workflow, "article_generation")
                token.delete.assert_called_once()

    def test_binding_scope_and_key_conflicts_do_not_destroy_the_original(self):
        for change in ({"domain": "other.test"}, {"workflow": "repo_scan"},
                       {"run_request": {"client_request_id": "different"}}, {"run_request": {"editorial_brief": None}}):
            with self.subTest(change=change):
                token = self.add_row("dispatch-key", snapshot())
                real = self.add_row("remote-run", {"run_request": {"worker_only": 1}, **change})
                self.ns["_rebind_billing_job"].reset_mock()
                self.assertIsNone(self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run"))
                token.delete.assert_not_called()
                real.save.assert_not_called()
                self.ns["_rebind_billing_job"].assert_not_called()

    def test_binding_legacy_empty_request_still_copies_without_editorial_approval(self):
        token = self.add_row("dispatch-key", {"workflow": "repo_scan", "run_request": {"client_request_id": "dispatch-key", "topic": "Legacy"}})
        real = self.add_row("remote-run", {"workflow": "repo_scan", "run_request": {}})
        result = self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run")
        self.assertIs(result, real)
        self.assertEqual(real.run_request, token.run_request)
        self.assertNotIn("editorial_brief", real.run_request)

    def test_binding_rename_preserves_request_without_deleting_history(self):
        token = self.add_row("dispatch-key", {**snapshot(), "status": "blocked", "error": "Unconfirmed", "result": {"old": 1}})
        pk = token.pk
        result = self.ns["bind_dispatch_token_run"](client_request_id="dispatch-key", remote_run_id="remote-run")
        self.assertIs(result, token)
        self.assertEqual(token.run_id, "remote-run")
        self.assertEqual(token.pk, pk)
        self.assertEqual(token.run_request, snapshot()["run_request"])
        self.assertEqual(token.status, "queued")
        self.assertEqual(token.result, {})
        token.delete.assert_not_called()
        self.ns["_rebind_billing_job"].assert_called_once_with("dispatch-key", "remote-run")

    def test_actual_put_rejects_clear_and_retarget_before_step_writes(self):
        for change in ({"domain": "other.test"}, {"workflow": "repo_scan"},
                       {"run_request": {"editorialBrief": None}}, {"run_request": {"client_request_id": "different"}}):
            with self.subTest(change=change):
                response = self.put({**snapshot(), **change, "step_states": {"research": {"status": "running"}}})
                self.assertEqual(response.status_code, 409)
                self.steps.objects.update_or_create.assert_not_called()
                self.model.objects.update_or_create.assert_not_called()

    def test_actual_put_records_failure_without_changing_the_original_offer(self):
        response = self.put({"workflow": "article_generation", "status": "blocked", "error": "Offer retired"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.existing.error, "Offer retired")
        self.assertEqual(self.existing.run_request["editorial_brief"], selected_brief())
