"""Both founder delivery routes, their isolated credentials and post-merge verification."""
import base64
import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from workflow_runs.models import ContentFactoryRun
from . import reviewed_article_pr as pr
from . import reviewed_article_pr_verify as verify
from . import reviewed_article_pr_worker as worker
from .activation import live_deployment_verified
from .founder_journey_testing import make_new_journey_org
from .models import WrittenArticle
from .tests_founder_draft_tools import _run, _review
from .website_content_files import has_static_deployment_proof
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_operations import validate_operation
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_SETUP_LADDER_ENABLED=True, ROO_API_KEY="fixture-service",
                   CONTENT_FACTORY_URL="http://factory.test", CONTENT_FACTORY_API_KEY="fixture-worker")
class ReviewedArticlePRTests(TestCase):
    def setUp(self):
        self.startup = make_new_journey_org()
        self.org, self.cfg, self.user = self.startup.organization, self.startup.config, self.startup.owner
        self.site = WebsiteConnection.objects.create(organization=self.org, repository_id=72, github_repo="example/blog",
                installation_id="9", branch="main", site_url=f"https://{self.org.domain}")
        self.cfg.website_connection, self.cfg.github_repo = self.site, self.site.github_repo
        self.cfg.save()
        self.run = _run(self.org)
        self.setup = ContentFactoryRun.objects.create(organization=self.org, domain=self.org.domain, run_id="setup",
                workflow="article_system_setup", approval_state="approved", status="completed", github_repo=self.site.github_repo,
                run_request=pr.contract_for(self.site), result={"setup_ladder": {"tier": "content_files",
                       "outcome": "handoff_prepared", "counts_as_published": False}})
        self.review = _review(self.run.run_id, revision="8"*64, media=[])
        self.review.update(approved=True, previewPending=False, reviewFindings=[])
        exp = self.review["articleExport"]
        self.review["reviewedContentFilesV1"] = {"version": 1, "runId": self.run.run_id, "revision": exp["revision"],
                "slug": exp["metadata"]["slug"], "markdownSha256": pr.sha256(exp["markdown"].encode()),
                "frontMatter": "---\ntitle: Article\n---\n", "jsonLd": [{"@type": "Article", "headline": exp["metadata"]["title"]}],
                "images": [], "wiring": "Connect the Markdown directory."}
        self.url = f"/api/v1/my-startup/runs/{self.run.run_id}/article/reviewed-pr/?company_id={self.startup.company.pk}"
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.source, self.head, self.merge = "a"*40, "b"*40, "c"*40
        self.remote = self.enterContext(patch("content_factory.article_review_views.remote_review", return_value=self.review))
        self.enterContext(patch.object(pr, "verify_repository_native_target"))
        self.git_head = self.enterContext(patch.object(pr, "read_head", return_value=self.source))
        self.transport = self.enterContext(patch.object(worker.http_client, "post", side_effect=self.deliver))
        self.detail = b""
        self.branch = ""

    def offer(self):
        result = self.client.get(self.url)
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()

    def accept(self, offer=None):
        value = offer or self.offer()
        self.assertTrue(value.get("available"), value)
        return self.client.post(self.url, {k:value[k] for k in ("deliveryKind", "articleSha256", "reviewedRevision", "offerId")}, format="json")

    def deliver(self, url, *, json, **kwargs):
        self.assertTrue(url.endswith(f"/api/runs/{self.run.run_id}/reviewed-article-pr"))
        op = WebsiteConnectionOperation.objects.get(pk=json["operation_id"])
        private = "/api/content-factory/reviewed-article-pr/authorize"
        self.assertEqual(APIClient().post(private, json, format="json", HTTP_X_API_KEY="fixture-service").status_code, 200)
        self.assertEqual(APIClient().post(private, json, format="json").status_code, 403)
        self.branch = pr.branch_name(op)
        files = ["mlai-content-files/how-to-start/index.md", "mlai-content-files/how-to-start/article.jsonld",
                 "mlai-content-files/how-to-start/WIRING.md"]
        extra = {}
        if json["delivery_kind"] == "static_dropin":
            files = ["stories/index.html", "stories/index.json", "stories/how-to-start/index.html"]
            extra = {"target_id": "static-stories", "artifact_digest": self.marker, "route_path": "/stories/how-to-start/",
                     "rendered_article_sha256": pr.sha256(self.detail), "directory_path": "/stories/",
                     "contract_digest": "f"*64, "source_sha": self.source}
        return SimpleNamespace(status_code=200, json=lambda: {**json, "paths": files, "pr_url": "https://github.com/example/blog/pull/7",
            "pr_number": 7, "head_sha": self.head, "branch_name": self.branch, "counts_as_published": False,
            "consumes_free_article": False, "draft": True, **extra})

    def test_files_only_creates_one_draft_with_no_article_charge_or_publication(self):
        before = self.run.result
        value = self.offer()
        self.assertEqual(value["repository"], self.site.github_repo)
        self.assertEqual(value["deliveryKind"], "content_files")
        self.assertFalse(self.site.operations.exists())
        self.git_head.assert_not_called()
        with patch("content_factory.website_wakeups.submit_operation") as submit:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.accept(value)
                self.assertEqual(first.status_code, 200, first.content)
                self.assertEqual(first.json()["status"], "pending")
                self.assertEqual(self.offer(), first.json())
                submit.assert_not_called()
                self.remote.reset_mock()
                self.git_head.reset_mock()
                self.assertEqual(self.accept(value).json(), first.json())
                self.remote.assert_not_called()
                self.git_head.assert_not_called()
                self.transport.assert_not_called()
            op = self.site.operations.get(action=pr.ACTION)
            submit.assert_called_once_with(worker.advance, op.pk)
        worker.advance(op.pk)
        self.assertEqual(self.offer()["status"], "draft_pr_open")
        self.assertEqual(self.accept(value).json(), self.offer())
        self.transport.assert_called_once()
        self.assertFalse(WrittenArticle.objects.exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.result, before)
        self.assertFalse(self.site.operations.filter(action="deployment-verify").exists())

    def test_fast_merge_before_ack_does_not_lose_the_scoped_pr_receipt(self):
        def merge_before_ack(url, **kwargs):
            response = self.deliver(url, **kwargs)
            self.git_head.return_value = self.merge
            return response
        self.transport.side_effect = merge_before_ack
        with patch("content_factory.failure_policy_engine.record_pull_request") as count:
            self.assertEqual(self.accept().json()["status"], "pending")
            worker.advance(self.site.operations.get(action=pr.ACTION).pk)
            self.assertEqual(self.offer()["status"], "draft_pr_open")
        self.assertEqual(count.call_count, 1)

    def test_authenticated_manifest_certifies_older_missing_empty_findings_only(self):
        del self.review["reviewFindings"]
        self.assertTrue(self.offer()["available"])
        self.review["reviewFindings"] = "unreadable"
        self.assertFalse(self.offer()["available"])
        self.review["reviewFindings"] = [{"kind":"claim"}]
        self.assertFalse(self.offer()["available"])

    def test_malformed_payloads_return_a_bounded_error(self):
        self.assertEqual(self.client.post(self.url, ["not an object"], format="json").status_code, 400)
        self.assertEqual(APIClient().post("/api/content-factory/reviewed-article-pr/authorize",
            ["not an object"], format="json", HTTP_X_API_KEY="fixture-service").status_code, 400)
        self.transport.assert_not_called()

    def test_missing_approval_claims_and_old_destination_cannot_dispatch(self):
        offer = self.offer()
        self.review["draftReviewFindings"] = [{"kind": "claim"}]
        self.assertFalse(self.offer()["available"])
        self.review.pop("draftReviewFindings")
        self.review["approved"] = False
        self.assertEqual(self.accept(offer).status_code, 409)
        self.review["approved"] = True
        self.site.generation += 1
        self.site.save()
        self.assertEqual(self.accept(offer).status_code, 409)
        self.transport.assert_not_called()

    def test_changed_manifest_and_revocation_fence_credentials_and_generic_operation(self):
        self.transport.side_effect = TimeoutError()
        response = self.accept()
        self.assertEqual(response.json()["status"], "pending")
        op = self.site.operations.get(action=pr.ACTION)
        wire = op.payload["wire"]
        with self.assertRaises(WebsiteAuthorityError):
            validate_operation(self.site, {**wire, "run_id": str(op.pk), "action": "publish"})
        with self.assertRaises(pr.Unavailable):
            with pr.authority({**wire, "article_sha256": "7"*64}):
                pass
        self.site.state = "disconnected"
        self.site.save()
        with patch("integrations.services.github_app.create_installation_access_token") as token, self.assertRaises(pr.Unavailable):
            worker.credential(wire)
        token.assert_not_called()

    def test_retry_from_sweep_reuses_identical_wire_and_no_duplicate_receipt(self):
        self.transport.side_effect = TimeoutError()
        pending = self.accept().json()
        self.assertEqual(pending["status"], "pending")
        op = self.site.operations.get(action=pr.ACTION)
        self.transport.assert_not_called()
        worker.advance(op.pk)
        wire = self.transport.call_args.kwargs["json"]
        self.transport.side_effect = self.deliver
        worker.advance(op.pk, now=timezone.now()+timedelta(hours=1))
        self.assertEqual(self.transport.call_args.kwargs["json"], wire)
        self.assertEqual(self.offer()["status"], "draft_pr_open")
        op.refresh_from_db()
        self.assertEqual(op.state, "completed")

    @override_settings(ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=False)
    def test_unavailable_executor_leaves_work_for_the_periodic_reconciler(self):
        from .website_reconciliation import process_website_connection_operations
        with patch("content_factory.website_wakeups._POOL.submit", side_effect=RuntimeError("shutting down")) as rejected:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.accept().json()["status"], "pending")
            rejected.assert_called_once()
        self.transport.assert_not_called()
        process_website_connection_operations(connection_id=self.site.pk)
        self.assertEqual(self.offer()["status"], "draft_pr_open")
        process_website_connection_operations(connection_id=self.site.pk)
        self.transport.assert_called_once()

    def test_service_ledger_accepts_only_exact_branch_and_owned_chain_across_files(self):
        self.transport.side_effect = TimeoutError()
        self.accept()
        op = self.site.operations.get(action=pr.ACTION)
        wire = op.payload["wire"]
        scope = {k:v for k,v in wire.items() if k in ("website_connection_id","connection_generation","connection_target_id",
                "repository_id","github_repo","domain","expected_source_sha","operation_id","operation_attempt","deletion_epoch")}
        body = {**scope, "run_id":str(op.pk),"base_sha":self.source,"branch":pr.branch_name(op),"files":[]}
        def write(**changes):
            return APIClient().post("/api/content-factory/connections/mutations", {**body, **changes}, format="json",
                                    HTTP_X_API_KEY="fixture-service")
        self.assertEqual(write(mutation_id="ref",status="proposed").status_code,201)
        self.assertEqual(write(mutation_id="ref",status="applied",head_sha=self.source).status_code,200)
        path = {"path":"mlai-content-files/how-to-start/index.md","kind":"user_content","ownership":"created",
                "after_sha256":"8"*64}
        self.assertEqual(write(mutation_id="file",status="applied",head_sha=self.head,files=[path]).status_code,201)
        self.assertEqual(write(mutation_id="pull",status="proposed",base_sha=self.head).status_code,201)
        self.assertEqual(write(mutation_id="pull",status="applied",base_sha=self.head,
                               pr_url="https://github.com/example/blog/pull/7").status_code,200)
        for changes in ({"base_sha":"f"*40},{"branch":"main"},{"files":[{**path,"path":".github/workflows/write.yml"}]},
                {"run_id":self.run.run_id},{"files":[{**path,"kind":"setup_scaffolding"}]},
                {"pr_url":"https://github.com/another/repository/pull/7"}):
            with self.subTest(change=changes):
                self.assertEqual(write(mutation_id="wrong",status="proposed",**changes).status_code,409)
        self.assertEqual(self.site.repository_mutations.count(), 3)

    def test_rechecks_approval_after_token_mint_and_current_provider_grant(self):
        self.transport.side_effect = TimeoutError()
        self.accept()
        op = self.site.operations.get(action=pr.ACTION)
        wire = op.payload["wire"]
        minted = SimpleNamespace(token="fixture-only")
        def revoke(**kwargs):
            self.site.state = "disconnected"
            self.site.save(update_fields=["state"])
            return minted
        with patch("integrations.services.github_app.create_installation_access_token", side_effect=revoke) as mint, \
                patch.object(worker.http_client, "delete") as discard, self.assertRaises(pr.Unavailable):
            worker.credential(wire)
        self.assertEqual(mint.call_args.kwargs, {"installation_id":"9", "repository":"example/blog", "repository_id":72,
                                                "permission_mode":"write", "use_cache":False})
        discard.assert_called_once()

    def test_stale_approved_source_and_feature_off_make_no_git_requests(self):
        self.run.approval_state = "denied"
        self.run.save()
        self.assertFalse(self.offer()["available"])
        self.run.approval_state = "approved"
        self.run.run_request = {**self.run.run_request, "website_connection_id": str(self.site.pk),
                              "connection_generation": self.site.generation + 1, "repository_id": self.site.repository_id}
        self.run.save()
        self.assertFalse(self.offer()["available"])
        with override_settings(ARTICLES_SETUP_LADDER_ENABLED=False):
            self.assertEqual(self.client.get(self.url).status_code,404)
        self.git_head.assert_not_called()
        self.transport.assert_not_called()



    def static(self):
        self.marker = "e"*64
        marker = f'<meta name="mlai-artifact-digest" content="{self.marker}">'.encode()
        self.detail = marker + b"<p>Exact reviewed article</p>"
        self.directory = marker + b'<a href="/stories/how-to-start/">How to start</a>'
        self.candidate = {"target_id": "static-stories", "delivery_adapter": "static_dropin_v1", "provisional": True,
                "source":"scaffold", "live_marker": {"kind":"artifact_digest","value":self.marker},
                "registration_strategy": {"recipe": {"kind": "static_dropin_v1", "static_root":"", "route_base":"/stories/"}},
                "contract_digest":"f"*64, "verification": {"status": "awaiting_live_proof", "source_sha":self.source,
                   "static_setup_merge":{"merge_sha":self.source,"setup_run_id":self.setup.run_id}}}
        self.cfg.default_publish_target_id, self.cfg.publish_targets = "static-stories", [self.candidate]
        self.cfg.save()
        self.target = self.site.targets.create(target_key="static-stories", generation=1,
                adapter="static_dropin_v1", contract=self.candidate, source_sha=self.source)
        self.bad_detail = False
        self.missing_status = 404
        self.merged = True

    def provider(self, site, path):
        repo = {"full_name": site.github_repo, "id":site.repository_id}
        if path == "pulls/7":
            return {"number":7, "merged":self.merged, "state":"closed" if self.merged else "open", "merge_commit_sha":self.merge,
                    "base":{"ref":site.branch,"repo":repo}, "head":{"sha":self.head,"repo":repo,"ref":self.branch}}
        name = path.removeprefix("contents/").split("?")[0]
        raw = self.detail if name == "stories/how-to-start/index.html" else self.directory
        return {"path":name, "size":len(raw), "type":"file", "encoding":"base64", "content":base64.b64encode(raw).decode(),
                "sha":hashlib.sha1(f"blob {len(raw)}\0".encode()+raw).hexdigest()}

    def live(self, url, domain, *, expected_status=200):
        self.assertEqual(domain, self.org.domain)
        if "mlai-missing-" in url:
            if self.missing_status != expected_status:
                raise WebsiteAuthorityError("live_route_unavailable", "No negative route.")
            return b"missing", {}
        return (b"old detail" if self.bad_detail else self.detail, {}) if "how-to-start" in url else (self.directory, {})

    def test_changed_source_review_after_merge_cannot_activate_a_static_target(self):
        self.static()
        self.assertEqual(self.accept().json()["status"], "pending")
        worker.advance(self.site.operations.get(action=pr.ACTION).pk)
        self.assertEqual(self.offer()["status"], "draft_pr_open")
        op = self.site.operations.get(action=pr.ACTION)
        self.git_head.return_value = self.merge
        self.review["approved"] = False
        with patch.object(verify, "_provider", side_effect=self.provider), patch.object(verify, "commit_tree_sha", return_value="4"*40), \
                patch.object(verify, "fetch_live_route", side_effect=self.live):
            worker.advance(op.pk, now=timezone.now()+timedelta(hours=1))
        self.assertEqual(self.offer()["status"], "needs_attention")
        self.site.refresh_from_db()
        self.assertFalse(self.site.capabilities.get("publishingReady"))
        self.assertFalse(WrittenArticle.objects.exists())

    def test_empty_static_opens_then_certifies_only_merged_source_and_exact_live_files(self):
        self.static()
        self.assertEqual(self.offer()["directoryRoute"], "/stories/")
        self.assertEqual(self.accept().json()["status"], "pending")
        worker.advance(self.site.operations.get(action=pr.ACTION).pk)
        self.assertEqual(self.offer()["status"], "draft_pr_open")
        self.assertFalse(self.site.capabilities.get("publishingReady"))
        op = self.site.operations.get(action=pr.ACTION)
        self.git_head.return_value = self.merge
        with patch.object(verify, "_provider", side_effect=self.provider), patch.object(verify, "commit_tree_sha", return_value="4"*40), \
                patch.object(verify, "fetch_live_route", side_effect=self.live):
            self.merged = False
            worker.advance(op.pk, now=timezone.now()+timedelta(hours=1))
            self.assertEqual(self.offer()["status"], "draft_pr_open")
            self.merged, self.bad_detail = True, True
            worker.advance(op.pk, now=timezone.now()+timedelta(hours=2))
            self.assertEqual(self.offer()["status"], "merged_awaiting_verification")
            self.assertFalse(WrittenArticle.objects.exists())
            self.bad_detail, self.missing_status = False, 200
            worker.advance(op.pk, now=timezone.now()+timedelta(hours=3))
            self.assertFalse(WrittenArticle.objects.exists())
            self.missing_status = 404
            worker.advance(op.pk, now=timezone.now()+timedelta(hours=4))
        self.assertEqual(self.offer()["status"], "verified_live")
        self.site.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual(WrittenArticle.objects.get().source_run_id, self.run.run_id)
        self.assertEqual(self.site.verified_sha, self.merge)
        self.assertTrue(self.site.capabilities["publishingReady"])
        self.assertTrue(has_static_deployment_proof(self.site,self.target))
        self.assertTrue(live_deployment_verified(self.site,self.target,now=timezone.now()+timedelta(hours=4)))
        from .website_connections import authority_guard
        # The regular publication route consumes the independently proven target.
        with patch("content_factory.website_connections.verify_repository_native_target"), \
                patch("content_factory.website_connections.verify_repository_head"), \
                patch("content_factory.activation.timezone.now", return_value=timezone.now()+timedelta(hours=4)):
            with authority_guard({**pr.contract_for(self.site), "connection_target_id": self.target.target_key,
                    "expected_source_sha": self.merge}, action="publish") as actual:
                self.assertEqual(actual.pk, self.site.pk)
        self.assertNotIn("provisional", self.target.contract)
        self.assertFalse(self.site.operations.get(action="deployment-verify").receipt["provider_verified"])
