"""Separate Slack apps must never share event authority or request budgets."""
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services.message_sync.authorizations import (
    AuthorizationConfigurationError, expand_authorization_page,
)
from integrations.services.message_sync.configuration import (
    authorization_token, user_app_id, valid_callback_signature,
)
from integrations.services.message_sync.slack_client import budgeted_client


@override_settings(
    MESSAGE_SYNC_ENABLED=True,
    MESSAGE_SYNC_SLACK_APP_ID="APUBLIC",
    MESSAGE_SYNC_SLACK_APP_TOKEN="xapp-public-synthetic",
    SLACK_BRIDGE_SIGNING_SECRET="public-synthetic-secret",
    MESSAGE_SYNC_SLACK_USER_APP_ID="APRIVATE",
    MESSAGE_SYNC_SLACK_USER_APP_TOKEN="xapp-private-synthetic",
    MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET="private-synthetic-secret",
)
class MessageSyncAppAuthorityTests(SimpleTestCase):
    def signed(self, payload, secret, age=0):
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()) - age)
        signature = "v0=" + hmac.new(
            secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256,
        ).hexdigest()
        return body, timestamp, signature

    def test_events_require_the_signing_secret_of_the_claimed_app(self):
        for app, secret in [("APUBLIC", "public-synthetic-secret"),
                            ("APRIVATE", "private-synthetic-secret")]:
            with self.subTest(app=app):
                payload = {"type": "event_callback", "api_app_id": app}
                self.assertTrue(valid_callback_signature(*self.signed(payload, secret)))
                other = "private-synthetic-secret" if app == "APUBLIC" else "public-synthetic-secret"
                self.assertFalse(valid_callback_signature(*self.signed(payload, other)))
                self.assertFalse(valid_callback_signature(*self.signed(payload, secret, age=301)))
        for app in ["AUNKNOWN", ""]:
            self.assertFalse(valid_callback_signature(*self.signed(
                {"type": "event_callback", "api_app_id": app}, "public-synthetic-secret",
            )))

    def test_either_app_can_verify_its_url_without_claiming_message_authority(self):
        for secret in ["public-synthetic-secret", "private-synthetic-secret"]:
            self.assertTrue(valid_callback_signature(*self.signed(
                {"type": "url_verification", "challenge": "synthetic"}, secret,
            )))
        self.assertFalse(valid_callback_signature(*self.signed(
            {"type": "url_verification"}, "unknown-secret",
        )))

    def test_app_tokens_are_exactly_scoped_and_single_app_remains_supported(self):
        self.assertEqual(authorization_token("APUBLIC"), "xapp-public-synthetic")
        self.assertEqual(authorization_token("APRIVATE"), "xapp-private-synthetic")
        self.assertEqual(authorization_token("AUNKNOWN"), "")
        self.assertEqual(user_app_id(), "APRIVATE")
        with override_settings(MESSAGE_SYNC_SLACK_USER_APP_ID=""):
            self.assertEqual(user_app_id(), "APUBLIC")
            self.assertEqual(authorization_token(user_app_id()), "xapp-public-synthetic")

    def test_private_recipient_pages_use_only_private_app_token_and_budget(self):
        client = MagicMock()
        client.apps_event_authorizations_list.side_effect = [
            {"ok": True, "authorizations": [{"team_id": "T1", "user_id": "U1"}],
             "response_metadata": {"next_cursor": "page2"}},
            {"ok": True, "authorizations": [{"team_id": "T1", "user_id": "U2"},
                                            {"team_id": "TOTHER", "user_id": "U3"}]},
        ]
        payload = {"api_app_id": "APRIVATE", "team_id": "T1", "event_context": "context"}
        with patch('integrations.services.message_sync.authorizations.WebClient', return_value=client) as sdk, patch(
            'integrations.services.message_sync.authorizations.budgeted_client', return_value=client,
        ) as budget:
            page, complete = expand_authorization_page(payload)
            self.assertFalse(complete)
            page, complete = expand_authorization_page(page)
        self.assertTrue(complete)
        self.assertEqual([row['user_id'] for row in page['authorizations']], ['U1', 'U2'])
        self.assertEqual(sdk.call_args.kwargs['token'], 'xapp-private-synthetic')
        self.assertEqual(budget.call_args.kwargs, {'workspace_id': 'T1', 'app_id': 'APRIVATE'})
        with self.assertRaises(AuthorizationConfigurationError):
            expand_authorization_page({**payload, 'api_app_id': 'AUNKNOWN'})

    def test_owner_and_bot_requests_do_not_charge_the_other_apps_budget(self):
        with patch('integrations.services.message_sync.slack_client.admit_request') as admit:
            for app in ['APUBLIC', user_app_id()]:
                budgeted_client(MagicMock(), workspace_id='T1', app_id=app).api_call('conversations.history')
        self.assertEqual([call.kwargs['app_id'] for call in admit.call_args_list], ['APUBLIC', 'APRIVATE'])
