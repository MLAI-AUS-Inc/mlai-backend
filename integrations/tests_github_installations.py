from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from content_factory.models import OrganizationContentConfig
from integrations.models import GitHubInstallation
from integrations.services import github_installations as gh
from organizations.models import Organization

User = get_user_model()

REPOS_HB = [
    {"full_name": "HB-Vastu/internash-ai-builder", "name": "internash-ai-builder",
     "owner": {"login": "HB-Vastu", "type": "User"}, "private": True, "default_branch": "main"},
    {"full_name": "HB-Vastu/studynash", "name": "studynash",
     "owner": {"login": "HB-Vastu", "type": "User"}, "private": True, "default_branch": "main"},
    {"full_name": "HB-Vastu/coworkadelaide", "name": "coworkadelaide",
     "owner": {"login": "HB-Vastu", "type": "User"}, "private": True, "default_branch": "main"},
]
REPOS_ORG = [
    {"full_name": "acme-inc/site", "name": "site",
     "owner": {"login": "acme-inc", "type": "Organization"}, "private": True, "default_branch": "main"},
]


def _fake_list_repos(installation_id):
    return {"144909617": REPOS_HB, "140000000": REPOS_ORG}.get(str(installation_id), [])


class _FakeInstToken:
    def __init__(self, token):
        self.token = token


class GitHubInstallationRegistryTests(TestCase):
    def setUp(self):
        self.founder = User.objects.create_user(email="founder@studynash.co", slack_id="U_FOUNDER")
        self.other = User.objects.create_user(email="other@example.com", slack_id="U_OTHER")

    def test_resolve_user_by_synthetic_and_slack(self):
        self.assertEqual(gh.resolve_user_for_actor_id(f"mlai_user:{self.founder.id}"), self.founder)
        self.assertEqual(gh.resolve_user_for_actor_id("U_FOUNDER"), self.founder)
        self.assertIsNone(gh.resolve_user_for_actor_id("U_NOPE"))
        self.assertIsNone(gh.resolve_user_for_actor_id(""))
        self.assertIsNone(gh.resolve_user_for_actor_id("mlai_user:not-an-int"))

    def test_upsert_creates_then_preserves_token_on_identity_update(self):
        inst = gh.upsert_github_installation(
            user=self.founder, installation_id="144909617",
            account_login="HB-Vastu", github_user_name="HB-Vastu",
            user_token="tok-1", refresh_token="ref-1",
        )
        self.assertIsNotNone(inst)
        self.assertEqual(inst.github_user_token_encrypted, "tok-1")

        # Identity-only re-auth (no token supplied) must NOT wipe the token.
        inst2 = gh.upsert_github_installation(
            user=self.founder, installation_id="144909617", account_login="HB-Vastu",
        )
        self.assertEqual(inst2.pk, inst.pk)
        self.assertEqual(inst2.github_user_token_encrypted, "tok-1")
        self.assertEqual(GitHubInstallation.objects.filter(user=self.founder).count(), 1)

    def test_upsert_noop_without_user_or_installation(self):
        self.assertIsNone(gh.upsert_github_installation(user=None, installation_id="1"))
        self.assertIsNone(gh.upsert_github_installation(user=self.founder, installation_id=""))
        self.assertEqual(GitHubInstallation.objects.count(), 0)

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    @mock.patch.object(gh, "list_installation_repositories_via_app", side_effect=_fake_list_repos)
    def test_list_user_repos_unions_across_installations(self, _m_list, _m_cfg):
        gh.upsert_github_installation(user=self.founder, installation_id="144909617", account_login="HB-Vastu")
        gh.upsert_github_installation(user=self.founder, installation_id="140000000", account_login="acme-inc")
        repos = gh.list_user_repos(self.founder)
        names = sorted(r["fullName"] for r in repos)
        self.assertEqual(names, [
            "HB-Vastu/coworkadelaide", "HB-Vastu/internash-ai-builder",
            "HB-Vastu/studynash", "acme-inc/site",
        ])
        by_name = {r["fullName"]: r for r in repos}
        self.assertEqual(by_name["acme-inc/site"]["installationId"], "140000000")
        self.assertEqual(by_name["HB-Vastu/studynash"]["accountLogin"], "HB-Vastu")

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    @mock.patch.object(gh, "list_installation_repositories_via_app", side_effect=_fake_list_repos)
    def test_cross_founder_isolation(self, _m_list, _m_cfg):
        gh.upsert_github_installation(user=self.founder, installation_id="144909617", account_login="HB-Vastu")
        self.assertEqual(gh.list_user_repos(self.other), [])
        self.assertEqual(gh.user_github_installations(self.other), [])

    @mock.patch.object(gh, "github_app_credentials_configured", return_value=True)
    @mock.patch.object(gh, "list_installation_repositories_via_app", side_effect=_fake_list_repos)
    def test_installation_for_repo_owner_disambiguation(self, _m_list, _m_cfg):
        gh.upsert_github_installation(user=self.founder, installation_id="144909617", account_login="HB-Vastu")
        gh.upsert_github_installation(user=self.founder, installation_id="140000000", account_login="acme-inc")
        self.assertEqual(
            gh.installation_for_repo(self.founder, "acme-inc/site").installation_id, "140000000")
        self.assertEqual(
            gh.installation_for_repo(self.founder, "HB-Vastu/coworkadelaide").installation_id, "144909617")

    def test_installation_for_repo_single_shortcut_and_empty(self):
        # Empty registry -> None
        self.assertIsNone(gh.installation_for_repo(self.founder, "x/y"))
        # Single installation -> returned without any API listing (no mock needed)
        gh.upsert_github_installation(user=self.founder, installation_id="144909617", account_login="HB-Vastu")
        self.assertEqual(
            gh.installation_for_repo(self.founder, "HB-Vastu/studynash").installation_id, "144909617")

    @mock.patch.object(gh, "create_installation_access_token", return_value=_FakeInstToken("ghs_minted"))
    def test_mint_user_repo_token_uses_resolved_installation(self, m_create):
        gh.upsert_github_installation(user=self.founder, installation_id="144909617", account_login="HB-Vastu")
        token = gh.mint_user_repo_token(self.founder, "HB-Vastu/studynash")
        self.assertEqual(token.token, "ghs_minted")
        kwargs = m_create.call_args.kwargs
        self.assertEqual(kwargs["installation_id"], "144909617")
        self.assertEqual(kwargs["repository"], "HB-Vastu/studynash")
        self.assertIsNotNone(GitHubInstallation.objects.get(user=self.founder).last_used_at)

    def test_mint_returns_none_when_no_installation(self):
        self.assertIsNone(gh.mint_user_repo_token(self.founder, "x/y"))


