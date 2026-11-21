"""Setup-merge transitions walked together (founder-proof plan, workstream 6).

BE #1348, #1355, #1360 and #1373 each fixed one way a merged articles setup
fell out of Ready or started a second setup run after the merge. Their tests
cover each fix alone. This file walks the merge outcomes side by side on the
same merged, Ready site: the setup files identical after the push, other
files changed, more than 300 files changed, and a second setup run already
open. Each must end Ready, with the proof carried and no second setup run.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from workflow_runs.models import ContentFactoryRun

from . import website_source_carry as carry
from .tests_live_url_verification import MERGED
from .tests_website_source_carry import PUSHED, SETUP, CarriedSite, _compare, _truncated
from .website_connections import settled_setup_run


@override_settings(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True)
class SetupMergeTransitionTests(CarriedSite, TestCase):
    def setup_runs(self):
        return ContentFactoryRun.objects.filter(organization_id=self.connection.organization_id,
                                                workflow="article_system_setup")

    def walk(self, comparison):
        before = set(self.setup_runs().values_list("run_id", flat=True))
        with patch("content_factory.website_prepare.wake_prepare_for_source") as wake:
            result, _compare_call = self.push(comparison)
        self.assertEqual(result.get("carried"), 1, result)
        # Ready at the pushed commit on the carried build proof.
        self.assertEqual(self.connection.verified_sha, PUSHED)
        self.assertEqual(self.target.capabilities.get("proofCarriedFrom"), MERGED)
        self.assertTrue(self.connection.capabilities["publishingReady"])
        self.assertFalse([item for item in self.connection.blockers if item.get("code") == "repository_source_changed"])
        evidence = self.evidence()
        self.assertTrue(evidence["verified"], evidence)
        self.assertTrue(evidence["publishingVerified"], evidence)
        # The free source re-check needs no scan, and nothing asks for setup again.
        self.assertFalse(self.operation().payload["scan_required"])
        for call in wake.call_args_list:
            self.assertTrue(call.kwargs.get("carried"), call)
        self.assertEqual(set(self.setup_runs().values_list("run_id", flat=True)), before)

    def test_merged_with_identical_setup_files(self):
        self.walk(_compare(status="identical"))

    def test_merged_with_other_files_changed(self):
        self.walk(_compare("src/app/page.tsx", "README.md", "public/logo.svg"))

    def test_merged_after_more_than_300_files_changed(self):
        blobs = {path: "c" * 40 for path in carry.setup_paths(SETUP)}
        comparison = _truncated(blobs, dict(blobs))
        self.assertGreaterEqual(len(comparison["files"]), carry.COMPARE_FILE_LIMIT)
        self.walk(comparison)

    def test_a_second_setup_run_already_open(self):
        request = {"website_connection_id": str(self.connection.pk),
                   "connection_generation": self.connection.generation, "github_repo": self.connection.github_repo}
        merged = ContentFactoryRun.objects.create(
            run_id="setup-merged", organization=self.connection.organization, domain=self.connection.organization.domain,
            workflow="article_system_setup", status="completed", github_repo=self.connection.github_repo,
            run_request=request, result={"merge_status": "merged", "merge_sha": MERGED,
                                         "pr_url": "https://github.com/banksia/website/pull/38",
                                         "adoption": {"kit_paths": [".github/workflows/mlai-articles.yml"]}})
        ContentFactoryRun.objects.create(
            run_id="setup-second-open", organization=self.connection.organization,
            domain=self.connection.organization.domain, workflow="article_system_setup", status="awaiting_approval",
            github_repo=self.connection.github_repo, run_request=request,
            result={"pr_url": "https://github.com/banksia/website/pull/39"})
        self.walk(_compare("src/app/page.tsx"))
        # The merged setup still settles the merged head, so the journey's next
        # step is never another setup run.
        settled = settled_setup_run(self.connection, source_sha=MERGED)
        self.assertIsNotNone(settled)
        self.assertEqual(settled.pk, merged.pk)
