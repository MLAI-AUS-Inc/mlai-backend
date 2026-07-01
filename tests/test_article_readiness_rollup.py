"""compute_article_readiness: the single source of truth for article-generation readiness.

It collapses the three previously-duplicated OR chains (the readiness primitive, the setup
gate, and _profile_checks' history fallback) into one function that returns the SAME boolean
plus an explicit blocking_reason / reason_code so the wizard can tell the user which step is
missing instead of a generic "setup required". These lock the boolean semantics (so the
#484/#486/#490 tuning is preserved) and the reason classification.
"""
from django.test import TestCase

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

    # --- ready paths -----------------------------------------------------------------
    def test_scaffolded_is_ready_via_scaffolded(self):
        result = compute_article_readiness(self.org, self._connected(articles_scaffolded=True), [])
        self.assertTrue(result["generation_ready"])
        self.assertIsNone(result["blocking_reason"])
        self.assertEqual(result["reason_code"], "")
        self.assertEqual(result["via"], "scaffolded")
        self.assertTrue(result["proofs"]["articles_scaffolded"])

    def test_written_article_history_makes_ready(self):
        WrittenArticle.objects.create(
            organization=self.org,
            title="Existing article",
            slug="existing-article",
            category="guide",
            primary_keyword="existing article",
        )
        result = compute_article_readiness(self.org, self._connected(), [])
        self.assertTrue(result["generation_ready"])
        self.assertEqual(result["via"], "history")
        self.assertTrue(result["proofs"]["generation_history"])

    # --- blocked paths, classified in wizard order -----------------------------------
    def test_github_required_when_not_connected(self):
        result = compute_article_readiness(self.org, self._config(github_repo=""), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "github_required")
        self.assertTrue(result["blocking_reason"])

    def test_scan_required_when_connected_without_scan(self):
        result = compute_article_readiness(self.org, self._connected(), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "scan_required")

    def test_articles_location_required_when_scanned_but_no_surface(self):
        # Scan ran (scan_summary present) but no publish target / ready article system was found.
        result = compute_article_readiness(self.org, self._connected(scan_summary="repo scanned"), [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "articles_location_required")

    def test_passing_setup_gate_is_reused_not_recomputed(self):
        # When a caller already has the gate, the rollup honors it (no divergent recompute).
        config = self._connected()
        gate = {"generationReady": True, "published": False, "setupMerged": False, "setupBlocked": False}
        result = compute_article_readiness(self.org, config, [], setup_gate=gate)
        self.assertTrue(result["generation_ready"])
        self.assertIsNone(result["blocking_reason"])
