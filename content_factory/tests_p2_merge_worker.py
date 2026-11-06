"""P2.7 M1-M12: real durable claims with mocked outbound provider effects."""
from django.db import connections
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import threading
from types import SimpleNamespace
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from . import publish_merge_worker as worker
from . import vibe_marketing_views as views
from .article_publish_approval import article_review_identity, make_article_publish_approval_receipt
from .p2_foundation import P2_BACKEND_RESULT_KEYS
from .self_heal_testing import ALL_FLAGS_ON, fake_member_context, fake_provider, make_p2_org, make_stamped_run, make_unstamped_run
from .website_models import WebsiteConnection
from workflow_runs.models import ContentFactoryRun

HEAD, MERGED, BODY, QUALITY = "b" * 40, "c" * 40, "d" * 64, "e" * 64


@override_settings(**ALL_FLAGS_ON)
class MergeWorkerTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.owner = self.fixture.organization, self.fixture.owner
        self.website = WebsiteConnection.objects.create(organization=self.org, repository_id=42,
            github_repo="fixture/site", installation_id="99", generation=1, branch="main", authorized_by=self.owner,
            site_url="https://" + self.org.domain, verified_sha="a" * 40, capabilities={})
        self.fixture.config.website_connection = self.website
        self.fixture.config.save(update_fields=["website_connection"])
        self.now = timezone.now()
        repo = {"id": 42, "full_name": "fixture/site"}
        self.pull = {"number": 12, "state": "open", "merged": False, "mergeable": True,
            "head": {"ref": "mlai/article", "sha": HEAD, "repo": repo},
            "base": {"ref": "main", "repo": deepcopy(repo)}}
        self.required, self.unknown, self.reviews = set(), True, False
        self.calls, self.tokens = [], []

    def run_fixture(self, *, kind="article", approved=True, stamp=True):
        receipt = {"pr_url": "https://github.com/fixture/site/pull/12", "number": 12, "head_sha": HEAD,
            "base": "main", "branch": "mlai/article", "repository_id": 42, "head_repository_id": 42,
            "kind": kind, "article_sha256": BODY, "review_quality_inputs_sha256": QUALITY}
        result = {"publish_pr_v1": receipt, "build_verification_v1": {"sha": HEAD, "status": "passed", "at": self.now.isoformat()},
            "article_url": "https://" + self.org.domain + "/blog/post"}
        if approved:
            key = "article_approval_v1" if kind == "article" else "owner_merge_approval_v1"
            result[key] = {"version": 1, "source": "publish_reviewed", "kind": kind, "head_sha": HEAD,
                "pr_url": receipt["pr_url"], "approved_by_user_id": self.owner.pk, "approved_at": self.now.isoformat(),
                "approved_body_sha256": BODY, "claims_open": 0}
        request = {"website_connection_id": str(self.website.pk), "connection_generation": 1, "repository_id": 42,
            "github_repo": "fixture/site", "domain": self.org.domain, "expected_source_sha": "a" * 40}
        run = (make_stamped_run if stamp else make_unstamped_run)(self.org, merge_policy="backend_worker" if stamp else None,
            workflow="article_system_setup" if kind != "article" else "article_generation", result=result,
            run_request=request, status="completed")
        run.domain, run.github_repo = self.org.domain, "fixture/site"
        run.save(update_fields=["domain", "github_repo"])
        return run

    def save(self, run):
        run.save(update_fields=["result", "run_request", "approval_state"])

    def api(self, website, token, method, path, **kwargs):
        self.assertFalse(connection.in_atomic_block, "provider HTTP ran inside a database transaction")
        self.calls.append((method, path, kwargs))
        if method == "PUT":
            self.assertEqual(kwargs["body"], {"sha": HEAD, "merge_method": "squash"})
            return {"merged": True, "sha": MERGED}
        if path == "/pulls/12":return deepcopy(self.pull)
        if path.startswith("/rules/"):return []
        if path.endswith("/protection"):raise worker.ProviderError(403)
        if path == "":return {"id": 42, "allow_squash_merge": True}
        if "/check-runs?" in path:return {"total_count": 0, "check_runs": []}
        if "/status?" in path:return {"sha": HEAD, "total_count": 0, "statuses": []}
        self.fail("Unexpected GitHub request " + path)

    def mint(self, **kwargs):
        self.assertFalse(connection.in_atomic_block)
        self.tokens.append(kwargs)
        return SimpleNamespace(token="synthetic-token")

    @contextmanager
    def providers(self):
        with patch.object(worker, "_authority", return_value=self.website), patch.object(worker, "_api", side_effect=self.api), patch.object(worker, "create_installation_access_token", side_effect=self.mint), patch.object(views, "_call_content_factory_run_action", return_value={"status": "queued"}):
            yield

    def sweep(self, *, minutes=0, limit=None):
        return worker.run_merge_sweep(now=self.now + timedelta(minutes=minutes), limit=limit)

    def test_page_closed_publish_reviewed_merges_once_with_exact_sha_and_scoped_tokens(self):
        run = self.run_fixture()
        before = run.updated_at
        with self.providers():
            self.assertEqual(self.sweep(), {"checked": 1, "merged": 1})
            self.assertEqual(self.sweep(minutes=20), {"checked": 0, "merged": 0})
        run.refresh_from_db()
        self.assertEqual(run.updated_at, before)
        self.assertEqual(run.result["merge_v1"]["merge_sha"], MERGED)
        self.assertEqual(len([row for row in self.calls if row[0] == "PUT"]), 1)
        self.assertTrue(all(row["repository_id"] == 42 for row in self.tokens))
        self.assertEqual(self.tokens[0]["permission_profile"], "ci_evidence")
        self.assertEqual(self.tokens[-1]["permission_mode"], "write")

    def test_retirement_publications_require_manual_merge_but_observe_it(self):
        run = self.run_fixture()
        run.run_request['retirement_requires_owner_merge'] = True
        self.save(run)
        with self.providers():
            self.assertEqual(self.sweep()['merged'], 0)
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.calls))
        self.pull.update(merged=True, state='closed', merge_commit_sha=MERGED)
        with self.providers():
            self.assertEqual(self.sweep(minutes=20)['merged'], 1)
        run.refresh_from_db()
        self.assertEqual(run.result['merge_v1']['method'], 'observed')
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.calls))

    def test_m1_setup_scaffold_proxy_never_merge_without_owner_click_across_three_sweeps(self):
        for kind in ("setup", "scaffold", "proxy"):
            run = self.run_fixture(kind=kind, approved=False)
            with self.providers():
                for minute in (0, 10, 30):self.sweep(minutes=minute)
            run.refresh_from_db()
            self.assertNotIn("merge_v1", run.result)
            run.delete()
        self.assertEqual(self.tokens, [])

    def test_m2_moved_head_asks_once_and_never_merges(self):
        run = self.run_fixture()
        self.pull["head"]["sha"] = "f" * 40
        with self.providers(), self.assertLogs(worker.logger, "INFO") as logs:
            self.sweep()
            self.sweep(minutes=61)
        self.assertEqual(sum("merge_head_moved" in line for line in logs.output), 1)
        run.refresh_from_db()
        self.assertEqual(run.result["user_action"]["id"], "approve_on_github")
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_m3_unknown_or_empty_required_set_needs_build_proof_for_exact_head(self):
        run = self.run_fixture()
        for proof in ({}, {"sha": "f" * 40, "status": "passed", "at": "now"}):
            run.result["build_verification_v1"] = proof
            run.result.pop("self_heal_v1", None)
            self.save(run)
            with self.providers():self.assertEqual(self.sweep()["merged"], 0)
        run.result["build_verification_v1"] = {"sha": HEAD, "status": "passed", "at": "now"}
        run.result.pop("self_heal_v1", None)
        self.save(run)
        with self.providers():self.assertEqual(self.sweep()["merged"], 1)

    def test_m4_review_policy_yields_one_protected_branch_ask_without_bypass(self):
        run = self.run_fixture()
        with self.providers(), patch.object(worker, "_policy", return_value=(set(), False, True)), self.assertLogs(worker.logger, "INFO") as logs:
            self.sweep()
            self.sweep(minutes=61)
        run.refresh_from_db()
        self.assertEqual(run.result["error_code"], "BRANCH_PROTECTION_APPROVAL_REQUIRED")
        self.assertEqual(sum("merge_review_required" in line for line in logs.output), 1)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_m5_foreign_fork_site_test_and_non_ml_ai_branches_never_merge(self):
        for key, value in (("repository_id", 99), ("head_repository_id", 99), ("branch", "foreign/branch"), ("kind", "site_test"), ("base", "other")):
            run = self.run_fixture()
            run.result["publish_pr_v1"][key] = value
            self.save(run)
            with self.providers():self.assertEqual(self.sweep()["merged"], 0)
            run.delete()
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_live_fork_cannot_borrow_a_valid_reported_receipt(self):
        self.run_fixture()
        self.pull["head"]["repo"]["id"] = 43
        with self.providers():self.assertEqual(self.sweep()["merged"], 0)

    def test_m6_already_merged_is_recorded_without_remerge(self):
        run = self.run_fixture()
        self.pull.update(merged=True, state="closed", merge_commit_sha=MERGED)
        with self.providers():self.assertEqual(self.sweep()["merged"], 1)
        run.refresh_from_db()
        self.assertEqual(run.result["merge_v1"]["method"], "observed")
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_m8_unstamped_and_flag_off_are_untouched(self):
        legacy = self.run_fixture(stamp=False)
        initial = deepcopy(legacy.result)
        with self.providers():self.assertEqual(self.sweep()["checked"], 0)
        legacy.refresh_from_db()
        self.assertEqual(legacy.result, initial)
        self.run_fixture()
        with override_settings(ARTICLES_MERGE_WORKER_ENABLED=False), self.providers():
            self.assertEqual(self.sweep()["checked"], 0)
        self.assertEqual(self.tokens, [])

    def test_m11_unreadable_or_open_claims_and_body_mismatch_never_merge(self):
        for claims in (None, 1, False):
            run = self.run_fixture()
            run.result["article_approval_v1"]["claims_open"] = claims
            self.save(run)
            with self.providers():self.assertEqual(self.sweep()["merged"], 0)
            run.delete()
        run = self.run_fixture()
        run.result["publish_pr_v1"]["article_sha256"] = "f" * 64
        self.save(run)
        with self.providers():self.assertEqual(self.sweep()["merged"], 0)

    def test_rights_are_rechecked_after_token_mint_and_removal_cannot_merge(self):
        run = self.run_fixture()
        def mint(**kwargs):
            if kwargs["permission_mode"] == "write":
                ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, "article_approval_v1": {}})
            return self.mint(**kwargs)
        with self.providers(), patch.object(worker, "create_installation_access_token", side_effect=mint):
            self.assertEqual(self.sweep()["merged"], 0)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_m12_poll_and_direct_setup_merge_skip_stamped_runs_with_flags_off(self):
        run = self.run_fixture(kind="setup")
        with override_settings(ARTICLES_MERGE_WORKER_ENABLED=False), patch.object(views, "_check_and_merge_publish_pr") as merge, patch.object(views, "_attempt_setup_publish_merge_authorized") as setup:
            self.assertIsNone(views._maybe_auto_merge_publish_child(child=run, viewed_run=run, context=None))
            self.assertEqual(views._attempt_setup_publish_merge(run=run, context=None)["outcome"], "waiting")
            merge.assert_not_called()
            setup.assert_not_called()

    def ordinary(self):
        run = self.run_fixture()
        run.approval_state = "approved"
        run.result.update(generation=1, resume_generation=0, livePreview={"previewUrl": "https://preview.example/post",
            "exactRender": True, "proof": {"commitSha": "f" * 40}}, article_preview_quality={"status": "passed",
            "inputs_sha256": QUALITY, "preview_url": "https://preview.example/post", "resume_generation": 0})
        run.result.pop("article_approval_v1")
        run.run_request["article_publish_approval_receipt"] = make_article_publish_approval_receipt(run, actor_id=self.owner.pk)
        self.save(run)
        from .founder_journey_cohort import update_marker
        update_marker(self.org, publish_automatically={"enabledAt": self.now.isoformat(), "enabledBy": self.owner.pk})
        return run

    def test_ordinary_article_copies_only_matching_explicit_receipt_and_current_claim_count(self):
        run = self.ordinary()
        canonical = {"reviewFindings": [], "reviewFindingsIdentity": {**article_review_identity(run), "revision": "r1"}}
        with self.providers(), patch("content_factory.article_review_views.remote_review", return_value=canonical):
            self.assertEqual(self.sweep()["merged"], 1)
        run.refresh_from_db()
        self.assertEqual(run.result["article_approval_v1"]["review_identity"], article_review_identity(run))
        self.assertEqual(run.result["article_approval_v1"]["claims_open"], 0)

    def test_ordinary_stale_or_missing_findings_quality_and_receipt_refuse_merge(self):
        for mode in ("missing", "stale", "quality", "approval"):
            run = self.ordinary()
            canonical = {"reviewFindings": [], "reviewFindingsIdentity": {**article_review_identity(run), "revision": "r1"}}
            if mode == "missing":canonical.pop("reviewFindings")
            if mode == "stale":canonical["reviewFindingsIdentity"]["quality_inputs_sha256"] = "0" * 64
            if mode == "quality":run.result["publish_pr_v1"]["review_quality_inputs_sha256"] = "0" * 64
            if mode == "approval":run.run_request["article_publish_approval_receipt"]["commit_sha"] = "0" * 40
            self.save(run)
            with self.providers(), patch("content_factory.article_review_views.remote_review", return_value=canonical):
                self.assertEqual(self.sweep()["merged"], 0)
            run.delete()

    def test_owner_click_records_head_and_manual_choice_revokes_worker_merge_authority(self):
        run = self.run_fixture(kind="setup", approved=False)
        from .website_setup_owner_approval import on_owner_setup_approve
        self.assertEqual(on_owner_setup_approve(organization=self.org, user=self.owner, run=run, data={}), {"merge": "auto"})
        run.refresh_from_db()
        self.assertEqual(run.result["owner_merge_approval_v1"]["head_sha"], HEAD)
        with self.providers():self.assertEqual(worker.run_merge_sweep()["merged"], 1)
        on_owner_setup_approve(organization=self.org, user=self.owner, run=run, data={"merge": "manual"})
        run.refresh_from_db()
        self.assertEqual(run.result["owner_merge_approval_v1"]["head_sha"], HEAD)
        self.assertIs(worker._state(run)["manual"], True)

    def test_member_cannot_record_owner_setup_consent(self):
        from core.models import User
        member = User.objects.create_user(email="member@synthetic.example.test")
        run = self.run_fixture(kind="setup", approved=False)
        with fake_member_context(members=(member.pk,), publishers=(member.pk,), owner_ids=(self.owner.pk,)):
            self.assertFalse(worker.record_owner_approval(run=run, organization=self.org, user=member, choice="auto"))
        run.refresh_from_db()
        self.assertNotIn("owner_merge_approval_v1", run.result)

    def test_snapshot_cannot_introduce_remove_or_change_backend_receipts(self):
        stored = {key: {"original": key} for key in P2_BACKEND_RESULT_KEYS}
        for incoming in ({}, {key: {"forged": True} for key in P2_BACKEND_RESULT_KEYS}):
            self.assertEqual(views._merge_django_owned_article_result(stored, incoming), stored)
        self.assertEqual(views._merge_django_owned_article_result({}, stored), {})

    def test_database_claim_serializes_two_simultaneous_sweeps(self):
        if connection.vendor != "postgresql":self.skipTest("PostgreSQL row-lock evidence")
        self.run_fixture()
        barrier = threading.Barrier(2)
        def sweep():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.sweep()
            finally:connections.close_all()
        with self.providers(), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: sweep(), range(2)))
        self.assertEqual(sum(row["merged"] for row in results), 1)
        self.assertEqual(len([row for row in self.calls if row[0] == "PUT"]), 1)

    def test_required_checks_are_union_of_rulesets_and_classic_and_exact_app(self):
        def api(website, token, method, path, **kwargs):
            if "/rules/" in path:return [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "build", "integration_id": 7}]}}]
            return {"required_status_checks": {"contexts": ["lint"], "checks": [{"context": "test", "app_id": 8}]}}
        with patch.object(worker, "_api", side_effect=api):
            required, unknown, reviews = worker._policy(self.website, "test")
        self.assertEqual(required, {("build", 7), ("test", 8), ("lint", None)})
        self.assertFalse(unknown or reviews)
        with patch.object(worker, "_api", side_effect=[{"sha": HEAD, "statuses": [{"context": "build", "state": "success"}]},
                {"check_runs": [{"head_sha": HEAD, "name": "build", "app": {"id": 9}, "status": "completed", "conclusion": "success"}]}]):
            self.assertEqual(worker._checks(self.website, "test", HEAD, {("build", 7)}), "pending")

    def test_m9_real_mint_body_is_bound_to_one_immutable_repository(self):
        from integrations.services import github_app
        response = SimpleNamespace(status_code=201, json=lambda: {"token": "synthetic-token", "expires_at": "2030-01-01T00:00:00Z",
            "permissions": {"contents": "write", "pull_requests": "write"}})
        with patch.object(github_app, "_github_app_jwt", return_value="synthetic-jwt"), patch.object(github_app.http_requests, "post", return_value=response) as post:
            self.assertEqual(worker._token(self.website), "synthetic-token")
        self.assertEqual(post.call_args.kwargs["json"], {"repository_ids": [42], "permissions": {"contents": "write", "pull_requests": "write"}})
        from pathlib import Path
        self.assertNotIn("_request_installation_access_token", Path(worker.__file__).read_text())

    def test_second_real_rejection_alerts_once_and_tick_limit_is_enforced(self):
        first = self.run_fixture()
        self.run_fixture()
        with self.providers(), patch.object(worker, "_checks", return_value="failed"), patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as alert:
            self.assertEqual(self.sweep(limit=1)["checked"], 1)
            self.sweep(minutes=10, limit=1)
            self.sweep(minutes=30, limit=1)
            self.assertEqual(alert.call_count, 1)
            self.assertEqual(alert.call_args.kwargs["run"].pk, first.pk)

    def test_connection_revocation_during_token_mint_is_fenced(self):
        self.run_fixture()
        def mint(**kwargs):
            if kwargs["permission_mode"] == "write":
                WebsiteConnection.objects.filter(pk=self.website.pk).update(state="revoked")
            return self.mint(**kwargs)
        with self.providers(), patch.object(worker, "create_installation_access_token", side_effect=mint):
            self.assertEqual(self.sweep()["merged"], 0)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_setup_example_prior_generation_acceptance_needs_backend_flag_and_stamp(self):
        from .website_setup_merge_policy import p2_setup_example_candidate_allowed
        run = self.run_fixture(kind="setup")
        run.result["setup_example_cleanup"] = [{"file": "articles.json", "record": "example", "ownership": "mutation_receipt", "basis": "mlai_seed_receipt_prior_connection"}]
        self.assertTrue(p2_setup_example_candidate_allowed(run))
        with override_settings(ARTICLES_SETUP_EXAMPLE_AUTO_REMOVE_ENABLED=False):
            self.assertFalse(p2_setup_example_candidate_allowed(run))
        run.run_request.pop("self_heal_policy_v1")
        self.assertFalse(p2_setup_example_candidate_allowed(run))

    def test_legacy_run_fields_are_empty_with_every_p2_flag_on(self):
        from .self_heal_testing import make_named_legacy_org
        for name in ("la_vie", "talathrive", "vyavos", "pawshare", "mlai"):
            legacy = make_named_legacy_org(name).organization
            run = make_unstamped_run(legacy, result={"error_code": "SOURCE_CHANGED"})
            self.assertEqual(worker.run_fields(run, organization=legacy), {})

    def test_installation_wakes_only_current_bound_p2_work_and_never_merges(self):
        from .website_wakeups import wake_installation_work
        run = self.run_fixture()
        run.result["self_heal_v1"] = {"next_at": (self.now + timedelta(hours=1)).isoformat(),
            "merge_worker": {"next_at": (self.now + timedelta(hours=1)).isoformat(), "rejections": 1}}
        self.save(run)
        payload = {"action": "created", "installation": {"id": 99}}
        with patch.object(worker, "run_merge_sweep") as merge:
            self.assertEqual(wake_installation_work("installation", payload), 1)
            merge.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(worker._state(run)["rejections"], 1)
        self.assertLessEqual(worker._moment(worker._state(run)["next_at"]), timezone.now())
        with override_settings(ARTICLES_GITHUB_INSTALLATION_WAKES_ENABLED=False):
            self.assertEqual(wake_installation_work("installation", payload), 0)
        self.assertEqual(wake_installation_work("installation_repositories", {"action": "added", "installation": {"id": 99}, "repositories_added": [{"id": 43}]}), 0)
        self.assertEqual(wake_installation_work("installation", {"action": "created", "installation": {"id": True}}), 0)

    def test_m7_invalid_webhook_signature_does_not_reach_wakes(self):
        from django.urls import reverse
        with override_settings(CONTENT_FACTORY_GITHUB_WEBHOOK_SECRET="synthetic-secret"), patch("content_factory.website_wakeups.wake_installation_work") as wake:
            response = self.client.post(reverse("content_factory_github_webhook"), data='{"action":"created","installation":{"id":99}}',
                content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256=bad", HTTP_X_GITHUB_EVENT="installation")
        self.assertEqual(response.status_code, 401)
        wake.assert_not_called()

    def watch_fixture(self, *, minutes=1):
        run = self.run_fixture()
        run.result["merge_v1"] = {"merged_at": (self.now - timedelta(minutes=minutes)).isoformat(), "merge_sha": MERGED, "head_sha": HEAD}
        self.save(run)
        return run

    def test_deployment_commit_statuses_for_all_three_providers_and_live_fetch(self):
        for label, provider in (("Vercel", "vercel"), ("netlify/site/deploy", "netlify"), ("Cloudflare Pages", "cloudflare")):
            run = self.watch_fixture()
            def api(website, token, method, path, **kwargs):
                self.assertFalse(connection.in_atomic_block)
                self.assertIn(MERGED, path)
                return {"statuses": [{"state": "success", "context": label}]}
            with self.providers(), patch.object(worker, "_api", side_effect=api), patch("content_factory.website_live_fetch.fetch_live_route", return_value=(b"article", {})) as fetch:
                self.assertEqual(worker.run_deployment_watch_sweep(now=self.now)["live"], 1)
                fetch.assert_called_once_with(run.result["article_url"], run.domain)
            run.refresh_from_db()
            self.assertEqual(run.result["deploy_v1"]["provider"], provider)
            run.delete()

    def test_deployment_records_are_exact_commit_production_only(self):
        for provider in ("vercel", "netlify", "cloudflare"):
            responses = [{"statuses": []}, [{"id": 1, "sha": "f" * 40, "production_environment": True, "environment": provider},
                {"id": 2, "sha": MERGED, "production_environment": False, "environment": provider},
                {"id": 3, "sha": MERGED, "production_environment": True, "environment": provider}], [{"state": "success"}]]
            with patch.object(worker, "_api", side_effect=responses) as api:
                self.assertEqual(worker.deployment_evidence(self.website, "test", MERGED), provider)
            self.assertIn("/deployments/3/statuses", api.call_args.args[3])

    def test_attested_deployment_calls_source_article_projection_after_receipt_is_durable(self):
        run = self.watch_fixture()
        observed = []
        def sync(current):
            self.assertFalse(connection.in_atomic_block)
            saved = ContentFactoryRun.objects.get(pk=current.pk)
            self.assertEqual(saved.result["deploy_v1"], current.result["deploy_v1"])
            observed.append(saved.run_id)
        with fake_provider("content_factory.publish_reviewed_draft", sync_published_article=sync), self.providers(), patch.object(worker, "deployment_evidence", return_value="vercel"), patch("content_factory.website_live_fetch.fetch_live_route", return_value=(b"article", {})):
            self.assertEqual(worker.run_deployment_watch_sweep(now=self.now)["live"], 1)
        self.assertEqual(observed, [run.run_id])

    def test_deployment_wait_alert_once_schedule_hourly_then_48_hour_wall(self):
        run = self.watch_fixture(minutes=30)
        self.website.capabilities = {"deploymentFingerprint": {"provider": "vercel"}}
        with self.providers(), patch.object(worker, "deployment_evidence", return_value=None), patch("content_factory.founder_journey_alerts.queue_founder_journey_alert") as alert:
            self.assertEqual(worker.run_deployment_watch_sweep(now=self.now)["checked"], 1)
            run.refresh_from_db()
            self.assertEqual(run.result["error_code"], "DEPLOYMENT_NOT_DETECTED")
            self.assertEqual(worker.run_fields(run, organization=self.org)["deployHost"], "Vercel")
            self.assertEqual(worker.run_deployment_watch_sweep(now=self.now + timedelta(minutes=30))["checked"], 0)
            self.assertEqual(worker.run_deployment_watch_sweep(now=self.now + timedelta(hours=1))["checked"], 1)
            self.assertEqual(alert.call_count, 1)
            with patch.object(worker, "_authority") as authority:
                self.assertEqual(worker.run_deployment_watch_sweep(now=self.now + timedelta(hours=49))["checked"], 1)
                authority.assert_not_called()
                self.assertEqual(worker.run_deployment_watch_sweep(now=self.now + timedelta(hours=50))["checked"], 0)

    def test_explicit_legacy_merge_api_cannot_bypass_the_stamp(self):
        run = self.run_fixture()
        with patch.object(views, "_get_config") as config:
            result = views._check_and_merge_publish_pr.__wrapped__(run=run, context=None)
        self.assertEqual(result["outcome"], "pending")
        config.assert_not_called()

    def test_setup_member_post_keeps_owner_required_and_writes_no_consent(self):
        from core.models import User
        from . import website_setup_owner_approval
        member = User.objects.create_user(email="post-member@synthetic.example.test")
        run = self.run_fixture(kind="setup", approved=False)
        context = SimpleNamespace(organization=self.org, profile=self.fixture.company.profile)
        with fake_member_context(members=(member.pk,), publishers=(member.pk,), owner_ids=(self.owner.pk,)), patch.object(website_setup_owner_approval, "on_owner_setup_approve") as approve, patch.object(views, "_call_content_factory_run_action") as remote:
            response = views.control_article_run(user=member, context=context, run=run, action="approve", data={})
        self.assertEqual((response.status_code, response.data["reason"]), (403, "owner_required"))
        approve.assert_not_called()
        remote.assert_not_called()
        run.refresh_from_db()
        self.assertNotIn("owner_merge_approval_v1", run.result)

    def test_stamped_setup_poll_queues_worker_after_owner_click(self):
        from .website_prepare import maybe_merge_prepared_setup
        from .website_setup_owner_approval import on_owner_setup_approve
        run = self.run_fixture(kind="setup", approved=False)
        run.result.update(pr_number=12, pr_url="https://github.com/fixture/site/pull/12", merge_status="not_merged")
        self.save(run)
        on_owner_setup_approve(organization=self.org, user=self.owner, run=run, data={})
        context = SimpleNamespace(organization=self.org, profile=self.fixture.company.profile)
        self.fixture.config.requires_review = False
        with patch.object(views, "_attempt_setup_publish_merge") as merge:
            result = maybe_merge_prepared_setup(run, context, self.fixture.config, user=self.owner)
        self.assertEqual(result, {"status": "pending"})
        merge.assert_not_called()

    def test_stamp_applies_once_before_dispatch_and_preserves_reserved_stamp(self):
        from .p2_foundation import stamp_run_request
        payload = {"self_heal_policy_v1": {"version": 1, "cohort": "legacy", "failure_policy": "pre_p2"}, "merge_policy": "backend_worker"}
        expected = deepcopy(payload)
        stamp_run_request(payload, organization=self.org, requester_user_id=self.owner.pk)
        self.assertEqual(payload, expected)
        context = SimpleNamespace(organization=self.org, profile=self.fixture.company.profile)
        with patch.object(views, "_queue_content_factory_run_authorized", return_value="queued") as dispatch:
            self.assertEqual(views._queue_content_factory_run(endpoint="article", workflow="portable_article", context=context, config=self.fixture.config,
                payload={"delivery_mode": "content_only"}), "queued")
        self.assertEqual(dispatch.call_args.kwargs["payload"]["merge_policy"], "backend_worker")
        self.assertEqual(dispatch.call_args.kwargs["payload"]["self_heal_policy_v1"]["cohort"], "p2")

    def test_optional_channel_start_seam_preserves_body_request_and_charge_identity(self):
        from .self_heal_testing import fake_provider
        from rest_framework.response import Response
        context = SimpleNamespace(organization=self.org, profile=self.fixture.company.profile)
        original = {"title": "An approved idea", "client_request_id": "one-charge", "delivery_mode": "review_draft"}
        sentinel = Response({"hold": True})
        with fake_provider("content_factory.channel_switch", article_start_delivery_mode=lambda **kwargs: "content_only"), patch.object(views, "_get_config", return_value=self.fixture.config), patch("content_factory.portable_drafts.keeps_copy_ready_start", return_value=True), patch.object(views, "_setup_blocked_response_for_generation", return_value=sentinel) as blocked:
            self.assertIs(views.start_article_generation(user=self.owner, context=context, data=original), sentinel)
        passed = blocked.call_args.kwargs["request_data"]
        self.assertEqual(passed["delivery_mode"], "content_only")
        self.assertEqual((passed["title"], passed["client_request_id"]), (original["title"], "one-charge"))

    def test_domain_claim_join_reference_seam_is_my_startup_only(self):
        from .self_heal_testing import fake_provider
        from rest_framework.response import Response
        def response():return Response({"reason": "company_domain_claimed"}, status=409)
        error = SimpleNamespace(organization=self.org, response=response)
        with fake_provider("founder_tools.startup_team", issue_join_reference=lambda **kwargs: "opaque-reference"), patch.object(views, "queue_domain_claim_alert"):
            result = views._domain_claimed_response(SimpleNamespace(user=self.owner, path="/api/v1/my-startup/settings/"), error)
            legacy = views._domain_claimed_response(SimpleNamespace(user=self.owner, path="/api/v1/vibe-marketing/settings/"), error)
        self.assertEqual(result.data["joinReference"], "opaque-reference")
        self.assertNotIn("joinReference", legacy.data)

    def test_notification_owner_seam_stands_old_sender_down_only_when_provider_owns_org(self):
        from . import website_prepare as prepare
        from .self_heal_testing import fake_provider
        op = SimpleNamespace(connection=self.website, state="pending", receipt={})
        with fake_provider("content_factory.founder_transitions", owns_notifications=lambda org: org.pk == self.org.pk), patch("content_factory.website_notification_outbox.enabled", return_value=True), patch("content_factory.website_goal_runner.owns_goal", return_value=True), patch("content_factory.website_cost_recovery.pending_for", return_value=False), patch("content_factory.website_operational_alerts.budget_alert", return_value=None), patch("content_factory.website_operational_alerts.stall_alert", return_value=None), patch.object(prepare, "_persist_prepare_state", return_value=op), patch("content_factory.website_notification_outbox.track_transition") as notice:
            self.assertIs(prepare._persist_prepare(op, now=self.now), op)
            notice.assert_not_called()

    def test_serializer_wire_ids_outcome_seams_and_l1_full_payload(self):
        from .self_heal_testing import fake_provider, make_named_legacy_org, ALL_P2_FLAGS_ON
        run = self.run_fixture()
        run.result.update(error_code="BRANCH_PROTECTION_APPROVAL_REQUIRED", user_action={"id": "approve_on_github", "url": "https://github.com/fixture/site/pull/12"})
        with fake_provider("content_factory.no_delivery_refunds", points_outcome=lambda _: {"state": "refunded", "points": 12}, cancel_outcome=lambda _: {"state": "refund", "points": 12, "freeArticle": False}):
            fields = worker.run_fields(run, organization=self.org)
            self.assertEqual(fields["pointsOutcome"]["points"], 12)
            self.assertEqual(fields["userAction"]["id"], "approve_on_github")
            with override_settings(MY_STARTUP_CANONICAL_ACTION_IDS_ENABLED=False):
                self.assertEqual(worker.run_fields(run, organization=self.org)["userAction"]["id"], "merge_pr")
        built = make_named_legacy_org("talathrive")
        legacy = make_unstamped_run(built.organization, workflow="topic_research", result={"error_code": "SOURCE_CHANGED"})
        context = SimpleNamespace(organization=built.organization, profile=built.company.profile)
        with patch.object(views, "_article_setup_state", return_value={}), patch.object(views, "_live_preview_from_run", return_value=None):
            with override_settings(**{name: False for name in ALL_P2_FLAGS_ON}):
                before = views._serialize_run(legacy, context=context, mode="summary", include_workflow_progress=False)
            after = views._serialize_run(legacy, context=context, mode="summary", include_workflow_progress=False)
        self.assertEqual(before, after)

    def test_inactive_approver_and_cross_organization_authority_are_refused(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertFalse(worker._approver(self.org, self.owner.pk))
        run = self.run_fixture()
        run.organization_id = self.org.pk + 1
        @contextmanager
        def guard(*args, **kwargs):yield self.website
        with patch.object(worker, "authority_guard", side_effect=guard), self.assertRaises(worker.WebsiteAuthorityError):
            worker._authority(run)

    def test_proxy_owner_approve_works_after_cf_renames_workflow_and_only_worker_merges(self):
        run = self.run_fixture(kind="proxy", approved=False)
        run.workflow = "hosted_native_verification"
        run.save(update_fields=["workflow"])
        context = SimpleNamespace(organization=self.org, profile=self.fixture.company.profile)
        with patch.object(views, "_serialize_run", return_value={"runId": run.run_id}), patch.object(views, "_call_content_factory_run_action") as remote:
            result = views.control_article_run(user=self.owner, context=context, run=run, action="approve", data={"merge": "auto"})
        self.assertEqual(result.status_code, 202)
        remote.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.result["owner_merge_approval_v1"]["kind"], "proxy")
        with self.providers():self.assertEqual(worker.run_merge_sweep()["merged"], 1)

    def test_delivery_mode_requires_completed_cf_conversion_and_real_export_evidence(self):
        run = self.run_fixture()
        run.run_request["delivery_mode"] = "review_draft"
        run.result["channel_v1"] = {"version": 1, "channel": "copy_ready", "dispatch_state": "pending"}
        self.assertEqual(views._run_delivery_mode(run), "review_draft")
        run.result["channel_v1"]["dispatch_state"] = "accepted"
        run.result["channel_switch_cf_v1"] = {"version": 1, "run_id": run.run_id, "key": run.run_id + ":copy-ready", "delivery_mode": "content_only", "state": "applied"}
        self.assertEqual(views._run_delivery_mode(run), "review_draft")
        run.result["channel_switch_cf_v1"]["state"] = "completed"
        self.assertEqual(views._run_delivery_mode(run), "review_draft")
        run.result["content_package"] = {"article_markdown": "Exact saved article"}
        self.assertEqual(views._run_delivery_mode(run), "content_only")
        run.result["channel_switch_cf_v1"]["run_id"] = "foreign"
        self.assertEqual(views._run_delivery_mode(run), "review_draft")

    def test_owner_click_brings_waiting_worker_due_now_without_resetting_caps_or_rejections(self):
        run = self.run_fixture(kind="proxy", approved=False)
        with self.providers():
            self.assertEqual(self.sweep()["merged"], 0)
        run.refresh_from_db()
        prior = worker._state(run)
        self.assertGreater(worker._moment(prior["next_at"]), self.now)
        worker.record_owner_approval(run=run, organization=self.org, user=self.owner, choice="auto")
        run.refresh_from_db()
        self.assertEqual(worker._state(run)["attempts"], prior["attempts"])
        self.assertEqual(worker._state(run)["rejections"], prior["rejections"])
        with self.providers():
            self.assertEqual(worker.run_merge_sweep()["merged"], 1)

    def test_manual_owner_choice_never_puts_but_attests_same_owned_github_merge(self):
        run = self.run_fixture(kind="proxy", approved=False)
        worker.record_owner_approval(run=run, organization=self.org, user=self.owner, choice="manual")
        with self.providers():
            self.assertEqual(worker.run_merge_sweep()["merged"], 0)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))
        self.pull.update(merged=True, state="closed", merge_commit_sha=MERGED)
        with self.providers():
            self.assertEqual(self.sweep(minutes=2)["merged"], 1)
        run.refresh_from_db()
        self.assertEqual(run.result["merge_v1"]["method"], "observed")
        self.assertEqual(run.result["merge_v1"]["approved_by_user_id"], self.owner.pk)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_manual_choice_during_token_mint_revokes_the_old_claim_without_put(self):
        run = self.run_fixture(kind="proxy")
        def mint(**kwargs):
            if kwargs["permission_mode"] == "write":
                self.assertTrue(worker.record_owner_approval(
                    run=run, organization=self.org, user=self.owner, choice="manual"))
            return self.mint(**kwargs)
        with self.providers(), patch.object(worker, "create_installation_access_token", side_effect=mint):
            self.assertEqual(self.sweep()["merged"], 0)
        run.refresh_from_db()
        self.assertIs(worker._state(run)["manual"], True)
        self.assertNotIn("claim", worker._state(run))
        self.assertNotIn("merge_v1", run.result)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_manual_observation_rechecks_consent_after_provider_read(self):
        run = self.run_fixture(kind="proxy")
        self.pull.update(merged=True, state="closed", merge_commit_sha=MERGED)
        def api(*args, **kwargs):
            result = self.api(*args, **kwargs)
            if args[3] == "/pulls/12":
                ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, "owner_merge_approval_v1": {}})
            return result
        with self.providers(), patch.object(worker, "_api", side_effect=api):
            self.assertEqual(self.sweep()["merged"], 0)
        run.refresh_from_db()
        self.assertNotIn("merge_v1", run.result)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.calls))

    def test_existing_merge_observation_omits_old_source_pins_but_new_merge_requires_them(self):
        run = self.run_fixture(kind="proxy")
        calls = []
        @contextmanager
        def guard(payload, *, action):
            calls.append((deepcopy(payload), action))
            if "expected_source_sha" in payload:
                raise worker.WebsiteAuthorityError("source_changed", "Source moved.")
            yield self.website
        with patch.object(worker, "authority_guard", side_effect=guard):
            self.assertEqual(worker._authority(run, watch=True), self.website)
            with self.assertRaises(worker.WebsiteAuthorityError):
                worker._authority(run)
        self.assertEqual(calls[0][1], "read")
        self.assertNotIn("expected_source_sha", calls[0][0])
        self.assertEqual(calls[1][1], "setup")
        self.assertEqual(calls[1][0]["expected_source_sha"], "a" * 40)
        for payload, _ in calls:
            self.assertEqual(payload["website_connection_id"], str(self.website.pk))
            self.assertEqual(payload["connection_generation"], 1)
            self.assertEqual(payload["repository_id"], 42)
