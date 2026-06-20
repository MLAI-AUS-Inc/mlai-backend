import hashlib
import hmac
import json

from django.test import TestCase, override_settings
from django.urls import reverse

from content_factory.github_webhook import (
    handle_pull_request,
    handle_push,
    process_github_event,
    verify_github_signature,
)
from content_factory.models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from organizations.models import Organization

REPO = "MLAI-AUS-Inc/mlai-au"


def _sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class VerifyGitHubSignatureTest(TestCase):
    def test_valid_signature(self):
        body = b'{"hello":"world"}'
        self.assertTrue(verify_github_signature("secret", body, _sign("secret", body)))

    def test_wrong_secret_fails(self):
        body = b'{"hello":"world"}'
        self.assertFalse(verify_github_signature("secret", body, _sign("other", body)))

    def test_missing_secret_always_fails(self):
        body = b"{}"
        self.assertFalse(verify_github_signature("", body, _sign("anything", body)))

    def test_non_sha256_header_fails(self):
        self.assertFalse(verify_github_signature("secret", b"{}", "sha1=deadbeef"))


class GitHubWebhookHandlersTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo=REPO)

    def _article(self, slug, **overrides):
        fields = {
            "organization": self.organization,
            "title": slug,
            "slug": slug,
            "category": "featured",
            "primary_keyword": slug,
        }
        fields.update(overrides)
        return WrittenArticle.objects.create(**fields)

    def _pr_payload(self, number, *, merged, base_ref="main", state="open", merge_commit_sha="abc123", html_url=None):
        return {
            "pull_request": {
                "number": number,
                "html_url": html_url or f"https://github.com/{REPO}/pull/{number}",
                "merged": merged,
                "merged_at": "2026-06-09T01:02:03Z" if merged else None,
                "state": state,
                "merge_commit_sha": merge_commit_sha,
                "base": {"ref": base_ref},
            },
            "repository": {"full_name": REPO, "default_branch": "main"},
        }

    def test_merge_into_default_branch_sets_on_main(self):
        article = self._article(
            "a",
            pr_url=f"https://github.com/{REPO}/pull/990",
            pr_number=990,
            publish_status=ArticlePublishStatus.PR_OPEN,
        )
        summary = handle_pull_request(self._pr_payload(990, merged=True, base_ref="main", state="closed"))
        article.refresh_from_db()
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNotNone(article.on_main_verified_at)
        self.assertEqual(article.on_main_commit_sha, "abc123")

    def test_merge_into_non_default_branch_does_not_set_on_main(self):
        article = self._article(
            "b",
            pr_url=f"https://github.com/{REPO}/pull/991",
            pr_number=991,
            publish_status=ArticlePublishStatus.PR_OPEN,
        )
        handle_pull_request(self._pr_payload(991, merged=True, base_ref="staging", state="closed"))
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNone(article.on_main_verified_at)
        self.assertEqual(article.merge_commit_sha, "abc123")

    def test_opened_pr_sets_pr_open(self):
        article = self._article("c", pr_url=f"https://github.com/{REPO}/pull/992", pr_number=992)
        handle_pull_request(self._pr_payload(992, merged=False, state="open"))
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.PR_OPEN)

    def test_matched_by_pr_number_and_repo_when_url_absent(self):
        # No stored pr_url, so matching falls back to (number, repo via config).
        article = self._article("d", pr_number=993, publish_status=ArticlePublishStatus.WRITTEN)
        summary = handle_pull_request(self._pr_payload(993, merged=True, base_ref="main", state="closed"))
        article.refresh_from_db()
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNotNone(article.on_main_verified_at)

    def test_push_to_default_branch_confirms_on_main_via_content_path(self):
        article = self._article("e", content_path="src/content/articles/e.mdx")
        summary = handle_push(
            {
                "ref": "refs/heads/main",
                "repository": {"full_name": REPO, "default_branch": "main"},
                "commits": [{"added": ["src/content/articles/e.mdx"], "modified": []}],
                "head_commit": {"id": "deadbeef"},
            }
        )
        article.refresh_from_db()
        self.assertEqual(summary["updated"], 1)
        self.assertIsNotNone(article.on_main_verified_at)
        self.assertEqual(article.on_main_commit_sha, "deadbeef")

    def test_push_to_non_default_branch_is_ignored(self):
        article = self._article("f", content_path="src/content/articles/f.mdx")
        summary = handle_push(
            {
                "ref": "refs/heads/feature",
                "repository": {"full_name": REPO, "default_branch": "main"},
                "commits": [{"added": ["src/content/articles/f.mdx"]}],
                "head_commit": {"id": "cafe"},
            }
        )
        article.refresh_from_db()
        self.assertIn("ignored", summary)
        self.assertIsNone(article.on_main_verified_at)

    def test_ping_and_unknown_events(self):
        self.assertEqual(process_github_event("ping", {}), {"event": "ping"})
        self.assertTrue(process_github_event("issues", {}).get("ignored"))


@override_settings(GITHUB_WEBHOOK_SECRET="s3cr3t")
class GitHubWebhookViewTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="mlai.au", name="MLAI")
        OrganizationContentConfig.objects.create(organization=self.organization, github_repo=REPO)
        self.url = reverse("content_factory_github_webhook")

    def _post(self, payload, *, event="pull_request", secret="s3cr3t"):
        body = json.dumps(payload).encode()
        return self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(secret, body),
            HTTP_X_GITHUB_EVENT=event,
        )

    def test_rejects_invalid_signature(self):
        body = json.dumps({}).encode()
        response = self.client.post(
            self.url,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=bad",
            HTTP_X_GITHUB_EVENT="ping",
        )
        self.assertEqual(response.status_code, 401)

    def test_ping_with_valid_signature(self):
        response = self._post({}, event="ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event"], "ping")

    def test_pull_request_merge_updates_article(self):
        article = WrittenArticle.objects.create(
            organization=self.organization,
            title="g",
            slug="g",
            category="featured",
            primary_keyword="g",
            pr_url=f"https://github.com/{REPO}/pull/994",
            pr_number=994,
            publish_status=ArticlePublishStatus.PR_OPEN,
        )
        payload = {
            "pull_request": {
                "number": 994,
                "html_url": f"https://github.com/{REPO}/pull/994",
                "merged": True,
                "merged_at": "2026-06-09T01:02:03Z",
                "state": "closed",
                "merge_commit_sha": "abc123",
                "base": {"ref": "main"},
            },
            "repository": {"full_name": REPO, "default_branch": "main"},
        }
        response = self._post(payload, event="pull_request")
        self.assertEqual(response.status_code, 200)
        article.refresh_from_db()
        self.assertEqual(article.publish_status, ArticlePublishStatus.MERGED)
        self.assertIsNotNone(article.on_main_verified_at)
