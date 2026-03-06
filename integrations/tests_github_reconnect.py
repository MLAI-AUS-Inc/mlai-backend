import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from integrations.services.github import (
    AUTH_RECONNECT_TEXT,
    GitHubAuthScanError,
    ScanError,
    scan_github_project,
    trigger_scan_async,
)


class ImmediateThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self.target = target

    def start(self):
        if self.target:
            self.target()


class GithubReconnectWorkerTests(TestCase):
    @patch('threading.Thread', new=ImmediateThread)
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.github.build_github_auth_url', return_value='https://github.example/reconnect')
    @patch('integrations.services.github.scan_github_project')
    def test_trigger_scan_async_posts_reconnect_buttons_for_auth_failures(
        self,
        mock_scan_github_project,
        mock_build_github_auth_url,
        mock_send_message,
    ):
        mock_scan_github_project.side_effect = GitHubAuthScanError("Bad credentials")

        trigger_scan_async(
            "U123",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            domain="mlai.au",
        )

        self.assertEqual(mock_send_message.call_count, 2)
        reconnect_call = mock_send_message.call_args_list[-1]

        self.assertEqual(reconnect_call.args[0], "C123")
        self.assertEqual(reconnect_call.args[1], AUTH_RECONNECT_TEXT)
        self.assertEqual(reconnect_call.kwargs["thread_ts"], "123.456")

        blocks = reconnect_call.kwargs["blocks"]
        self.assertEqual(blocks[0]["type"], "section")
        self.assertEqual(blocks[0]["text"]["text"], AUTH_RECONNECT_TEXT)
        self.assertEqual(blocks[1]["type"], "actions")
        self.assertEqual(len(blocks[1]["elements"]), 2)

        connect_button = blocks[1]["elements"][0]
        self.assertEqual(connect_button["action_id"], "connect_github")
        self.assertEqual(connect_button["url"], "https://github.example/reconnect")
        self.assertEqual(connect_button["style"], "danger")

        resume_button = blocks[1]["elements"][1]
        self.assertEqual(resume_button["action_id"], "resume_scan")
        self.assertEqual(resume_button["style"], "primary")
        self.assertEqual(json.loads(resume_button["value"]), {"domain": "mlai.au"})

        mock_build_github_auth_url.assert_called_once_with("U123", domain="mlai.au")

    @patch('threading.Thread', new=ImmediateThread)
    @patch('integrations.services.slack.SlackService.send_message')
    @patch('integrations.services.github.scan_github_project')
    def test_trigger_scan_async_keeps_plain_text_for_non_auth_failures(
        self,
        mock_scan_github_project,
        mock_send_message,
    ):
        mock_scan_github_project.side_effect = ScanError("Repository scan timed out")

        trigger_scan_async(
            "U123",
            slack_channel_id="C123",
            slack_thread_ts="123.456",
            domain="mlai.au",
        )

        self.assertEqual(mock_send_message.call_count, 2)
        failure_call = mock_send_message.call_args_list[-1]
        self.assertEqual(failure_call.args[0], "C123")
        self.assertEqual(failure_call.args[1], "❌ Scan failed: Repository scan timed out")
        self.assertEqual(failure_call.kwargs["thread_ts"], "123.456")
        self.assertNotIn("blocks", failure_call.kwargs)


class GithubReconnectScanClassificationTests(TestCase):
    @patch('integrations.services.github.time.sleep', return_value=None)
    @patch('integrations.services.github.http_requests.get')
    @patch('integrations.services.github.http_requests.post')
    @patch('integrations.services.github.get_latest_repo_sha', return_value='sha-123')
    @patch('integrations.services.article_generation.get_github_credentials_for_domain')
    def test_scan_github_project_classifies_remote_bad_credentials_as_auth_error(
        self,
        mock_get_credentials,
        mock_get_latest_repo_sha,
        mock_post,
        mock_get,
        mock_sleep,
    ):
        mock_get_credentials.return_value = {
            "token": "gh-token",
            "repo": "owner/repo",
            "source": "org",
        }

        queued_response = MagicMock()
        queued_response.status_code = 202
        queued_response.json.return_value = {"job_id": "job-123", "status": "queued"}
        mock_post.return_value = queued_response

        status_response = MagicMock()
        status_response.status_code = 200
        status_response.json.return_value = {"status": "failed", "error": "Bad credentials"}
        mock_get.return_value = status_response

        with self.assertRaises(GitHubAuthScanError):
            scan_github_project("U123", domain="mlai.au")
