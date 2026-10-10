"""Observation fallback retains progress without granting repository authority."""
from types import SimpleNamespace
from unittest.mock import Mock
from django.test import SimpleTestCase


class RunObservationTests(SimpleTestCase):
    def test_admitted_progress_retains_exact_editorial_receipt_and_rejects_changed_selection(self):
        from copy import deepcopy
        from .run_observations import observation_payload
        from .editorial_run_state import EditorialRunConflict, merge_editorial_run_snapshot
        from .tests_editorial_snapshot_unit import admitted_snapshot
        original = admitted_snapshot()
        original["github_repo"] = "fixture/site"
        run = SimpleNamespace(**original, result={})
        incoming = deepcopy(original)
        incoming["run_request"]["connection_generation"] = 999
        safe = observation_payload(incoming, run)
        merged = merge_editorial_run_snapshot(original, safe)
        self.assertEqual(merged["run_request"]["editorial_admission"], original["run_request"]["editorial_admission"])
        self.assertNotIn("connection_generation", safe["run_request"])
        incoming["run_request"]["editorial_admission"]["selection_sha256"] = "0" * 64
        with self.assertRaises(EditorialRunConflict):
            merge_editorial_run_snapshot(original, observation_payload(incoming, run))

    def test_progress_is_retained_but_nested_authority_is_removed(self):
        from .run_observations import observation_payload
        run = SimpleNamespace(domain="example.test", github_repo="fixture/site", run_request={"client_request_id": "dispatch-1"}, result={})
        payload = {"domain": "example.test", "status": "running", "publish_targets": [{"id": "forged"}],
            "run_request": {"connection_generation": 99, "client_request_id": "forged-key"},
            "result": {"message": "Drafting", "prUrl": "https://example.test/pr", "build_verified": True,
                       "article_system_setup": {"publishingReady": True}}}
        projected = observation_payload(payload, run)
        self.assertEqual(projected["status"], "running")
        self.assertEqual(projected["result"], {"message": "Drafting"})
        self.assertNotIn("publish_targets", projected)
        self.assertEqual(projected["run_request"], run.run_request)

    def test_existing_run_requires_same_domain_and_organisation(self):
        from .run_observations import valid_existing_provenance
        run = SimpleNamespace(domain="example.test", organization_id=1, run_request={}, github_repo="fixture/site")
        website = SimpleNamespace(organization_id=2)
        self.assertFalse(valid_existing_provenance(run, {"domain": "other.test"}))
        self.assertFalse(valid_existing_provenance(run, {"domain": "example.test"}, website))
        self.assertTrue(valid_existing_provenance(run, {"domain": "example.test"}))
        for state in ("denied", "cancelled"):
            run.status = state
            self.assertFalse(valid_existing_provenance(run, {"domain": "example.test"}))

    def test_worker_cannot_replace_the_saved_operation_or_publication_approval(self):
        from .run_observations import observation_payload
        run = SimpleNamespace(domain="example.test", github_repo="fixture/site", result={},
            run_request={"operation_id": "saved", "operation_attempt": 1, "deletion_epoch": 2})
        result = observation_payload({"domain": "example.test", "run_request": {
            "operation_id": "forged", "operation_attempt": 99, "deletion_epoch": 0}, "result": {
            "approvalReceipt": {"approved": True}, "publish_approved": True, "message": "Draft ready"}}, run)
        self.assertEqual(result["run_request"], run.run_request)
        self.assertEqual(result["result"], {"message": "Draft ready"})

    def test_new_run_requires_a_backend_reserved_dispatch(self):
        from .run_observations import valid_dispatch_provenance
        operation = SimpleNamespace(state="running", payload={"client_request_id": "dispatch-1", "domain": "example.test"},
            connection=SimpleNamespace(organization=SimpleNamespace(domain="example.test")))
        self.assertTrue(valid_dispatch_provenance(operation, {"domain": "example.test", "run_request": {"client_request_id": "dispatch-1"}}))
        self.assertFalse(valid_dispatch_provenance(operation, {"domain": "example.test", "run_request": {"client_request_id": "unknown"}}))
        operation.state = "cancelled"
        self.assertFalse(valid_dispatch_provenance(operation, {"domain": "example.test", "run_request": {"client_request_id": "dispatch-1"}}))


