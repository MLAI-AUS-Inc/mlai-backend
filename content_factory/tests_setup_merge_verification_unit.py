"""First-time setup merge proof, without database or provider access."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from . import service_views, website_connections as authority
from .website_contract import WebsiteAuthorityError


class SetupMergeVerificationUnitTests(SimpleTestCase):
    def setUp(self):
        self.base, self.head = "a" * 40, "b" * 40
        identifier = "11111111-1111-4111-8111-111111111111"
        self.website = SimpleNamespace(id=identifier, pk=identifier, generation=2,
            repository_id=123, github_repo="example/site", state="connected", verified_sha="",
            organization=SimpleNamespace(domain="example.test"), targets=Mock())
        self.website.targets.filter.return_value = []
        self.run = SimpleNamespace(run_id="setup-1", workflow="article_system_setup", status="completed",
            approval_state="approved", domain="example.test", github_repo="example/site",
            run_request={"website_connection_id": identifier, "connection_generation": 2,
                "repository_id": 123, "expected_source_sha": self.base},
            result={"status": "setup_pr_created", "source_sha": self.base, "branch_commit_sha": self.head,
                "resume_generation": 1,
                "article_system_setup": {"status": "pr_created", "setup_run_id": "setup-1",
                    "source_sha": self.base, "branch_commit_sha": self.head, "resume_generation": 1},
                "live_preview": {"status": "running", "exactRender": True, "commitSha": self.head},
                "directory_quality_gates": {"status": "passed", "local_build_skipped": False,
                    **{key: True for key in ("dependency_validation", "static_policy", "local_build",
                        "dom_slot_compliance", "publish_surface", "browser", "visual_style")}},
                "directory_quality_verification": {"status": "passed", "commit_sha": self.head,
                    "preview_identity": f"commit:{self.head}", "resume_generation": 1,
                    "task_id": "verification-1", "completed_at": "2026-10-09T10:45:37+00:00"}})

    def validate(self, head=None):
        with patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website), \
                patch.object(authority, "verify_repository_head", return_value=self.base) as verify:
            authority.validate_setup_merge_source(self.run, head or self.head)
        verify.assert_called_once_with(self.website, self.base)

    def test_first_setup_uses_exact_approved_run_without_promoting_target(self):
        before = deepcopy(self.run.result)
        self.validate()
        self.assertEqual(self.run.result, before)
        self.assertEqual(self.website.verified_sha, "")
        self.website.targets.update_or_create.assert_not_called()

    def test_preserves_existing_target_proof_path(self):
        self.website.verified_sha = self.base
        self.website.targets.filter.return_value = [SimpleNamespace(contract={"verification": {
            "status": "preview_verified", "source_sha": self.head, "base_sha": self.base}})]
        self.run.result = {}
        self.validate()

    def test_rejects_changed_head_and_incomplete_or_stale_run_proof(self):
        original = deepcopy(self.run.result)
        changes = [
            ((), "branch_commit_sha", "c" * 40),
            ((), "source_sha", "c" * 40),
            ((), "status", "preview_ready"),
            ((), "resume_generation", True),
            ((), "manual_quality_gate_override", {"approved": True}),
            (("article_system_setup",), "branch_commit_sha", "c" * 40),
            (("article_system_setup",), "source_sha", "c" * 40),
            (("article_system_setup",), "setup_run_id", "other"),
            (("article_system_setup",), "resume_generation", 0),
            (("live_preview",), "exactRender", False),
            (("live_preview",), "commitSha", "c" * 40),
            (("directory_quality_verification",), "commit_sha", "c" * 40),
            (("directory_quality_verification",), "preview_identity", "commit:" + "c" * 40),
            (("directory_quality_verification",), "resume_generation", 0),
            (("directory_quality_verification",), "resume_generation", True),
            (("directory_quality_verification",), "status", "running"),
            (("directory_quality_verification",), "completed_at", None),
            (("directory_quality_verification",), "task_id", None),
            (("directory_quality_gates",), "local_build_skipped", True),
            (("directory_quality_gates",), "browser", False),
            (("directory_quality_gates",), "visual_style", False),
            (("directory_quality_gates",), "status", "failed"),
        ]
        for path, key, value in changes:
            with self.subTest(key=key, value=value):
                self.run.result = deepcopy(original)
                node = self.run.result
                for part in path:
                    node = node[part]
                node[key] = value
                with self.assertRaises(WebsiteAuthorityError):
                    self.validate()
        self.run.result = original
        with self.assertRaises(WebsiteAuthorityError):
            self.validate("c" * 40)

    def test_rejects_unapproved_cancelled_and_other_workflows(self):
        for key, value in (("approval_state", "pending"), ("status", "cancelled"), ("workflow", "article_generation")):
            with self.subTest(key=key):
                previous = getattr(self.run, key)
                setattr(self.run, key, value)
                with self.assertRaises(WebsiteAuthorityError):
                    self.validate()
                setattr(self.run, key, previous)

    def test_rejects_revoked_rebound_repository_and_changed_base(self):
        for key, value in (("state", "revoked"), ("generation", 3), ("repository_id", 999),
                ("verified_sha", "c" * 40)):
            with self.subTest(key=key):
                previous = getattr(self.website, key)
                setattr(self.website, key, value)
                with self.assertRaises(WebsiteAuthorityError):
                    self.validate()
                setattr(self.website, key, previous)
        with patch.object(authority.WebsiteConnection.objects, "get", return_value=self.website), \
                patch.object(authority, "verify_repository_head", side_effect=WebsiteAuthorityError("website_source_changed", "Changed")):
            with self.assertRaises(WebsiteAuthorityError):
                authority.validate_setup_merge_source(self.run, self.head)


class SetupMergeSnapshotUnitTests(SimpleTestCase):
    def setUp(self):
        self.existing = {"merge_status": "merged", "merged_at": "2026-10-09T11:41:48+00:00",
            "publish_merge_intent": {"run_id": "setup-1", "head_sha": "b" * 40},
            "article_system_setup": {"setup_run_id": "setup-1", "merge_status": "merged"}}
        self.incoming = {"status": "setup_pr_created", "merge_status": "not_merged",
            "source_sha": "a" * 40, "resume_generation": 1,
            "directory_quality_gates": {"status": "passed"},
            "article_system_setup": {"setup_run_id": "setup-1", "status": "pr_created",
                "merge_status": "not_merged", "mergeStatus": "not_merged",
                "generationReady": False, "publishingReady": False}}

    def test_worker_checkpoint_retains_confirmed_merge_in_nested_ui_without_readiness(self):
        before = deepcopy((self.existing, self.incoming))
        result = service_views._merge_django_owned_run_result(self.existing, self.incoming)
        self.assertEqual(result["merge_status"], "merged")
        setup = result["article_system_setup"]
        self.assertEqual((setup["merge_status"], setup["mergeStatus"]), ("merged", "merged"))
        self.assertEqual(setup["merged_at"], self.existing["merged_at"])
        self.assertEqual(setup["status"], "pr_created")
        self.assertIs(setup["generationReady"], False)
        self.assertIs(setup["publishingReady"], False)
        for key in ("status", "source_sha", "resume_generation", "directory_quality_gates"):
            self.assertEqual(result[key], self.incoming[key])
        self.assertEqual((self.existing, self.incoming), before)

    def test_later_polls_and_camel_case_identity_keep_the_same_merge_observation(self):
        for case in ("setup_run_id", "setupRunId"):
            with self.subTest(case=case):
                old, incoming = deepcopy(self.existing), deepcopy(self.incoming)
                old["article_system_setup"][case] = old["article_system_setup"].pop("setup_run_id")
                incoming["article_system_setup"][case] = incoming["article_system_setup"].pop("setup_run_id")
                first = service_views._merge_django_owned_run_result(old, incoming)
                second = service_views._merge_django_owned_run_result(first, incoming)
                self.assertEqual(first, second)
                self.assertEqual(second["article_system_setup"]["merge_status"], "merged")

    def test_missing_local_merge_observation_cannot_be_created_by_a_worker(self):
        for missing in ("merge_status", "merged_at", "article_system_setup"):
            with self.subTest(missing=missing):
                old = deepcopy(self.existing)
                old.pop(missing)
                incoming = deepcopy(self.incoming)
                incoming.update(merge_status="merged", merged_at=self.existing["merged_at"])
                result = service_views._merge_django_owned_run_result(old, incoming)
                self.assertEqual(result["article_system_setup"]["merge_status"], "not_merged")

    def test_different_or_missing_setup_identity_does_not_inherit_merge(self):
        for identifier in (None, "other-setup"):
            with self.subTest(identifier=identifier):
                incoming = deepcopy(self.incoming)
                incoming["article_system_setup"]["setup_run_id"] = identifier
                result = service_views._merge_django_owned_run_result(self.existing, incoming)
                self.assertEqual(result["article_system_setup"]["merge_status"], "not_merged")

    def test_sparse_or_malformed_setup_payload_is_not_filled_with_authority(self):
        for value in (None, {}, [], "invalid"):
            with self.subTest(value=value):
                incoming = {"article_system_setup": value}
                result = service_views._merge_django_owned_run_result(self.existing, incoming)
                self.assertEqual(result["article_system_setup"], value)


class SetupSourceReverificationUnitTests(SimpleTestCase):
    def setUp(self):
        import uuid
        self.website = SimpleNamespace(pk=uuid.uuid4(), generation=2, repository_id=123,
            github_repo="example/site", app_root="", branch="main", state="connected",
            installation_id="456", configuration_version=3,
            organization=SimpleNamespace(domain="example.test"), targets=Mock())
        self.config = SimpleNamespace(default_publish_target_id=None)
        self.target = None
        self.website.targets.filter.side_effect = lambda **kwargs: SimpleNamespace(
            first=lambda: self.target if kwargs.get("target_key") == "featured" else None)
        self.op = SimpleNamespace(pk=uuid.uuid4(), connection=self.website, generation=2,
            payload={"source_sha": "b" * 40, "target_id": None, "scan_required": False,
                "owned_merge_run_id": "setup-1"}, receipt={}, attempts=0,
            next_attempt_at=None, save=Mock())

    def process(self, *, now=None):
        from . import website_reconciliation as reconciliation, website_operations as operations
        from . import website_verification as verification
        with ExitStack() as stack:
            stack.enter_context(patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()))
            claimed = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update"))
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = self.op
            stored = stack.enter_context(patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter"))
            stored.return_value.update.return_value = 1
            stack.enter_context(patch.object(authority, "authority_guard", side_effect=lambda *a, **kw: nullcontext(self.website)))
            stack.enter_context(patch.object(authority, "require_unlocked_remote_call"))
            owned = stack.enter_context(patch.object(reconciliation, "_find_owned_merge"))
            stack.enter_context(patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=self.config))
            stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "https://worker.example.test"}))
            stack.enter_context(patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={}))
            create = stack.enter_context(patch("content_factory.vibe_marketing_views._create_local_run",
                return_value=SimpleNamespace(run_id="current-source-scan")))
            reserve = stack.enter_context(patch.object(operations, "reserve_workflow_operation"))
            bind = stack.enter_context(patch.object(operations, "bind_operation_run"))
            post = stack.enter_context(patch("integrations.http_client.post", return_value=SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: {"run_id": "current-source-scan"})))
            proof = {"source_sha": "b" * 40}
            discover = stack.enter_context(patch.object(verification, "discover_source_attestation", return_value=proof))
            ci = stack.enter_context(patch.object(verification, "record_ci_attestation",
                return_value=SimpleNamespace(pk="ci-1", receipt=proof)))
            live = stack.enter_context(patch.object(verification, "verify_live_deployment",
                return_value=SimpleNamespace(pk="live-1")))
            result = reconciliation._process_source_reverification(self.op.pk, now or timezone.now())
        return SimpleNamespace(result=result, receipt=stored.return_value.update.call_args.kwargs["receipt"],
            reserve=reserve, bind=bind, post=post, create=create, discover=discover, ci=ci, live=live, owned=owned)

    def test_owned_first_setup_without_target_dispatches_current_source_scan_once(self):
        observed = self.process()
        self.assertEqual(observed.result, "pending")
        self.assertEqual(observed.receipt["scan_run_id"], "current-source-scan")
        self.assertEqual(observed.receipt["code"], "verified_target_required")
        observed.post.assert_called_once()
        payload = observed.post.call_args.kwargs["json"]
        self.assertEqual(payload["expected_source_sha"], "b" * 40)
        self.assertEqual(payload["website_connection_id"], str(self.website.pk))
        self.assertEqual(payload["connection_generation"], 2)
        self.assertIs(payload["force_refresh"], True)
        observed.reserve.assert_called_once()
        self.assertEqual(observed.reserve.call_args.kwargs["workflow"], "repo_scan")
        observed.bind.assert_called_once()
        observed.owned.assert_not_called()
        observed.discover.assert_not_called()
        observed.ci.assert_not_called()
        observed.live.assert_not_called()

        self.op.receipt = observed.receipt
        observed = self.process(now=timezone.now() + timedelta(minutes=6))
        self.assertEqual(observed.result, "pending")
        self.assertEqual(observed.receipt["scan_run_id"], "current-source-scan")
        observed.post.assert_not_called()
        observed.reserve.assert_not_called()
        observed.live.assert_not_called()

    def test_scanned_default_target_proceeds_through_current_ci_and_live_verification(self):
        self.op.receipt = {"scan_run_id": "current-source-scan"}
        self.config.default_publish_target_id = "featured"
        self.target = SimpleNamespace(target_key="featured")
        observed = self.process()
        self.assertEqual(observed.result, "completed")
        self.assertEqual(observed.receipt["status"], "verified")
        self.assertEqual(observed.receipt["source_sha"], "b" * 40)
        observed.post.assert_not_called()
        observed.reserve.assert_not_called()
        observed.discover.assert_called_once_with(self.website, self.target, "b" * 40)
        observed.ci.assert_called_once_with({"source_sha": "b" * 40})
        observed.live.assert_called_once_with(self.config, data={"source_sha": "b" * 40})
        self.assertTrue(all(call.kwargs["generation"] == 2 for call in self.website.targets.filter.call_args_list))
