"""A bootstrap read that made no GitHub check shows saved access as connected."""
from unittest.mock import patch

from django.test import SimpleTestCase

from .activation import article_capabilities
from .website_journey import project_journey

SHA = "a" * 40


def journey(capabilities, status="connected", repository_id=123):
    return project_journey(company_id=9, domain="fixture.test", website={"status": status, "repositoryId": repository_id,
        "connectionGeneration": 3, "writePolicy": {"allowed": True}, "verifiedSha": SHA},
        capabilities=capabilities, discovery={"complete": True, "sourceSha": SHA})


def capabilities(access):
    config = type("Config", (), {"github_repo": "owner/site"})()
    with patch("content_factory.activation.get_content_factory_content_island_topic_cost_points", return_value=1), \
            patch("content_factory.activation.get_content_factory_article_cost_points", return_value=1):
        return article_capabilities(config, domain="fixture.test", account={"saved": True, "owned": True},
            evidence={"verified": True, "branch": "main", "sha": SHA}, repository_access=access)


UNOBSERVED = {"verified": False, "reasonCode": "github_verification_required"}


class SavedAccessTests(SimpleTestCase):
    def test_unobserved_access_keeps_github_and_repository_complete(self):
        facts = journey(capabilities(UNOBSERVED))["prerequisites"]
        self.assertEqual(facts["account"]["status"], "complete")
        self.assertEqual(facts["repository"]["status"], "complete")

    def test_unobserved_access_grants_nothing_else(self):
        projected = journey(capabilities(UNOBSERVED))
        self.assertFalse(projected["capabilities"]["canRead"])
        self.assertFalse(projected["capabilities"]["canGenerateArticle"])
        self.assertFalse(projected["capabilities"]["canPublishArticle"])

    def test_refused_access_still_asks_the_owner(self):
        for code in ("github_access_required", "github_unavailable", "website_disconnected"):
            with self.subTest(code=code):
                facts = journey(capabilities({"verified": False, "reasonCode": code}))["prerequisites"]
                self.assertEqual(facts["repository"]["status"], "needs_action")

    def test_unobserved_access_needs_a_live_connection(self):
        for status, repository_id in (("disconnected", 123), ("paused", 123), ("connected", None)):
            with self.subTest(status=status, repository_id=repository_id):
                facts = journey(capabilities(UNOBSERVED), status=status, repository_id=repository_id)["prerequisites"]
                self.assertEqual(facts["repository"]["status"], "needs_action")