class GitHubRegistryCrossCompanyResolutionTests(TestCase):
    """The headline requirement: access granted while setting up company A is
    usable from company B without any per-company GitHub credential."""

    def setUp(self):
        self.founder = User.objects.create_user(email="founder@studynash.co", slack_id="U_FOUNDER")
        actor = f"mlai_user:{self.founder.id}"

        # Company A connected during setup -> per-org installation id present.
        self.org_a = Organization.objects.create(name="Internash", domain="internash.co")
        self.cfg_a = OrganizationContentConfig.objects.create(
            organization=self.org_a, connected_slack_user_id=actor,
            github_repo="HB-Vastu/internash-ai-builder", github_installation_id="144909617",
        )
        # Company B picked a shared-pool repo but has NO per-org token/installation.
        self.org_b = Organization.objects.create(name="Cowork", domain="coworkadelaide.com.au")
        self.cfg_b = OrganizationContentConfig.objects.create(
            organization=self.org_b, connected_slack_user_id=actor,
            github_repo="HB-Vastu/coworkadelaide",
        )
        # Founder registry holds the shared installation (from connecting company A).
        gh.upsert_github_installation(
            user=self.founder, installation_id="144909617",
            account_login="HB-Vastu", github_user_name="HB-Vastu",
        )

    def test_resolve_installation_for_company_b_from_registry(self):
        from content_factory.vibe_marketing_views import _resolve_installation_id_for_repo
        self.assertEqual(
            _resolve_installation_id_for_repo(self.cfg_b, "HB-Vastu/coworkadelaide"), "144909617")

    def test_resolve_installation_prefers_registry_over_stale_per_org_id(self):
        from content_factory.vibe_marketing_views import _resolve_installation_id_for_repo
        self.cfg_b.github_installation_id = "999-stale"
        self.cfg_b.save(update_fields=["github_installation_id"])
        self.assertEqual(
            _resolve_installation_id_for_repo(self.cfg_b, "HB-Vastu/coworkadelaide"), "144909617")

    def test_resolve_installation_falls_back_to_per_org_when_registry_empty(self):
        from content_factory.vibe_marketing_views import _resolve_installation_id_for_repo
        GitHubInstallation.objects.all().delete()  # empty registry
        self.assertEqual(
            _resolve_installation_id_for_repo(self.cfg_a, "HB-Vastu/internash-ai-builder"), "144909617")

    @mock.patch.object(gh, "create_installation_access_token", return_value=_FakeInstToken("ghs_minted"))
    def test_get_github_credentials_mints_from_registry_for_company_b(self, _m_create):
        from integrations.services.article_generation import get_github_credentials_for_domain
        creds = get_github_credentials_for_domain(
            "coworkadelaide.com.au", f"mlai_user:{self.founder.id}")
        self.assertEqual(creds["source"], "app_installation")
        self.assertEqual(creds["repo"], "HB-Vastu/coworkadelaide")
        self.assertEqual(creds["token"], "ghs_minted")


