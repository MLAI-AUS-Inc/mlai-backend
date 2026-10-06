"""Rate-card read contracts; no database setup or migrations are required."""

import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from roo.models import TaskTemplate
from roo.views import RateCardView


@override_settings(
    ROO_API_KEY="roo-test-key",
    INTERNAL_API_KEY="internal-test-key",
    MLAI_API_KEY="legacy-test-key",
    ORG_BRAIN_API_KEY="unrelated-admin-test-key",
)
class RateCardTests(SimpleTestCase):
    databases = set()
    endpoint = "/api/v1/points/rate-card/"

    def setUp(self):
        self.factory = APIRequestFactory()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.template = TaskTemplate(
            name="Newsletter", alias="newsletter", points=5,
            description="Write a newsletter", is_active=True,
        )

    def response(self, request, rows=None):
        with patch.object(RateCardView, "get_queryset", return_value=rows or []) as query:
            response = resolve(self.endpoint).func(request)
        return response, query

    def test_permissions_instantiate_without_raw_setting_strings(self):
        self.assertTrue(RateCardView().get_permissions())

    def test_read_credentials_and_authenticated_user_are_accepted(self):
        requests = [
            self.factory.get(self.endpoint, HTTP_X_API_KEY=key)
            for key in ("roo-test-key", "internal-test-key", "legacy-test-key")
        ]
        requests.append(self.factory.get(
            self.endpoint, HTTP_AUTHORIZATION="Api-Key roo-test-key",
        ))
        browser_request = self.factory.get(self.endpoint)
        force_authenticate(browser_request, user=User(email="member@example.test"))
        requests.append(browser_request)
        for request in requests:
            with self.subTest(headers=dict(request.headers)):
                response, _ = self.response(request, [self.template])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, [{
                    "name": "Newsletter", "alias": "newsletter", "points": 5,
                    "description": "Write a newsletter", "is_active": True,
                }])

    def test_untrusted_readers_are_denied_before_query(self):
        for key in (None, "bad-test-key", "unrelated-admin-test-key"):
            with self.subTest(key=key):
                headers = {"HTTP_X_API_KEY": key} if key else {}
                response, query = self.response(self.factory.get(self.endpoint, **headers))
                self.assertIn(response.status_code, (401, 403))
                query.assert_not_called()
        with override_settings(ROO_API_KEY="", INTERNAL_API_KEY="", MLAI_API_KEY=""):
            response, query = self.response(self.factory.get(
                self.endpoint, HTTP_X_API_KEY="roo-test-key",
            ))
            self.assertIn(response.status_code, (401, 403))
            query.assert_not_called()

    def test_empty_card_is_successful_empty_array(self):
        response, _ = self.response(self.factory.get(
            self.endpoint, HTTP_X_API_KEY="roo-test-key",
        ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_queryset_preserves_active_filter_and_name_ordering(self):
        self.assertEqual(
            str(RateCardView.queryset.query),
            str(TaskTemplate.objects.filter(is_active=True).query),
        )
        self.assertEqual(TaskTemplate._meta.ordering, ["name"])

    def test_rate_card_route_is_read_only(self):
        response, query = self.response(self.factory.post(
            self.endpoint, {}, HTTP_X_API_KEY="roo-test-key",
        ))
        self.assertEqual(response.status_code, 405)
        query.assert_not_called()
