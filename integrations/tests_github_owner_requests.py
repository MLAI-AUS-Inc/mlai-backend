"""An organisation owner's approval connects the founder who asked for the GitHub app."""
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from content_factory.founder_journey_cohort import read_marker
from content_factory.founder_journey_testing import make_named_legacy_org, make_new_journey_org
from content_factory.website_reconciliation import handle_website_github_event
from integrations.models import GitHubInstallation
from integrations.services import github_callback_reasons as reasons
from integrations.services import github_owner_requests as owner_requests
from integrations.tests_github_callback_reasons import CallbackReasonTestBase


def installation_created(*repositories, installation_id=555):
    return {"action": "created",
            "installation": {"id": installation_id, "account": {"login": "agency", "type": "Organization"}},
            "repositories": [{"id": index + 100, "full_name": name} for index, name in enumerate(repositories)]}


class OwnerApprovalTests(CallbackReasonTestBase):
    def setUp(self):
        super().setUp()
        patches = [
            patch("content_factory.website_connections.verify_repository_access",
                  side_effect=lambda *, user, repo, **kw: {"repository_id": 4242, "github_repo": repo,
                                                           "installation_id": "555", "branch": "main"}),
            patch("content_factory.website_prepare.start_prepare", return_value=Obj(pk=1)),
            patch("content_factory.website_prepare.advance_prepare"),
        ]
        self.verify, self.prepare, _ = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def request(self, built, *, days_ago=0):
        owner_requests.record_request(organization=built.organization, user_id=built.owner.pk, company_id=built.company.pk)
        if days_ago:
            built.config.refresh_from_db()
            system = built.config.article_system
            system[owner_requests.KEY]["requested_at"] = (timezone.now() - timedelta(days=days_ago)).isoformat()
            built.config.article_system = system
            built.config.save(update_fields=["article_system"])

    def test_owner_approval_request_is_remembered_for_the_founder(self):
        with patch("integrations.services.github_owner_requests.record_request") as record:
            response = self.callback(self.params(self.chat_state(self.new), setup_action="request",
                                                 installation_id=None, code=None))
        self.assertReason(response, reasons.OWNER_APPROVAL_REQUESTED)
        record.assert_called_once_with(organization=self.new.organization, user_id=self.new.owner.pk,
                                       company_id=self.new.company.pk)
        self.callback(self.params(self.chat_state(self.new), setup_action="request", installation_id=None, code=None))
        self.new.config.refresh_from_db()
        saved = self.new.config.article_system[owner_requests.KEY]
        self.assertEqual((saved["user_id"], saved["company_id"]), (self.new.owner.pk, str(self.new.company.pk)))

    def test_approved_install_connects_the_founder_and_moves_their_card_on(self):
        self.request(self.new)
        result = handle_website_github_event("installation",
                                             installation_created("agency/banksia-studio", "agency/other"))
        self.assertEqual(result["owner_requests_connected"], 1)
        self.new.config.refresh_from_db()
        connection = self.new.config.website_connection
        self.assertEqual(connection.github_repo, "agency/banksia-studio")
        self.assertTrue(GitHubInstallation.objects.filter(user=self.new.owner, installation_id="555",
                                                          account_login="agency").exists())
        self.assertNotIn(owner_requests.KEY, self.new.config.article_system)
        self.assertEqual(read_marker(self.new.organization)["githubReturn"]["reason"], reasons.CONNECTED)
        self.prepare.assert_called_once()

    def test_repositories_added_later_also_connects(self):
        self.request(self.new)
        payload = {"action": "added", "installation": {"id": 556, "account": {"login": "agency"}},
                   "repositories_added": [{"id": 9, "full_name": "agency/banksia-studio"}]}
        self.assertEqual(handle_website_github_event("installation_repositories", payload)["owner_requests_connected"], 1)

    def test_a_legacy_founder_is_connected_too(self):
        legacy = make_named_legacy_org("mlai", domain="legacy-banksia.studio")
        legacy.config.website_connection = None
        legacy.config.save(update_fields=["website_connection"])
        self.request(legacy)
        handle_website_github_event("installation", installation_created("agency/legacy-banksia-studio"))
        legacy.config.refresh_from_db()
        self.assertEqual(legacy.config.website_connection.github_repo, "agency/legacy-banksia-studio")

    def test_ambiguous_old_unmatched_and_connected_requests_are_left_alone(self):
        twin = make_new_journey_org(domain="banksia-studio.com")
        self.request(self.new)
        self.request(twin)
        with self.subTest("two startups claim one repository"):
            result = handle_website_github_event("installation", installation_created("agency/banksia-studio"))
            self.assertEqual(result["owner_requests_connected"], 0)
        twin.config.article_system = {}
        twin.config.save(update_fields=["article_system"])
        with self.subTest("no repository named like the domain"):
            result = handle_website_github_event("installation", installation_created("agency/unrelated"))
            self.assertEqual(result["owner_requests_connected"], 0)
        with self.subTest("request older than the window"):
            self.request(self.new, days_ago=owner_requests.REQUEST_DAYS + 1)
            result = handle_website_github_event("installation", installation_created("agency/banksia-studio"))
            self.assertEqual(result["owner_requests_connected"], 0)
        self.verify.assert_not_called()
        self.new.config.refresh_from_db()
        self.assertIsNone(self.new.config.website_connection)

    def test_an_already_connected_startup_is_never_rebound(self):
        legacy = make_named_legacy_org("talathrive", domain="connected-banksia.studio")
        before = legacy.config.website_connection_id
        self.request(legacy)
        result = handle_website_github_event("installation", installation_created("agency/connected-banksia-studio"))
        self.assertEqual(result["owner_requests_connected"], 0)
        legacy.config.refresh_from_db()
        self.assertEqual(legacy.config.website_connection_id, before)

    @override_settings(ARTICLES_GITHUB_OWNER_APPROVAL_PICKUP_ENABLED=False)
    def test_kill_switch_records_and_picks_up_nothing(self):
        self.request(self.new)
        self.new.config.refresh_from_db()
        self.assertNotIn(owner_requests.KEY, self.new.config.article_system or {})
        result = handle_website_github_event("installation", installation_created("agency/banksia-studio"))
        self.assertEqual(result["owner_requests_connected"], 0)
