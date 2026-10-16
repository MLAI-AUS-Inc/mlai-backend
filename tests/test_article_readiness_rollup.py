"""Readiness requires current integration proof, including for legacy configs."""
import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from content_factory.models import OrganizationContentConfig, WrittenArticle
from content_factory.vibe_marketing_views import compute_article_readiness
from organizations.models import Organization


class ComputeArticleReadinessTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="acme.example", name="Acme")

    def _config(self, **kwargs):
        kwargs.setdefault("organization", self.org)
        return OrganizationContentConfig.objects.create(**kwargs)

    def _connected(self, **kwargs):
        # github_connection_state is derived: a non-empty token + repo => "connected".
        kwargs.setdefault("github_token_encrypted", "tok")
        kwargs.setdefault("github_repo", "acme/site")
        return self._config(**kwargs)

    def _verified(self, **overrides):
        target = {"target_id": "articles", "kind": "react_article_system", "publish_capability": "direct"}
        scan = {
            "github_repo": "acme/site",
            "default_branch": "main",
            "default_branch_sha": "a" * 40,
            "scan_completed_at": timezone.now().isoformat(),
            "article_system_readiness": {"ready": True, "safe_publish_route": True},
            "publish_targets": [target],
        }
        scan.update(overrides)
        return self._connected(publish_targets=[target], scan_summary=json.dumps(scan))

    def test_scaffold_flag_does_not_grant_generation(self):
        result = compute_article_readiness(self.org, self._connected(articles_scaffolded=True), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "integration_required")
        self.assertFalse(result["proofs"]["integration_verified"])

    def test_written_article_history_does_not_grant_generation(self):
        WrittenArticle.objects.create(
            organization=self.org,
            title="Existing article",
            slug="existing-article",
            category="guide",
            primary_keyword="existing article",
        )
        result = compute_article_readiness(self.org, self._connected(), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["via"], "")
        self.assertFalse(result["proofs"]["integration_verified"])

    def test_github_required_when_not_connected(self):
        result = compute_article_readiness(self.org, self._config(github_repo=""), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "github_required")
        self.assertTrue(result["blocking_reason"])

    def test_integration_required_when_connected_without_scan(self):
        result = compute_article_readiness(self.org, self._connected(), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "integration_required")

    def test_integration_required_when_scan_has_no_surface(self):
        # Scan ran (scan_summary present) but no publish target / ready article system was found.
        result = compute_article_readiness(self.org, self._connected(scan_summary="repo scanned"), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "integration_required")

    def test_passing_setup_gate_does_not_replace_integration_proof(self):
        config = self._connected()
        gate = {"generationReady": True, "published": False, "setupMerged": False, "setupBlocked": False}
        result = compute_article_readiness(self.org, config, [], setup_gate=gate)
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "integration_required")

    def test_current_scoped_scan_grants_generation(self):
        result = compute_article_readiness(self.org, self._verified(), [])
        self.assertTrue(result["generation_ready"])
        self.assertEqual(result["via"], "verified_integration")
        self.assertTrue(result["proofs"]["integration_verified"])
        self.assertIsNone(result["blocking_reason"])

    def test_other_repository_scan_does_not_grant_generation(self):
        result = compute_article_readiness(self.org, self._verified(github_repo="other/site"), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "verification_required")

    def test_old_verification_does_not_grant_generation(self):
        config = self._verified(scan_completed_at=(timezone.now() - timedelta(days=8)).isoformat())
        result = compute_article_readiness(self.org, config, [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "verification_stale")
