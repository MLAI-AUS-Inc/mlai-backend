from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .article_request_identity import ContentFactoryDispatchOutbox, stabilize_article_request_metadata
from .website_contract import WebsiteAuthorityError


class ArticleRequestIdentityTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(pk=7, slack_id="U76543210")
        self.org = SimpleNamespace(pk=42)
        self.rows = self.enterContext(patch.object(ContentFactoryDispatchOutbox.objects, "filter"))
        self.rows.return_value.first.return_value = None

    def payload(self, **extra):
        return {"client_request_id": "same-request", "analytics_article_id": "random-on-each-click",
                "analytics_config": {"analytics_article_id": "random-on-each-click"},
                "slack_user_id": self.user.slack_id, "topic": "Original article", **extra}

    def stabilize(self, payload):
        stabilize_article_request_metadata(payload, organization=self.org, user=self.user)
        return payload

    def test_same_request_reuses_analytics_identity_in_both_locations(self):
        first = self.stabilize(self.payload())
        retry = self.stabilize(self.payload(analytics_article_id="another-random-id"))
        self.assertEqual(first, retry)
        self.assertEqual(first["analytics_article_id"], first["analytics_config"]["analytics_article_id"])

    def test_different_tenant_or_request_has_an_independent_analytics_identity(self):
        first = self.stabilize(self.payload())["analytics_article_id"]
        self.assertNotEqual(first, self.stabilize(self.payload(client_request_id="other"))["analytics_article_id"])
        self.org.pk = 43
        self.assertNotEqual(first, self.stabilize(self.payload())["analytics_article_id"])

    def test_owned_legacy_outbox_preserves_original_metadata_after_slack_linking(self):
        saved = self.payload(analytics_article_id="historical-id", slack_user_id="mlai_user:7",
                             analytics_config={"analytics_article_id": "historical-id", "enabled": True},
                             roo_points_requested_by_user_id=7)
        original = deepcopy(saved)
        self.rows.return_value.first.return_value = SimpleNamespace(payload={"request": saved})
        result = self.stabilize(self.payload(topic="Changed content must still fail the ordinary digest"))
        self.assertEqual(result["analytics_article_id"], "historical-id")
        self.assertEqual(result["slack_user_id"], "mlai_user:7")
        self.assertEqual(result["analytics_config"], saved["analytics_config"])
        self.assertEqual(result["topic"], "Changed content must still fail the ordinary digest")
        self.assertEqual(saved, original)

    def test_other_requester_cannot_adopt_the_original_request_metadata(self):
        saved = self.payload(roo_points_requested_by_user_id=8)
        self.rows.return_value.first.return_value = SimpleNamespace(payload={"request": saved})
        with self.assertRaises(WebsiteAuthorityError):
            self.stabilize(self.payload())

    def test_legacy_actor_must_still_belong_to_the_current_account(self):
        self.rows.return_value.first.return_value = SimpleNamespace(payload={"request": self.payload(slack_user_id="UOTHER1234")})
        with self.assertRaises(WebsiteAuthorityError):
            self.stabilize(self.payload())

    def test_non_article_callers_without_analytics_do_not_query_storage(self):
        payload = {"client_request_id": "goal-request"}
        self.stabilize(payload)
        self.assertEqual(payload, {"client_request_id": "goal-request"})
        self.rows.assert_not_called()
