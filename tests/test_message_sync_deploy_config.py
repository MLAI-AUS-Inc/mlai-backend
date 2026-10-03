"""Deployment configuration must fail before credentials or services change."""
import unittest
from pathlib import Path
import shutil
import subprocess
import tempfile
from scripts.validate_message_sync_deploy_config import validate


class MessageSyncDeployConfigTests(unittest.TestCase):
    def test_quiet_backoff_deploys_disabled_by_default_and_upserts_safely(self):
        root = Path(__file__).resolve().parents[1]
        key = "MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED"
        workflow = (root / ".github/workflows/deploy.yml").read_text()
        deploy = (root / "deploy.sh").read_text()
        self.assertIn(key + ": ${{ vars." + key + " || 'false' }}", workflow)
        self.assertIn('install_remote_env_value ' + key + ' "$' + key + '"', deploy)
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            (sandbox / "scripts").mkdir()
            shutil.copy(root / "scripts/upsert_env_value_from_stdin.sh", sandbox / "scripts")
            env_file = sandbox / ".env"
            env_file.write_text("KEEP_ME=yes\n")
            for value in ("true", "false", "yes", "TRUE", "true\nfalse"):
                with self.subTest(value=value):
                    before = env_file.read_text()
                    result = subprocess.run(
                        ["bash", "scripts/upsert_env_value_from_stdin.sh", key],
                        cwd=sandbox, input=value, text=True, capture_output=True, timeout=5,
                    )
                    if value in {"true", "false"}:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(env_file.read_text(), f"KEEP_ME=yes\n{key}={value}\n")
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(env_file.read_text(), before)

    def test_disabled_needs_no_new_credentials(self):
        validate({})

    def test_quiet_backoff_defaults_off_and_requires_explicit_valid_sync(self):
        validate({"MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED": "false"})
        for value in ("yes", "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "QUIET_HEAD_BACKOFF"):
                validate({"MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED": value})

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
        validate({**valid, "MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED": "true"})
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