class GitHubInstallationBackfillMigrationTests(TestCase):
    """Exercise the 0022 data-migration backfill directly against ORM state."""

    def _run_backfill(self):
        import importlib
        from django.apps import apps as global_apps

        mod = importlib.import_module("integrations.migrations.0022_githubinstallation")
        mod.backfill_github_installations(global_apps, None)

    def test_backfill_dedupes_newest_token_wins(self):
        from datetime import timedelta
        from django.utils import timezone
        from integrations.models import UserIntegration

        founder = User.objects.create_user(email="f@x.co", slack_id="U_F")
        actor = f"mlai_user:{founder.id}"
        org = Organization.objects.create(name="Internash", domain="internash.co")
        older = timezone.now() + timedelta(hours=1)
        newer = timezone.now() + timedelta(hours=8)
        OrganizationContentConfig.objects.create(
            organization=org, connected_slack_user_id=actor,
            github_repo="HB-Vastu/internash-ai-builder", github_installation_id="144909617",
            github_token_encrypted="cfg-tok", github_token_expires_at=older, github_user_name="HB-Vastu",
        )
        UserIntegration.objects.create(
            slack_user_id=actor, github_installation_id="144909617",
            github_access_token="ui-tok", github_token_expires_at=newer, github_user_name="HB-Vastu",
        )
        self._run_backfill()
        rows = GitHubInstallation.objects.filter(user=founder, installation_id="144909617")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().github_user_token_encrypted, "ui-tok")

    def test_backfill_skips_unresolvable_owner(self):
        org = Organization.objects.create(name="Ghost", domain="ghost.co")
        OrganizationContentConfig.objects.create(
            organization=org, connected_slack_user_id="U_NOBODY",
            github_repo="x/y", github_installation_id="555", github_token_encrypted="t",
        )
        self._run_backfill()
        self.assertFalse(GitHubInstallation.objects.filter(installation_id="555").exists())

    def test_backfill_is_idempotent(self):
        founder = User.objects.create_user(email="f2@x.co", slack_id="U_F2")
        org = Organization.objects.create(name="Org", domain="org2.co")
        OrganizationContentConfig.objects.create(
            organization=org, connected_slack_user_id=f"mlai_user:{founder.id}",
            github_repo="a/b", github_installation_id="777", github_token_encrypted="t",
        )
        self._run_backfill()
        self._run_backfill()
        self.assertEqual(GitHubInstallation.objects.filter(installation_id="777").count(), 1)


class GitHubConnectRegistryReuseTests(TestCase):
    """Phase D: connect binds a repo from the founder's shared installations
    without a fresh OAuth (no auth_url => frontend proceeds)."""

    def setUp(self):
        self.founder = User.objects.create_user(email="founder@studynash.co", slack_id="U_FOUNDER")
        # Company B: no per-org GitHub credential of its own.
        self.org_b = Organization.objects.create(name="Cowork", domain="coworkadelaide.com.au")
        self.cfg_b = OrganizationContentConfig.objects.create(
            organization=self.org_b, connected_slack_user_id=f"mlai_user:{self.founder.id}",
        )
        # Founder already authorized this installation (e.g. setting up company A).
        gh.upsert_github_installation(
            user=self.founder, installation_id="144909617",
            account_login="HB-Vastu", github_user_name="HB-Vastu",
        )

    def test_bind_repo_from_registry_returns_connected_without_reauth(self):
        from content_factory.vibe_marketing_views import _connect_with_registry_installation
        resp = _connect_with_registry_installation(
            self.cfg_b, user=self.founder, requested_repo="HB-Vastu/coworkadelaide")
        self.assertIsNotNone(resp)
        self.assertEqual(resp["status"], "connected")
        self.assertEqual(resp["credential_source"], "user_registry")
        self.assertEqual(resp["installation_id"], "144909617")
        self.assertNotIn("auth_url", resp)  # frontend proceeds instead of re-authing
        self.cfg_b.refresh_from_db()
        self.assertEqual(self.cfg_b.github_repo, "HB-Vastu/coworkadelaide")
        self.assertEqual(self.cfg_b.github_installation_id, "144909617")
        self.assertEqual(self.cfg_b.github_user_name, "HB-Vastu")

    def test_bind_uses_existing_config_repo_when_none_requested(self):
        self.cfg_b.github_repo = "HB-Vastu/studynash"
        self.cfg_b.save(update_fields=["github_repo"])
        from content_factory.vibe_marketing_views import _connect_with_registry_installation
        resp = _connect_with_registry_installation(self.cfg_b, user=self.founder, requested_repo="")
        self.assertEqual(resp["status"], "connected")
        self.cfg_b.refresh_from_db()
        self.assertEqual(self.cfg_b.github_installation_id, "144909617")

    def test_no_installations_returns_none(self):
        GitHubInstallation.objects.all().delete()
        from content_factory.vibe_marketing_views import _connect_with_registry_installation
        self.assertIsNone(_connect_with_registry_installation(
            self.cfg_b, user=self.founder, requested_repo="HB-Vastu/coworkadelaide"))

    def test_no_repo_anywhere_returns_none(self):
        from content_factory.vibe_marketing_views import _connect_with_registry_installation
        self.assertIsNone(_connect_with_registry_installation(
            self.cfg_b, user=self.founder, requested_repo=""))
