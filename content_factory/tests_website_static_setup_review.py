"""An authenticated scan can locate a specific reviewed setup without gaining publication rights."""
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from . import website_static_setup_review as reviews
from .models import OrganizationContentConfig
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection


@override_settings(ARTICLES_SETUP_LADDER_ENABLED=True, ROO_API_KEY="fixture-service")
class StaticReviewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Fixture", domain="fixture.example.test")
        self.site = WebsiteConnection.objects.create(organization=self.org, repository_id=11, github_repo="fixture/site",
                                                    branch="main", installation_id="37")
        self.config = OrganizationContentConfig.objects.create(organization=self.org, website_connection=self.site,
                                                              github_repo=self.site.github_repo)
        self.scope = contract_for(self.site)
        self.source, self.head, self.merge = "1" * 40, "2" * 40, "3" * 40
        self.candidate = {"delivery_adapter": "static_dropin_v1", "target_id": "static-directory-test",
                          "registration_strategy": {"recipe": {"kind": "static_dropin_v1"}},
                          "contract_digest": "f" * 64, "verification": {"status": "awaiting_live_proof"}}
        result = {"pr_number": 9, "setup_ladder": {"tier": "static_dropin", "head_sha": self.head,
                  "source_sha": self.source, "candidate_publish_target": self.candidate}}
        self.run = ContentFactoryRun.objects.create(organization=self.org, github_repo=self.site.github_repo,
            run_id="setup-test", domain=self.org.domain, workflow="article_system_setup", status="completed",
            approval_state="approved", run_request={**self.scope, "expected_source_sha": self.source}, result=result)
        self.hint = {"setup_run_id": self.run.run_id, "pr_number": 9, "source_sha": self.source,
                     "reviewed_head_sha": self.head, "merge_sha": self.merge, "branch": "main"}
        repo = {"id": self.site.repository_id, "full_name": self.site.github_repo}
        self.pull = {"merged": True, "number": 9, "merge_commit_sha": self.merge,
                     "head": {"sha": self.head, "repo": repo}, "base": {"ref": "main", "repo": repo}}
        self.branch = self.enterContext(patch.object(reviews, "read_head", return_value=self.merge))
        self.remote = self.enterContext(patch.object(reviews, "read_bound_repository"))
        self.remote.return_value.json.return_value = self.pull
        self.trees = self.enterContext(patch.object(reviews, "commit_tree_sha", return_value="4" * 40))
        self.client = APIClient()
        self.query = {"domain": self.org.domain, "github_repo": self.site.github_repo, "static_setup_source_sha": self.merge}
        self.url = "/api/content-factory/org/config"

    def test_only_the_selected_service_read_can_receive_manual_merge_identity(self):
        self.assertIn(self.client.get(self.url, self.query).status_code, {401, 403})
        self.remote.assert_not_called()
        def get(data):
            return self.client.get(self.url, data, HTTP_X_API_KEY="fixture-service")
        body = get(self.query).data
        self.assertEqual(body["static_setup_review"], self.hint)
        self.assertFalse(self.site.operations.exists())
        self.assertFalse(self.site.targets.exists())
        self.assertFalse(body["website_connection"]["capabilities"].get("publishingReady"))
        for fields in ({"domain": self.org.domain}, {**self.query, "github_repo": "fixture/other"},
                       {**self.query, "static_setup_source_sha": self.source}, {**self.query, "domain": "other.test"}):
            self.assertNotIn("static_setup_review", get(fields).data)
        self.config.website_connection = None
        self.config.save(update_fields=("website_connection",))
        self.assertIsNone(reviews.current_review(self.site, self.merge))

    def test_unmerged_wrong_repo_wrong_tree_and_replaced_approval_are_refused(self):
        for patch_value in ({"merged": False}, {"number": 7},
                            {"base": {"ref": "main", "repo": {"id": 12, "full_name": self.site.github_repo}}}):
            self.remote.return_value.json.return_value = {**self.pull, **patch_value}
            self.assertIsNone(reviews.current_review(self.site, self.merge))
        self.remote.return_value.json.return_value = self.pull
        self.trees.side_effect = ["a" * 40, "b" * 40]
        self.assertIsNone(reviews.current_review(self.site, self.merge))
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(approval_state="pending")
        self.assertIsNone(reviews.current_review(self.site, self.merge))

    def _candidate_write(self):
        return {**self.candidate, "provisional": True, "verification": {"status": "awaiting_live_proof",
            "source_sha": self.merge, "static_setup_merge": {**self.hint, "tree_sha": "4"*40, "listing_sha256": "5"*64}}}

    def test_service_write_registers_only_a_provisional_target_at_current_head(self):
        target = self._candidate_write()
        command = {**self.scope, "default_publish_target_id": target["target_id"], "publish_targets": [target],
                   "source_sha": self.merge, "expected_source_sha": self.merge}
        def send():
            return self.client.put(self.url, command, format="json", HTTP_X_API_KEY="fixture-service")
        with patch("content_factory.website_connections.verify_repository_head",
                   side_effect=WebsiteAuthorityError("website_source_changed", "The source changed.")):
            self.assertEqual(send().status_code, 409)
        self.assertFalse(self.site.targets.exists())
        with patch("content_factory.website_connections.verify_repository_head") as head:
            response = send()
        self.assertIn(response.status_code, (200, 201), response.data)
        head.assert_called()
        saved = self.site.targets.get()
        self.assertFalse(saved.capabilities.get("publishingReady"))
        self.assertIsNone(saved.verified_at)
        self.site.refresh_from_db()
        self.assertFalse(self.site.capabilities.get("generationReady"))

    def test_build_certification_without_live_directory_proof_never_activates_static_publishing(self):
        from .website_connections import record_scan_evidence
        target = {**self.candidate, "publish_capability": "direct",
                  "verification": {"status": "verified", "source_sha": self.merge, "preview_capable": True}}
        with patch("content_factory.website_connections.verify_repository_head"), \
                patch("content_factory.website_connections.verify_repository_native_target"):
            record_scan_evidence(self.site, {"source_sha": self.merge, "publish_targets": [target]})
        self.assertIsNotNone(self.site.targets.get().verified_at)
        self.site.refresh_from_db()
        self.assertFalse(self.site.capabilities.get("publishingReady"))
        self.assertFalse(self.site.capabilities.get("generationReady"))

    def test_provisional_scan_cannot_replace_an_already_verified_selection(self):
        from django.utils import timezone
        self.site.targets.create(generation=self.site.generation, target_key="existing", verified_at=timezone.now(),
                                 source_sha=self.merge, contract={"target_id": "existing"})
        self.config.default_publish_target_id = "existing"
        self.config.save(update_fields=("default_publish_target_id",))
        target = self._candidate_write()
        body = {**self.scope, "source_sha": self.merge, "expected_source_sha": self.merge,
                "publish_targets": [target], "default_publish_target_id": target["target_id"]}
        with patch("content_factory.website_connections.verify_repository_head"):
            result = self.client.put(self.url, body, format="json", HTTP_X_API_KEY="fixture-service")
        self.assertEqual(result.status_code, 409)
        self.config.refresh_from_db()
        self.assertEqual(self.config.default_publish_target_id, "existing")
        self.assertFalse(self.site.targets.filter(target_key=target["target_id"]).exists())

    def test_matching_backend_merge_receipt_and_provisional_write_do_not_elevate_readiness(self):
        self.run.result.update(merge_status="merged", merge_response={"merged": True, "sha": self.merge},
            publish_merge_intent={**self.scope, "run_id": self.run.run_id, "pr_number": 9, "head_sha": self.head,
                                  "source_sha": self.source, "base_branch": "main"})
        self.run.save(update_fields=("result",))
        self.assertEqual(reviews.current_review(self.site, self.merge), self.hint)
        self.remote.assert_not_called()
        target = self._candidate_write()
        reviews.validate_provisional_target(self.site, target, self.merge, {"source_sha": self.merge})
        for modified, source in (({**target, "contract_digest": "7"*64}, self.merge),
                                 (target, self.source), ({**target, "verification": {"status": "verified"}}, self.merge)):
            with self.assertRaises(WebsiteAuthorityError):
                reviews.validate_provisional_target(self.site, modified, self.merge, {"source_sha": source})
        self.assertFalse(self.site.operations.exists())
        self.assertFalse(self.site.targets.exists())
