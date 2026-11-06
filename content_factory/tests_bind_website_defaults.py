"""D-09: an empty-looking folder or the default branch never blocks a new-journey founder.

Legacy organisations keep today's blockers, even with every P1 flag on.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from .founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from .website_connections import bind_website, summary_for
from .website_contract import WebsiteAuthorityError

TARGET_BLOCKERS = {"APPLICATION_ROOT_VERIFICATION_REQUIRED", "SETUP_BRANCH_VERIFICATION_REQUIRED"}
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}


def _metadata(repo, branch="main"):
    return {"repository_id": 4242, "github_repo": repo, "installation_id": "45", "branch": branch}


def _blockers(connection):
    return {item.get("code") for item in connection.blockers or []} & TARGET_BLOCKERS


@override_settings(**ALL_P1_FLAGS_ON)
class NewJourneyBindDefaultsTests(TestCase):
    repo = "banksia/website"

    def bind(self, *, app_root="", branch=""):
        built = make_new_journey_org()
        with patch("content_factory.website_connections.verify_repository_access", return_value=_metadata(self.repo)):
            connection = bind_website(built.config, user=built.owner, repo=self.repo, app_root=app_root, branch=branch)
        built.config.refresh_from_db()
        return built, connection

    def test_root_folder_spellings_bind_as_the_whole_repository(self):
        for folder in (".", "./", "/", " ./ "):
            with self.subTest(folder=folder):
                built, connection = self.bind(app_root=folder)
                self.assertEqual(connection.app_root, "")
                self.assertEqual(_blockers(connection), set())
                summary = summary_for(built.config, company_id=built.company.pk)
                self.assertIn("setup", summary["allowedActions"])

    def test_the_default_branch_binds_as_empty(self):
        for branch in ("main", "refs/heads/main"):
            with self.subTest(branch=branch):
                _, connection = self.bind(branch=branch)
                self.assertEqual(connection.branch, "main")
                self.assertEqual(_blockers(connection), set())

    def test_a_real_subfolder_or_another_branch_still_needs_its_own_adapter(self):
        _, connection = self.bind(app_root="apps/web")
        self.assertEqual(_blockers(connection), {"APPLICATION_ROOT_VERIFICATION_REQUIRED"})
        _, connection = self.bind(branch="staging")
        self.assertEqual(_blockers(connection), {"SETUP_BRANCH_VERIFICATION_REQUIRED"})


class LegacyBindDefaultsTests(TestCase):
    """Today's blockers stay for legacy organisations and whenever V2 is off."""

    def legacy_bind(self, name, *, app_root="", branch="", settings_override=ALL_P1_FLAGS_ON):
        with override_settings(**settings_override):
            built = make_named_legacy_org(name)
            repo = f"example-{name.replace('_', '-')}/next-site"
            with patch("content_factory.website_connections.verify_repository_access", return_value=_metadata(repo)):
                return bind_website(built.config, user=built.owner, repo=repo, app_root=app_root, branch=branch)

    def test_named_legacy_orgs_keep_the_folder_blocker_with_every_flag_on(self):
        for name in ("talathrive", "vyavos", "pawshare", "la_vie", "mlai"):
            with self.subTest(org=name):
                connection = self.legacy_bind(name, app_root="./")
                self.assertEqual(connection.app_root, ".")
                self.assertEqual(_blockers(connection), {"APPLICATION_ROOT_VERIFICATION_REQUIRED"})

    def test_a_legacy_slash_folder_is_still_refused(self):
        with self.assertRaises(WebsiteAuthorityError) as raised:
            self.legacy_bind("talathrive", app_root="/")
        self.assertEqual(raised.exception.code, "invalid_repository_path")

    def test_a_legacy_refs_heads_branch_still_blocks(self):
        connection = self.legacy_bind("vyavos", branch="refs/heads/main")
        self.assertEqual(connection.branch, "refs/heads/main")
        self.assertEqual(_blockers(connection), {"SETUP_BRANCH_VERIFICATION_REQUIRED"})

    def test_a_new_journey_org_with_v2_off_keeps_todays_blockers(self):
        with override_settings(**V2_OFF):
            built = make_new_journey_org()
            with patch("content_factory.website_connections.verify_repository_access",
                       return_value=_metadata("banksia/website")):
                connection = bind_website(built.config, user=built.owner, repo="banksia/website", app_root="./")
        self.assertEqual(connection.app_root, ".")
        self.assertEqual(_blockers(connection), {"APPLICATION_ROOT_VERIFICATION_REQUIRED"})
