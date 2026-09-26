"""Deployment configuration must fail before credentials or services change."""
import unittest
from scripts.validate_message_sync_deploy_config import validate


class MessageSyncDeployConfigTests(unittest.TestCase):
    def test_disabled_needs_no_new_credentials(self):
        validate({})

    def test_inventory_requires_explicit_boolean_and_durable_sync(self):
        validate({"SLACK_OWNER_INVENTORY_ENABLED": "false"})
        with self.assertRaisesRegex(ValueError, "SLACK_OWNER_INVENTORY_ENABLED must be true or false"):
            validate({"SLACK_OWNER_INVENTORY_ENABLED": "yes"})
        with self.assertRaisesRegex(ValueError, "requires MESSAGE_SYNC_ENABLED=true"):
            validate({"SLACK_OWNER_INVENTORY_ENABLED": "true"})
        with self.assertRaisesRegex(ValueError, "requires MESSAGE_SYNC_ENABLED=true"):
            validate({"SLACK_OWNER_INVENTORY_ENABLED": "true", "MESSAGE_SYNC_ENABLED": "false"})

    def test_staged_private_callback_requires_secret_before_enabling_sync(self):
        valid = {"MESSAGE_SYNC_SLACK_USER_APP_ID": "APRIVATE",
                 "MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET": "a" * 32}
        validate(valid)
        with self.assertRaises(ValueError):
            validate({**valid, "MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET": ""})

    def test_enabled_requires_app_recipient_authority_and_matching_workspace(self):
        valid = {
            "MESSAGE_SYNC_ENABLED": "true",
            "COMMUNITY_BRIDGE_PRODUCTION_ENABLED": "true",
            "MESSAGE_SYNC_SLACK_APP_ID": "ATEST",
            "MESSAGE_SYNC_SLACK_APP_TOKEN": "xapp-synthetic",
            "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID": "TTEST",
            "SLACK_BRIDGE_WORKSPACE_ID": "TTEST",
            "MESSAGE_SYNC_SLACK_DISTRIBUTION": "restricted",
        }
        validate(valid)
        validate({**valid, "SLACK_OWNER_INVENTORY_ENABLED": "true"})
        private = {**valid, "MESSAGE_SYNC_SLACK_USER_APP_ID": "APRIVATE",
                   "MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET": "a" * 32,
                   "MESSAGE_SYNC_SLACK_USER_APP_TOKEN": "xapp-private-synthetic"}
        validate(private)
        with self.assertRaises(ValueError):
            validate({**private, "MESSAGE_SYNC_SLACK_USER_APP_TOKEN": ""})
        for key, value in [("MESSAGE_SYNC_ENABLED", "typo"),
                           ("COMMUNITY_BRIDGE_PRODUCTION_ENABLED", "false"),
                           ("MESSAGE_SYNC_SLACK_APP_ID", "wrong"),
                           ("MESSAGE_SYNC_SLACK_APP_TOKEN", "xoxb-synthetic-secret"),
                           ("MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "TOTHER"),
                           ("MESSAGE_SYNC_SLACK_DISTRIBUTION", "unlimited")]:
            with self.subTest(key=key), self.assertRaises(ValueError) as error:
                validate({**valid, key: value})
            self.assertNotIn("synthetic-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
