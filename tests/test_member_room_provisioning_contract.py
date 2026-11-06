"""Deployment cannot recreate or repoint member-managed Chat rooms."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class MemberRoomProvisioningContractTests(unittest.TestCase):
    def test_deploy_retains_worker_without_manual_channel_upsert(self):
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertNotIn("upsert_community_bridge_channel", deploy)
        self.assertNotIn("upsert_discord_bridge_channel", deploy)
        self.assertIn("bridge_runtime_services", deploy)
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        for legacy_key in ("SLACK_BRIDGE_CHANNEL_ID", "SLACK_BRIDGE_CHANNEL_NAME",
                           "BUZZ_BRIDGE_DESTINATION_WORKSPACE_ID", "BUZZ_BRIDGE_DESTINATION_CHANNEL_ID",
                           "BUZZ_BRIDGE_DESTINATION_CHANNEL_NAME"):
            self.assertNotIn(legacy_key, deploy)
            self.assertNotIn(legacy_key, workflow)
        self.assertFalse((ROOT / ".github/workflows/community-bridge-tech-volunteers.yml").exists())
        self.assertFalse((ROOT / "integrations/management/commands/upsert_community_bridge_channel.py").exists())

    def test_member_admin_actions_remain_the_reviewed_override(self):
        admin = (ROOT / "integrations/admin.py").read_text()
        for action in ("mirror_channels", "hold_channels", "exclude_channels", "queue_refresh"):
            self.assertIn(action, admin)
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("tests.test_member_room_provisioning_contract", workflow)

if __name__ == "__main__":
    unittest.main()
