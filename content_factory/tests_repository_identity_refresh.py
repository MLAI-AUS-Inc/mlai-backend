"""A renamed, moved, archived or private repository keeps its website connection."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from integrations.models import GitHubInstallation
from workflow_runs.models import ContentFactoryRun
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import summary_for
from .website_models import WebsiteConnectionOperation
from .website_reconciliation import handle_website_github_event
from . import website_repository_identity as identity


def repository_event(action, *, full_name="example/site-renamed", repository_id=123, owner="example"):
    return {"action": action, "installation": {"id": 45},
            "repository": {"id": repository_id, "full_name": full_name, "owner": {"login": owner}}}


class RepositoryIdentityRefreshTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        self.founder = get_user_model().objects.create_user(email="identity@example.test")
        self.website.authorized_by = self.founder
        self.website.save(update_fields=["authorized_by"])
        self.generation = self.website.generation
        self.running = ContentFactoryRun.objects.create(run_id="in-flight", organization=self.org, domain=self.org.domain,
            github_repo="example/site", workflow="article_generation", status="running",
            run_request={"website_connection_id": str(self.website.pk), "connection_generation": self.generation})
        self.scan = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.generation,
            action="scan", state="pending", idempotency_key="identity-scan")

    def assert_setup_kept(self):
        self.website.refresh_from_db()
        self.config.refresh_from_db()
        self.running.refresh_from_db()
        self.scan.refresh_from_db()
        self.assertEqual(self.website.state, "connected")
        self.assertEqual(self.website.generation, self.generation)
        self.assertEqual(self.config.publish_targets, [{"target_id": "native"}])
        self.assertTrue(self.config.auto_publish)
        self.assertEqual(self.config.article_template, "# Article\nTemplate")
        self.assertEqual(self.running.status, "running")
        self.assertEqual(self.scan.state, "pending")

    def test_rename_refreshes_the_name_and_keeps_everything_else(self):
        result = handle_website_github_event("repository", repository_event("renamed"))
        self.assertEqual(result["revoked"], 0)
        self.assertEqual(result["refreshed"], 1)
        self.assert_setup_kept()
        self.assertEqual(self.website.github_repo, "example/site-renamed")
        self.assertEqual(self.config.github_repo, "example/site-renamed")

    def test_going_private_keeps_the_connection(self):
        result = handle_website_github_event("repository", repository_event("privatized", full_name="example/site"))
        self.assertEqual(result["revoked"], 0)
        self.assert_setup_kept()
        self.assertEqual(self.website.github_repo, "example/site")

    def test_transfer_moves_to_the_founders_installation_on_the_new_account(self):
        GitHubInstallation.objects.create(user=self.founder, installation_id="99", account_login="NewOwner")
        seen = []

        def probe(installation_id, **kwargs):
            seen.append(installation_id)
            return installation_id == "99"

        with patch.object(identity, "installation_can_see", side_effect=probe):
            result = handle_website_github_event("repository",
                repository_event("transferred", full_name="NewOwner/site", owner="NewOwner"))
        self.assertEqual(result["revoked"], 0)
        self.assertEqual(result["access_required"], 0)
        self.assertEqual(seen, ["45", "99"])
        self.assert_setup_kept()
        self.assertEqual((self.website.github_repo, self.website.installation_id), ("NewOwner/site", "99"))
        self.assertEqual((self.config.github_repo, self.config.github_installation_id), ("NewOwner/site", "99"))
        self.assertFalse(identity.has_pausing_blocker(self.website))

    def test_transfer_without_access_asks_for_github_access_instead_of_revoking(self):
        with patch.object(identity, "installation_can_see", return_value=False):
            result = handle_website_github_event("repository",
                repository_event("transferred", full_name="Agency/site", owner="Agency"))
        self.assertEqual((result["revoked"], result["access_required"]), (0, 1))
        self.assert_setup_kept()
        self.assertEqual(self.website.github_repo, "Agency/site")
        self.assertEqual([item["code"] for item in self.website.blockers], ["github_access_required"])
        summary = summary_for(self.config)
        self.assertIn("reconnect", summary["allowedActions"])
        self.assertFalse({"setup", "publish", "preview"} & set(summary["allowedActions"]))
        self.assertIn("Give MLAI access on GitHub", self.website.blockers[0]["message"])

    def test_transfer_never_uses_another_founders_installation(self):
        from django.contrib.auth import get_user_model
        stranger = get_user_model().objects.create_user(email="stranger@example.test")
        GitHubInstallation.objects.create(user=stranger, installation_id="77", account_login="Agency")
        seen = []
        with patch.object(identity, "installation_can_see", side_effect=lambda i, **k: seen.append(i) or False):
            handle_website_github_event("repository", repository_event("transferred", full_name="Agency/site", owner="Agency"))
        self.assertNotIn("77", seen)

    def test_unknown_github_answer_changes_nothing_but_the_name(self):
        with patch.object(identity, "installation_can_see", return_value=None):
            result = handle_website_github_event("repository",
                repository_event("transferred", full_name="Agency/site", owner="Agency"))
        self.assertEqual(result["access_required"], 0)
        self.website.refresh_from_db()
        self.assertEqual(self.website.blockers, [])
        self.assertEqual(self.website.installation_id, "45")

    def test_archive_pauses_changes_and_unarchive_resumes(self):
        handle_website_github_event("repository", repository_event("archived", full_name="example/site"))
        self.assert_setup_kept()
        self.assertEqual([item["code"] for item in self.website.blockers], ["github_repository_archived"])
        summary = summary_for(self.config)
        self.assertFalse({"setup", "publish", "preview"} & set(summary["allowedActions"]))
        self.assertNotIn("reconnect", summary["allowedActions"])
        handle_website_github_event("repository", repository_event("unarchived", full_name="example/site"))
        self.website.refresh_from_db()
        self.config.refresh_from_db()
        self.assertEqual(self.website.blockers, [])
        self.assertIn("setup", summary_for(self.config)["allowedActions"])

    def test_deleting_the_repository_still_revokes(self):
        result = handle_website_github_event("repository", repository_event("deleted"))
        self.assertEqual(result["revoked"], 1)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, "revoked")

    def test_a_malformed_name_is_ignored(self):
        handle_website_github_event("repository", repository_event("renamed", full_name="../evil"))
        self.website.refresh_from_db()
        self.assertEqual(self.website.github_repo, "example/site")

    def test_a_revoked_connection_gets_the_new_name_but_stays_revoked(self):
        self.website.state = "revoked"
        self.website.save(update_fields=["state"])
        with patch.object(identity, "installation_can_see") as probe:
            handle_website_github_event("repository", repository_event("transferred", full_name="Agency/site"))
        probe.assert_not_called()
        self.website.refresh_from_db()
        self.assertEqual((self.website.state, self.website.github_repo), ("revoked", "Agency/site"))

    @override_settings(ARTICLES_REPOSITORY_IDENTITY_REFRESH_ENABLED=False)
    def test_kill_switch_restores_the_old_revoke(self):
        result = handle_website_github_event("repository", repository_event("renamed"))
        self.assertEqual(result["revoked"], 1)
        self.website.refresh_from_db()
        self.assertEqual(self.website.state, "revoked")


class InstallationProbeTests(TestCase):
    def test_probe_reads_definite_refusals_and_unknowns(self):
        from integrations.services.github_app import GitHubAppTokenError
        for error, expected in ((GitHubAppTokenError("no", status_code=422), False),
                                (GitHubAppTokenError("no", status_code=404), False),
                                (GitHubAppTokenError("down", status_code=502), None),
                                (RuntimeError("network"), None)):
            with patch("integrations.services.github_app.create_installation_access_token", side_effect=error):
                self.assertIs(identity.installation_can_see("1", repository_id=5, full_name="a/b"), expected)

    def test_probe_mints_by_repository_id_and_drops_the_token(self):
        from types import SimpleNamespace
        with patch("integrations.services.github_app.create_installation_access_token",
                   return_value=SimpleNamespace(token="t")) as mint, \
                patch("integrations.http_client.delete") as drop:
            self.assertTrue(identity.installation_can_see("1", repository_id=5, full_name="a/b"))
        self.assertEqual(mint.call_args.kwargs["repository_id"], 5)
        self.assertFalse(mint.call_args.kwargs["use_cache"])
        drop.assert_called_once()
