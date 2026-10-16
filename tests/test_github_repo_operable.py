"""The generation/publish gates must accept the credential the platform actually uses.

Every repo operation (scan, scaffold, publish PR, live preview) authenticates through the
content-factory token service, which mints from the **GitHub App installation**
(``config.github_installation_id``) first and only falls back to the org's user OAuth token.
But the article-generation gate, review-capability, promote affordance, and setup-blocked
guard historically defined "GitHub ready" as *user-OAuth-token present*. Result (prod,
coworkadelaide org 108, 2026-07-09): a fully-scaffolded org running on an App installation —
the fleet norm, since all 20 repo-connected orgs have expired/absent user tokens — 409-ed at
generation with the misleading "Connect and verify the article repository location…" message,
even though the platform could mint a write token for the repo right then.

These tests retain App credential support while requiring current owner consent
and native integration proof before dispatch. The setup goal repairs integration
before the article endpoint can spend or publish; legacy scaffold labels alone
cannot grant that authority.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import (
    _article_repo_is_review_capable,
    _effective_article_delivery_mode,
    _github_repo_operable,
    _profile_checks,
    _run_can_promote_package,
    _setup_blocked_response_for_generation,
    compute_article_readiness,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from roo.models import PointsAccount
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus
from tests.website_fixtures import (
    WebsiteBoundAPIClient, bind_config_fixture, bind_verified_target_fixture,
    install_github_provider_fixture,
)


User = get_user_model()


class _QueuedResponse:
    """Minimal requests-like response for a queued content-factory dispatch."""

    status_code = 202
    text = ""

    def __init__(self, payload):
        self._payload = payload

    @property
    def content(self):
        return b"{}"

    def json(self):
        return self._payload


# The article-system shape coworkadelaide had after publishing its scaffold: a merged setup,
# roo_scaffolded state (a READY state), and a registered react_article_system publish target.
SCAFFOLDED_ARTICLE_SYSTEM = {
    "state": "roo_scaffolded",
    "source": "scaffold",
    "system_type": "react_article_system",
    "confidence": "high",
    "generationReady": True,
    "publish_mutation_target": "src/articles/registry.ts",
}
SCAFFOLDED_PUBLISH_TARGETS = [
    {"target_id": "react_article_system_src_articles", "kind": "react_article_system", "confidence": "high"}
]


class GithubRepoOperableUnitTests(TestCase):
    """`_github_repo_operable`: repo present AND (connected user token OR stamped App install)."""

    def setUp(self):
        self.org = Organization.objects.create(domain="acme.example", name="Acme")

    def _config(self, **kwargs):
        kwargs.setdefault("organization", self.org)
        kwargs.setdefault("github_repo", "acme/site")
        return OrganizationContentConfig.objects.create(**kwargs)

    def test_connected_user_token_is_operable(self):
        # A live user token + repo derives github_connection_state == "connected".
        config = self._config(github_token_encrypted="tok")
        self.assertEqual(config.github_connection_state, "connected")
        self.assertTrue(_github_repo_operable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_app_installation_without_token_is_operable(self, _creds):
        # No user token (auth_required) but a stamped installation + server app creds => operable.
        config = self._config(github_installation_id="144909617")
        self.assertEqual(config.github_connection_state, "auth_required")
        self.assertTrue(_github_repo_operable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_expired_token_falls_back_to_installation(self, _creds):
        # The fleet norm: a stored-but-expired user token (=> auth_required) + a stamped install.
        config = self._config(
            github_token_encrypted="stale",
            github_token_expires_at=timezone.now() - timezone.timedelta(days=1),
            github_installation_id="144909617",
        )
        self.assertEqual(config.github_connection_state, "auth_required")
        self.assertTrue(_github_repo_operable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_no_token_and_no_installation_is_not_operable(self, _creds):
        config = self._config()
        self.assertFalse(_github_repo_operable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_expired_but_refreshable_token_without_installation_is_operable(self, _creds):
        # Legacy pre-App org: expired access token (=> auth_required) + a stored refresh
        # token + NO installation. ensure_valid_org_token still mints by refreshing, so the
        # gate must not block it (mint-parity with the token service's user fallback).
        config = self._config(
            github_token_encrypted="stale",
            github_refresh_token_encrypted="refresh",
            github_token_expires_at=timezone.now() - timezone.timedelta(days=1),
        )
        self.assertEqual(config.github_connection_state, "auth_required")
        self.assertTrue(_github_repo_operable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_expired_token_without_refresh_or_installation_is_not_operable(self, _creds):
        # Expired access token, NO refresh token, NO installation: ensure_valid_org_token
        # raises TokenRefreshError, so this is genuinely non-operable (and the OLD gate's
        # raw-github_token_encrypted acceptance was wrong to let it through to a mint failure).
        config = self._config(
            github_token_encrypted="stale",
            github_token_expires_at=timezone.now() - timezone.timedelta(days=1),
        )
        self.assertEqual(config.github_connection_state, "auth_required")
        self.assertFalse(_github_repo_operable(config))

    def test_no_repo_is_not_operable(self):
        config = self._config(github_repo="", github_installation_id="144909617")
        self.assertFalse(_github_repo_operable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=False)
    def test_installation_without_server_app_creds_is_not_operable(self, _creds):
        # Can't mint from an installation if the server has no GitHub App credentials.
        config = self._config(github_installation_id="144909617")
        self.assertFalse(_github_repo_operable(config))

    def test_none_config_is_not_operable(self):
        self.assertFalse(_github_repo_operable(None))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_review_capable_via_installation_and_publish_targets(self, _creds):
        config = self._config(
            github_installation_id="144909617",
            publish_targets=SCAFFOLDED_PUBLISH_TARGETS,
        )
        self.assertTrue(_article_repo_is_review_capable(config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_content_only_auto_upgrades_to_review_draft_via_installation(self, _creds):
        # The second, quieter bug: a content_only org that IS review-capable (surface + App
        # credential) must auto-upgrade to review_draft (exact-preview articles). The old
        # token-only check pinned it to content_only fleet-wide.
        config = self._config(
            github_installation_id="144909617",
            article_system=dict(SCAFFOLDED_ARTICLE_SYSTEM),
            publish_targets=SCAFFOLDED_PUBLISH_TARGETS,
            articles_scaffolded=True,
            article_delivery_mode="content_only",
        )
        self.assertEqual(_effective_article_delivery_mode(config), "review_draft")


class ReadinessAndProfileCheckOperabilityTests(TestCase):
    """The wizard-facing readiness classifier and the `github` profile check honor the
    App installation — a revert of either swap to a connection_state-only test must fail
    here (the pre-existing readiness suites use a connected token, which satisfies both
    the old and new definition, so they cannot catch such a revert)."""

    def setUp(self):
        self.org = Organization.objects.create(domain="readiness.example", name="Readiness")

    def _config(self, **kwargs):
        kwargs.setdefault("organization", self.org)
        kwargs.setdefault("github_repo", "readiness/site")
        return OrganizationContentConfig.objects.create(**kwargs)

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_profile_github_check_passes_on_installation_only(self, _creds):
        # No user token, just a stamped installation => the wizard's `github` check passes.
        config = self._config(github_installation_id="144909617", scan_summary="scanned")
        checks = _profile_checks(self.org, config, latest_runs=[])
        self.assertTrue(checks["github"]["passed"])

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_profile_github_check_fails_without_credential(self, _creds):
        config = self._config()  # repo set, no token, no installation
        checks = _profile_checks(self.org, config, latest_runs=[])
        self.assertFalse(checks["github"]["passed"])

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_readiness_not_github_blocked_on_installation_only(self, _creds):
        # App access plus current native proof is ready without user OAuth.
        config = self._config(
            github_installation_id="144909617",
            article_system=dict(SCAFFOLDED_ARTICLE_SYSTEM),
            publish_targets=SCAFFOLDED_PUBLISH_TARGETS,
            articles_scaffolded=True,
        )
        owner = User.objects.create_user(email='readiness-owner@example.test')
        bind_verified_target_fixture(config, user=owner)
        result = compute_article_readiness(self.org, config, [])
        self.assertTrue(result["generation_ready"])
        self.assertNotEqual(result["reason_code"], "github_required")

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_readiness_repo_set_but_no_credential_requires_connection(self, _creds):
        config = self._config()
        result = compute_article_readiness(self.org, config, [])
        self.assertFalse(result["generation_ready"])
        self.assertEqual(result["reason_code"], "github_required")
        self.assertIn("Connect GitHub", result["blocking_reason"])


class RunCanPromotePackageTests(TestCase):
    """The promote/publish affordance writes to the repo via the same credential."""

    def setUp(self):
        self.org = Organization.objects.create(domain="promote.example", name="Promote")

    def _config(self, **kwargs):
        kwargs.setdefault("organization", self.org)
        kwargs.setdefault("github_repo", "promote/site")
        return OrganizationContentConfig.objects.create(**kwargs)

    def _run(self):
        return ContentFactoryRun.objects.create(
            run_id="promote-run-1",
            workflow="vibe_marketing",
            domain="promote.example",
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"delivery_mode": "content_only"},
        )

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_promotable_via_installation(self, _creds):
        config = self._config(github_installation_id="144909617")
        self.assertTrue(_run_can_promote_package(self._run(), config=config))

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_not_promotable_without_credential(self, _creds):
        config = self._config()  # repo set, no token, no installation
        self.assertFalse(_run_can_promote_package(self._run(), config=config))


class SetupBlockedGuardOperabilityTests(TestCase):
    """Current consent and integration proof augment App credential support."""

    def setUp(self):
        self.org = Organization.objects.create(domain="guard.example", name="Guard")
        self.user = User.objects.create_user(email='guard-owner@example.test')
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.company = VibeRaisingCompany.objects.create(profile=self.profile,
            organization=self.org, name='Guard', domain=self.org.domain)
        self.context = SimpleNamespace(organization=self.org, profile=self.profile, company=self.company)

    def _config(self, **kwargs):
        kwargs.setdefault("organization", self.org)
        kwargs.setdefault("github_repo", "guard/site")
        return OrganizationContentConfig.objects.create(**kwargs)

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_non_operable_repo_requires_github_before_dispatch(self, _creds):
        config = self._config()  # repo set, no credential at all
        response = _setup_blocked_response_for_generation(self.context, config)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['reasonCode'], 'github_required')

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    def test_operable_but_unblocked_repo_returns_none(self, _creds):
        # A verified native target and current provider proof permit dispatch.
        config = self._config(
            github_installation_id="144909617",
            article_system=dict(SCAFFOLDED_ARTICLE_SYSTEM),
            publish_targets=SCAFFOLDED_PUBLISH_TARGETS,
            articles_scaffolded=True,
        )
        bind_verified_target_fixture(config, user=self.user)
        install_github_provider_fixture(self, config.website_connection)
        self.assertTrue(_github_repo_operable(config))
        self.assertIsNone(_setup_blocked_response_for_generation(self.context, config))


class ArticleGenerationGateEndpointTests(TestCase):
    """End-to-end: POST /api/v1/vibe-marketing/article/ honors the App installation."""

    ARTICLE_URL = "/api/v1/vibe-marketing/article/"

    def setUp(self):
        self.client = WebsiteBoundAPIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        self.org = Organization.objects.create(domain="coworkexample.com", name="Cowork Example")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Cowork Example",
            domain="coworkexample.com",
            registered=True,
            organization=self.org,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        PointsAccount.objects.update_or_create(
            user=self.user,
            defaults={"balance": 20, "earned_balance": 20},
        )
        self.client.force_authenticate(user=self.user)
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="HB-Vastu/coworkadelaide",
            baseline_skipped_at=timezone.now(),
        )

    def _make_scaffolded(self):
        # org-108's post-publish shape: App installation, no user token, ready surface.
        self.config.github_installation_id = "144909617"
        self.config.article_system = dict(SCAFFOLDED_ARTICLE_SYSTEM)
        self.config.publish_targets = SCAFFOLDED_PUBLISH_TARGETS
        self.config.articles_scaffolded = True
        self.config.article_delivery_mode = "content_only"
        self.config.save()
        bind_verified_target_fixture(self.config, user=self.user)
        self.client.website_fixture = self.config.website_connection
        install_github_provider_fixture(self, self.config.website_connection)

    @override_settings(
        CONTENT_FACTORY_URL="https://content-factory.test",
        CONTENT_FACTORY_API_KEY="secret-key",
        IS_LOCAL_ENV=False,
    )
    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    @patch("content_factory.vibe_marketing_views.http_client.post")
    def test_scaffolded_app_installation_org_does_not_409(self, post, _creds):
        """The shipped bug: scaffolded org on an App installation must not be told to
        'connect and verify the article repository location'."""
        post.return_value = _QueuedResponse({"run_id": "r1", "status": "queued"})
        self._make_scaffolded()

        response = self.client.post(
            self.ARTICLE_URL,
            {"topic": "How coworking boosts focus", "targetKeyword": "coworking focus"},
            format="json",
        )

        self.assertNotEqual(response.status_code, 409, response.data)
        self.assertNotIn("repo_articles_setup_not_trusted", str(response.data))
        # Reaching the content-factory dispatch proves it cleared the credential gate.
        self.assertTrue(post.called)

    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    @patch("content_factory.vibe_marketing_views.http_client.post")
    def test_no_credential_returns_github_auth_required(self, post, _creds):
        # Repo set, no token, no installation, but a ready surface => the failing leg is auth.
        self.config.article_system = dict(SCAFFOLDED_ARTICLE_SYSTEM)
        self.config.publish_targets = SCAFFOLDED_PUBLISH_TARGETS
        self.config.articles_scaffolded = True
        self.config.save()

        response = self.client.post(
            self.ARTICLE_URL,
            {"topic": "How coworking boosts focus", "targetKeyword": "coworking focus"},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data.get("code"), "website_connection_required")
        self.assertFalse(post.called)

    @override_settings(
        CONTENT_FACTORY_URL="https://content-factory.test",
        CONTENT_FACTORY_API_KEY="secret-key",
        IS_LOCAL_ENV=False,
    )
    @patch("content_factory.vibe_marketing_views.github_app_credentials_configured", return_value=True)
    @patch("content_factory.vibe_marketing_views.http_client.post")
    def test_operable_repo_without_surface_waits_for_verified_integration(self, post, _creds):
        # The owned setup goal must establish native proof before paid generation.
        post.return_value = _QueuedResponse(
            {
                "run_id": "cowork-repair-parent-1",
                "status": "precondition_failed",
                "repair_status": "queued",
                "repair_run_id": "cowork-repair-scan-1",
            }
        )
        self.config.github_installation_id = "144909617"
        self.config.article_system = {
            "state": "ambiguous",
            "directory_name": "articles",
            "directory_path": "src/articles",
            "publish_mutation_target": None,
        }
        self.config.article_system_setup_cache = {
            "schema_version": 1,
            "route_path": "/articles",
            "route_shell_path": "src/routes/articles.tsx",
        }
        self.config.publish_targets = []
        self.config.save()
        bind_config_fixture(self.config)
        self.config.website_connection.authorized_by = self.user
        self.config.website_connection.save(update_fields=['authorized_by'])
        self.client.website_fixture = self.config.website_connection
        install_github_provider_fixture(self, self.config.website_connection)

        # Discriminates from the credential-leg 409: the repo IS operable here, so the
        # block is specifically the article-surface leg (a revert of the operability swap
        # would compute github_ready False and take the github_auth_required branch instead).
        self.assertTrue(_github_repo_operable(self.config))

        response = self.client.post(
            self.ARTICLE_URL,
            {"topic": "How coworking boosts focus", "targetKeyword": "coworking focus"},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data.get('reasonCode'), 'integration_required')
        post.assert_not_called()
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 20)
        self.assertFalse(ContentFactoryRun.objects.filter(organization=self.org,
            workflow__in={'article_generation', 'direct_generate'}).exists())
