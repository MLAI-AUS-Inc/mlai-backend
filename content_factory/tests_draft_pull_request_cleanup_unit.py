"""Deleting a draft closes only the draft's own automation PR.

TalaThrive (18 Oct): founders could not delete drafts because the delete gate
stopped at any recorded PR link. Deletion now asks GitHub for the PR's real
state, closes an open PR only when every commit came from automation on a
branch of the connected repository, and never deletes branches.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import draft_pull_request_cleanup as cleanup
from .website_contract import WebsiteAuthorityError

REPO = "acme/site"
PR_URL = f"https://github.com/{REPO}/pull/7"
BOT_COMMIT = {"author": None, "commit": {"author": {"email": "content-factory-bot@users.noreply.github.com"}}}
APP_COMMIT = {"author": {"login": "mlai-articles[bot]", "type": "Bot"}, "commit": {"author": {"email": "1+mlai-articles[bot]@users.noreply.github.com"}}}
HUMAN_COMMIT = {"author": {"login": "founder", "type": "User"}, "commit": {"author": {"email": "founder@acme.test"}}}


def _response(status_code=200, payload=None, headers=None):
    return SimpleNamespace(status_code=status_code, json=lambda: payload, headers=headers or {})


def _pull(*, state="open", merged=False, head_repo=REPO, head_ref="mlai/article-7"):
    return {"state": state, "merged": merged, "merged_at": "2026-10-18T00:00:00Z" if merged else None,
        "head": {"ref": head_ref, "repo": {"full_name": head_repo}},
        "base": {"ref": "main", "repo": {"full_name": REPO, "default_branch": "main"}}}


class CloseDraftPullRequestTests(SimpleTestCase):
    def setUp(self):
        self.config = SimpleNamespace(website_connection=SimpleNamespace(github_repo=REPO))
        self.patches = []
        self.minted = []
        self.denied_modes = {}
        self.deleted_tokens = 0
        self.closed = []
        self.pull = _pull()
        self.pull_status = 200
        self.commits = [BOT_COMMIT]
        self.close_status = 200

    def _close(self, url=PR_URL):
        from integrations import http_client

        def mint(data, *, permission_mode, action):
            self.assertEqual(data["github_repo"], REPO)
            self.minted.append((permission_mode, action))
            if permission_mode in self.denied_modes:
                raise self.denied_modes[permission_mode]
            return {"github_token": f"{permission_mode}-token"}

        def get(url, **kwargs):
            if url.endswith("/pulls/7"):
                return _response(self.pull_status, self.pull)
            if url.endswith("/pulls/7/commits"):
                return _response(200, self.commits)
            raise AssertionError(f"Unexpected GitHub read {url}")

        def patch_pull(url, **kwargs):
            self.assertEqual(kwargs["json"], {"state": "closed"})
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer write-token")
            self.closed.append(url)
            return _response(self.close_status, {})

        def delete(url, **kwargs):
            self.assertEqual(url, "https://api.github.com/installation/token")
            self.deleted_tokens += 1
            return _response(204)

        with patch("content_factory.website_connections.contract_for", return_value={"website_connection_id": "c1"}), \
                patch("content_factory.website_tokens.mint_website_token", side_effect=mint), \
                patch.object(http_client, "get", side_effect=get), \
                patch.object(http_client, "patch", side_effect=patch_pull), \
                patch.object(http_client, "delete", side_effect=delete):
            return cleanup.close_draft_pull_request(self.config, url)

    def test_open_automation_pull_request_is_closed_under_cleanup_authority(self):
        self.commits = [BOT_COMMIT, APP_COMMIT]
        receipt = self._close()
        self.assertEqual(receipt, {"prUrl": PR_URL, "ok": True, "action": cleanup.CLOSED, "reason": ""})
        self.assertEqual(self.minted, [("write", "cleanup")])
        self.assertEqual(self.closed, [f"https://api.github.com/repos/{REPO}/pulls/7"])
        self.assertEqual(self.deleted_tokens, 1)

    def test_already_closed_pull_request_lets_the_draft_go_without_a_write(self):
        self.pull = _pull(state="closed")
        receipt = self._close()
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["action"], cleanup.ALREADY_CLOSED)
        self.assertEqual(self.closed, [])

    def test_merged_pull_request_keeps_the_draft(self):
        self.pull = _pull(state="closed", merged=True)
        receipt = self._close()
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason"], cleanup.MERGED)
        self.assertEqual(self.closed, [])

    def test_commit_from_a_person_leaves_the_pull_request_open(self):
        self.commits = [BOT_COMMIT, HUMAN_COMMIT]
        receipt = self._close()
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason"], cleanup.TEAM_CHANGES)
        self.assertEqual(self.closed, [])

    def test_fork_or_default_branch_head_is_never_closed(self):
        for pull in (_pull(head_repo="someone/fork"), _pull(head_ref="main")):
            self.pull, self.closed = pull, []
            receipt = self._close()
            self.assertEqual(receipt["reason"], cleanup.TEAM_CHANGES)
            self.assertEqual(self.closed, [])

    def test_pull_request_outside_the_connected_repository_is_not_touched(self):
        receipt = self._close("https://github.com/other/repo/pull/7")
        self.assertEqual(receipt["action"], cleanup.NOT_ACCESSIBLE)
        self.assertTrue(receipt["ok"])
        self.assertEqual(self.minted, [])

    def test_draft_without_a_website_connection_is_not_blocked_forever(self):
        self.config = SimpleNamespace(website_connection=None)
        receipt = self._close()
        self.assertEqual((receipt["ok"], receipt["action"]), (True, cleanup.NOT_ACCESSIBLE))
        self.assertEqual(self.minted, [])

    def test_paused_publishing_still_confirms_closed_but_asks_to_close_open(self):
        self.denied_modes = {"write": WebsiteAuthorityError("website_publishing_paused", "Publishing is paused.")}
        receipt = self._close()
        self.assertEqual(receipt["reason"], cleanup.CLOSE_MANUALLY)
        self.assertEqual(self.minted, [("write", "cleanup"), ("read", "read")])
        self.assertEqual(self.closed, [])
        self.pull = _pull(state="closed")
        self.assertEqual(self._close()["action"], cleanup.ALREADY_CLOSED)

    def test_transient_failures_ask_to_retry(self):
        self.denied_modes = {"write": WebsiteAuthorityError("github_temporarily_unavailable", "Retry.", retryable=True)}
        self.assertEqual(self._close()["reason"], cleanup.GITHUB_UNAVAILABLE)
        self.denied_modes = {}
        self.pull_status = 502
        self.assertEqual(self._close()["reason"], cleanup.GITHUB_UNAVAILABLE)
        self.pull_status = 200
        self.close_status = 422
        self.assertEqual(self._close()["reason"], cleanup.GITHUB_UNAVAILABLE)

    def test_lost_repository_access_does_not_block_deletion(self):
        self.pull_status = 404
        receipt = self._close()
        self.assertEqual((receipt["ok"], receipt["action"]), (True, cleanup.NOT_ACCESSIBLE))

    def test_blocked_detail_names_the_actionable_reason_first(self):
        self.assertIn("commits from your team", cleanup.blocked_detail({cleanup.MERGED, cleanup.TEAM_CHANGES}))
        self.assertIn("already been merged", cleanup.blocked_detail({cleanup.MERGED}))
        self.assertIn("Try deleting it again", cleanup.blocked_detail(set()))
