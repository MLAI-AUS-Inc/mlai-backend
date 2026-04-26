from unittest.mock import MagicMock, patch

from django.test import Client, TestCase
from django.urls import reverse

from content_factory.models import OrganizationContentConfig
from integrations.models import UserIntegration
from integrations.services.github_connections import build_github_oauth_state, store_github_oauth_state
from organizations.models import Organization


def _json_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class GitHubCallbackTests(TestCase):
    def setUp(self):
        self.client = Client()

    def _callback(
        self,
        *,
        domain: str,
        slack_user_id: str,
        installation_id: str,
        repos: list[dict],
        github_login: str = "octocat",
        store_state: bool = True,
    ):
        oauth_state = build_github_oauth_state(domain=domain, slack_user_id=slack_user_id)
        if store_state:
            store_github_oauth_state(oauth_state)

        token_payload = {
            "access_token": "gh-access",
            "refresh_token": "gh-refresh",
            "expires_in": 3600,
        }
        user_payload = {"login": github_login}
        repo_payload = {"repositories": repos}

        with patch("integrations.views.requests.post", return_value=_json_response(token_payload)), patch(
            "integrations.views.requests.get",
            side_effect=[_json_response(user_payload), _json_response(repo_payload)],
        ), patch("integrations.services.github.trigger_scan_async") as mock_trigger_scan, patch(
            "integrations.services.slack.SlackService.send_dm"
        ) as mock_send_dm:
            response = self.client.get(
                reverse("github_callback"),
                {
                    "code": "oauth-code",
                    "installation_id": installation_id,
                    "setup_action": "install",
                    "state": oauth_state.raw,
                },
            )

        return response, mock_trigger_scan, mock_send_dm

    def test_callback_rejects_invalid_state(self):
        response = self.client.get(
            reverse("github_callback"),
            {
                "code": "oauth-code",
                "installation_id": "12345",
                "setup_action": "install",
                "state": "invalid::state",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Invalid or expired state", response.content)

    def test_org_level_callback_creates_owned_domain_config_for_single_repo(self):
        response, mock_trigger_scan, mock_send_dm = self._callback(
            domain="mlai.au",
            slack_user_id="U123",
            installation_id="inst-1",
            repos=[{"full_name": "owner/mlai-au"}],
        )

        self.assertEqual(response.status_code, 200)
        config = OrganizationContentConfig.objects.get(organization__domain="mlai.au")
        self.assertEqual(config.connected_slack_user_id, "U123")
        self.assertEqual(config.github_repo, "owner/mlai-au")
        self.assertEqual(config.github_installation_id, "inst-1")

        integration = UserIntegration.objects.get(slack_user_id="U123")
        self.assertEqual(integration.github_repo, "owner/mlai-au")

        mock_trigger_scan.assert_called_once_with("U123", domain="mlai.au")
        mock_send_dm.assert_called_once()

    def test_org_level_callback_accepts_signed_state_without_cache_or_session(self):
        response, mock_trigger_scan, mock_send_dm = self._callback(
            domain="worker-safe.com",
            slack_user_id="U123",
            installation_id="inst-signed",
            repos=[{"full_name": "owner/worker-safe"}],
            store_state=False,
        )

        self.assertEqual(response.status_code, 200)
        config = OrganizationContentConfig.objects.get(organization__domain="worker-safe.com")
        self.assertEqual(config.connected_slack_user_id, "U123")
        self.assertEqual(config.github_repo, "owner/worker-safe")
        mock_trigger_scan.assert_called_once_with("U123", domain="worker-safe.com")
        mock_send_dm.assert_called_once()

    def test_callback_preserves_existing_domain_connection_when_connecting_new_domain(self):
        self._callback(
            domain="domain-one.com",
            slack_user_id="U123",
            installation_id="inst-1",
            repos=[{"full_name": "owner/domain-one"}],
            github_login="sam-one",
        )

        response, mock_trigger_scan, _ = self._callback(
            domain="domain-two.com",
            slack_user_id="U123",
            installation_id="inst-2",
            repos=[{"full_name": "owner/domain-two"}],
            github_login="sam-two",
        )

        self.assertEqual(response.status_code, 200)
        configs = {
            config.organization.domain: config
            for config in OrganizationContentConfig.objects.order_by("organization__domain")
        }
        self.assertEqual(configs["domain-one.com"].github_repo, "owner/domain-one")
        self.assertEqual(configs["domain-two.com"].github_repo, "owner/domain-two")
        self.assertEqual(configs["domain-one.com"].connected_slack_user_id, "U123")
        self.assertEqual(configs["domain-two.com"].connected_slack_user_id, "U123")

        integration = UserIntegration.objects.get(slack_user_id="U123")
        self.assertEqual(integration.github_repo, "owner/domain-one")
        self.assertEqual(integration.github_installation_id, "inst-2")
        self.assertEqual(mock_trigger_scan.call_count, 1)

    def test_callback_requires_exactly_one_repo_for_domain_binding(self):
        response, mock_trigger_scan, mock_send_dm = self._callback(
            domain="ambiguous.com",
            slack_user_id="U123",
            installation_id="inst-3",
            repos=[
                {"full_name": "owner/repo-one"},
                {"full_name": "owner/repo-two"},
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"exactly one repository", response.content)

        config = OrganizationContentConfig.objects.get(organization__domain="ambiguous.com")
        self.assertEqual(config.connected_slack_user_id, "U123")
        self.assertIsNone(config.github_repo)
        mock_trigger_scan.assert_not_called()
        mock_send_dm.assert_called_once()

    def test_callback_preserves_preselected_repo_when_installation_has_multiple_repos(self):
        org = Organization.objects.create(domain="preselected.com", name="Preselected")
        OrganizationContentConfig.objects.create(
            organization=org,
            github_repo="owner/repo-two",
            connected_slack_user_id="U123",
        )

        response, mock_trigger_scan, mock_send_dm = self._callback(
            domain="preselected.com",
            slack_user_id="U123",
            installation_id="inst-4",
            repos=[
                {"full_name": "owner/repo-one"},
                {"full_name": "owner/repo-two"},
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Linked repository: <strong>owner/repo-two</strong>", response.content)

        config = OrganizationContentConfig.objects.get(organization__domain="preselected.com")
        self.assertEqual(config.github_repo, "owner/repo-two")
        self.assertEqual(config.github_installation_id, "inst-4")
        mock_trigger_scan.assert_called_once_with("U123", domain="preselected.com")
        mock_send_dm.assert_called_once()

    def test_callback_keeps_existing_repo_without_auto_scan_when_multiple_repos_do_not_match(self):
        org = Organization.objects.create(domain="needs-selection.com", name="Needs Selection")
        OrganizationContentConfig.objects.create(
            organization=org,
            github_repo="owner/not-selected",
            connected_slack_user_id="U123",
        )

        response, mock_trigger_scan, mock_send_dm = self._callback(
            domain="needs-selection.com",
            slack_user_id="U123",
            installation_id="inst-5",
            repos=[
                {"full_name": "owner/repo-one"},
                {"full_name": "owner/repo-two"},
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Multiple repositories are selected", response.content)

        config = OrganizationContentConfig.objects.get(organization__domain="needs-selection.com")
        self.assertEqual(config.github_repo, "owner/not-selected")
        self.assertEqual(config.github_token_encrypted, "gh-access")
        self.assertEqual(config.github_installation_id, "inst-5")
        mock_trigger_scan.assert_not_called()
        mock_send_dm.assert_called_once()