class ObservationGuardTests(SimpleTestCase):
    def test_explicit_cancelled_operation_never_enters_observation_fallback(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from . import website_connections as guards
        from .website_contract import WebsiteAuthorityError
        @contextmanager
        def denied(*args, **kwargs):
            raise WebsiteAuthorityError("website_operation_cancelled", "The operation was stopped")
            yield
        handler = guards.guarded_service_write("config_write", observation_fallback=True)(Mock())
        with patch.object(guards, "authority_guard", denied), patch("content_factory.run_observations.proven_run") as prove, \
                patch.object(guards, "record_denied_terminal_callback", return_value=False):
            response = handler(None, SimpleNamespace(method="PUT", data={"domain": "example.test"}), run_id="run")
        self.assertEqual(response.status_code, 409)
        prove.assert_not_called()

    def test_generation_refusal_is_a_200_observation_and_not_a_terminal_callback(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from rest_framework.response import Response
        from . import website_connections as guards
        from .website_contract import WebsiteAuthorityError
        @contextmanager
        def denied(*args, **kwargs):
            raise WebsiteAuthorityError("website_connection_changed", "Generation changed")
            yield
        method = Mock()
        handler = guards.guarded_service_write("config_write", observation_fallback=True)(method)
        request = SimpleNamespace(method="PUT", data={"domain": "example.test", "status": "running",
            "run_request": {}, "result": {"publish_targets": [{"id": "fake"}], "message": "Drafting"}})
        run = SimpleNamespace(domain="example.test", github_repo="fixture/site", run_request={}, result={})
        with patch.object(guards, "authority_guard", denied), patch("content_factory.run_observations.proven_run", return_value=run), \
                patch("content_factory.service_views._apply_run_snapshot", return_value=Response({"run_id": "run"})) as apply, \
                patch.object(guards, "record_denied_terminal_callback") as terminal:
            response = handler(None, request, run_id="run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sync_status"], "observation_only")
        self.assertEqual(apply.call_args.args[1]["result"]["message"], "Drafting")
        self.assertNotIn("publish_targets", apply.call_args.args[1]["result"])
        terminal.assert_not_called(); method.assert_not_called()

    def test_unknown_provenance_retains_denial(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from . import website_connections as guards
        from .website_contract import WebsiteAuthorityError
        @contextmanager
        def denied(*args, **kwargs):
            raise WebsiteAuthorityError("website_scope_mismatch", "Another company")
            yield
        handler = guards.guarded_service_write("config_write", observation_fallback=True)(Mock())
        with patch.object(guards, "authority_guard", denied), patch("content_factory.run_observations.proven_run", return_value=None), \
                patch.object(guards, "record_denied_terminal_callback", return_value=False):
            response = handler(None, SimpleNamespace(method="PUT", data={"domain": "other.test"}), run_id="run")
        self.assertEqual(response.status_code, 409)

    def test_cancelled_snapshot_remains_409_even_with_provenance(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from rest_framework.response import Response
        from . import website_connections as guards
        from .website_contract import WebsiteAuthorityError
        @contextmanager
        def denied(*args, **kwargs):
            raise WebsiteAuthorityError("website_connection_changed", "Old generation")
            yield
        handler = guards.guarded_service_write("config_write", observation_fallback=True)(Mock())
        run = SimpleNamespace(domain="example.test", github_repo="fixture/site", run_request={}, result={})
        with patch.object(guards, "authority_guard", denied), patch("content_factory.run_observations.proven_run", return_value=run), \
                patch("content_factory.service_views._apply_run_snapshot", return_value=Response({"error": "run_cancelled"}, status=409)):
            response = handler(None, SimpleNamespace(method="PUT", data={"domain": "example.test"}), run_id="run")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("sync_status", response.data)
