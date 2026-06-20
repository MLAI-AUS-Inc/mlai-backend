from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import (
    _article_repo_is_review_capable,
    _effective_article_delivery_mode,
)
from organizations.models import Organization


class ArticleRepoReviewCapableTest(TestCase):
    """Regression coverage for the article-generation readiness gate.

    `_article_repo_is_review_capable` used to test `article_system["state"]` against
    {"ready", "detected", "registry_driven_seo_ready", "article_system_ready"} -- none
    of which `resolve_article_system()` can ever return (it clamps to
    {"missing", "existing", "roo_scaffolded", "ambiguous"}). That made a scaffolded
    article system invisible to the gate, so `POST /api/v1/vibe-marketing/article`
    returned a 409 "Connect and verify the article repository location..." even when
    the repo was fully set up but had no cached publish_targets. The gate must honor
    the real ready states via `article_system_ready()`.
    """

    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")

    def _config(self, **overrides):
        defaults = dict(
            organization=self.organization,
            github_repo="MLAI-AUS-Inc/mlai-au",
            github_token_encrypted="gho_test_token",
            github_token_expires_at=timezone.now() + timedelta(days=1),
        )
        defaults.update(overrides)
        return OrganizationContentConfig.objects.create(**defaults)

    def test_scaffolded_system_without_publish_targets_is_review_capable(self):
        # The reported bug: articles scaffolded + GitHub connected, but no cached
        # publish_targets. resolve_article_system() returns "roo_scaffolded".
        config = self._config(articles_scaffolded=True, publish_targets=[])

        self.assertEqual(config.github_connection_state, "connected")
        self.assertTrue(_article_repo_is_review_capable(config))

    def test_existing_system_without_publish_targets_is_review_capable(self):
        config = self._config(
            publish_targets=[],
            article_system={
                "state": "existing",
                "confidence": "high",
                "directory_name": "articles",
                "reason": "Detected existing article system at app/articles",
            },
        )

        self.assertTrue(_article_repo_is_review_capable(config, github_ready=True))

    def test_publish_targets_alone_keep_review_capable(self):
        config = self._config(
            publish_targets=[{"kind": "registry_driven_seo", "id": "primary"}],
            article_system={},
        )

        self.assertTrue(_article_repo_is_review_capable(config, github_ready=True))

    def test_missing_system_without_publish_targets_is_not_review_capable(self):
        config = self._config(publish_targets=[], article_system={})

        self.assertFalse(_article_repo_is_review_capable(config, github_ready=True))

    def test_scaffolded_but_github_not_ready_is_not_review_capable(self):
        # Article side is ready, but the GitHub side still gates the preview.
        config = self._config(articles_scaffolded=True, publish_targets=[])

        self.assertFalse(_article_repo_is_review_capable(config, github_ready=False))

    def test_scaffolded_system_defaults_delivery_mode_to_review_draft(self):
        # Ties the gate to user-facing behavior: a review-capable repo upgrades a
        # legacy content_only default to review_draft (the intent of the commit that
        # introduced the bug, "preserve review draft backend defaults").
        config = self._config(
            articles_scaffolded=True,
            publish_targets=[],
            article_delivery_mode="content_only",
        )

        self.assertEqual(
            _effective_article_delivery_mode(config, github_ready=True),
            "review_draft",
        )
