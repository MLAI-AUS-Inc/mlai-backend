"""Private relay role lookups remain authenticated and outside public quotas."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from community_chat.permission_views import RelayChatRoleView

TOKEN = "synthetic-independent-relay-role-credential"
KEY = "a" * 64


@override_settings(COMMUNITY_CHAT_ROLE_SERVICE_TOKEN=TOKEN,
    REST_FRAMEWORK={"DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
        "DEFAULT_THROTTLE_RATES": {"anon": "1/hour"}})
class RelayRoleServiceUnitTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = RelayChatRoleView.as_view()

    def test_sixty_five_authorized_lookups_preserve_role_authority_without_public_ip_budget(self):
        with patch("community_chat.permission_views.device_chat_role", return_value="member") as role, patch(
                "community_chat.permission_views.device_protection_role", return_value="member") as protection:
            for _ in range(65):
                response = self.view(self.factory.get("/relay-roles/", HTTP_AUTHORIZATION="Bearer " + TOKEN), public_key=KEY)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["role"], "member")
                self.assertEqual(response.data["public_key"], KEY)
                self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(role.call_count, 65)
        self.assertEqual(protection.call_count, 65)

    def test_missing_or_wrong_service_credentials_never_resolve_an_account(self):
        with patch("community_chat.permission_views.device_chat_role") as role:
            for header in ("", "Bearer wrong-credential", "Bearer " + TOKEN + "x"):
                response = self.view(self.factory.get("/relay-roles/", HTTP_AUTHORIZATION=header), public_key=KEY)
                self.assertEqual(response.status_code, 403)
        role.assert_not_called()

    def test_invalid_public_key_is_rejected_before_lookup(self):
        with patch("community_chat.permission_views.device_chat_role") as role:
            response = self.view(self.factory.get("/relay-roles/", HTTP_AUTHORIZATION="Bearer " + TOKEN), public_key="invalid")
        self.assertEqual(response.status_code, 400)
        role.assert_not_called()
