"""The one-off public bridge workflow cannot target an arbitrary room."""

from pathlib import Path
import subprocess
import unittest


WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/community-bridge-tech-volunteers.yml"
).read_text()
DEPLOY_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml"
).read_text()


class TechVolunteersBridgeWorkflowTests(unittest.TestCase):
    def test_contract_runs_in_deployment_checks(self):
        self.assertIn("tests.test_tech_volunteers_bridge_workflow", DEPLOY_WORKFLOW)

    def test_dispatch_has_only_fixed_modes_and_relay_attestation(self):
        inputs = WORKFLOW.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(
            [line.strip() for line in inputs.splitlines()
             if line.startswith("      ") and not line.startswith("       ") and line.endswith(":")],
            ["mode:", "confirm_relay_ready:"],
        )
        for mode in ("inspect", "stage", "enable"):
            self.assertIn(f"          - {mode}\n", inputs)
        self.assertIn('[[ "$CONFIRM_RELAY_READY" != "true" ]]', WORKFLOW)
        self.assertNotIn("${{ inputs.slack_channel_id }}", WORKFLOW)
        self.assertNotIn("${{ inputs.destination_channel_id }}", WORKFLOW)

    def test_mapping_is_fixed_and_stage_is_disabled(self):
        for literal in (
            "--slack-workspace-id T05N9C1QSJC",
            "--slack-channel-id C0BS0J2Q3M1",
            "--slack-channel-name tech_volunteers",
            "--destination-platform buzz",
            "--destination-channel-id da406415-80c3-5a53-82a9-3d200597b856",
            "--destination-channel-name tech_volunteers",
        ):
            self.assertIn(literal, WORKFLOW)
        self.assertNotIn("--destination-workspace-id", WORKFLOW)
        self.assertIn('if [[ "$mode" == "stage" ]]; then\n              args+=(--disabled)', WORKFLOW)
        self.assertIn("docker compose exec -T web python manage.py migrate --check --noinput", WORKFLOW)
        self.assertIn('if mapping is None or mapping.enabled:', WORKFLOW)

    def test_enable_checks_public_bot_membership_and_uses_existing_ssh_secret(self):
        for guard in (
            'client.auth_test().get("team_id") != "T05N9C1QSJC"',
            'channel.get("is_channel") is not True',
            'channel.get("is_private") is not False',
            'channel.get("is_member") is not True',
            'channel.get("is_ext_shared") is True',
        ):
            self.assertIn(guard, WORKFLOW)
        self.assertIn("ssh-private-key: ${{ secrets.DO_SSH_KEY }}", WORKFLOW)
        self.assertIn("ssh-keyscan -H 209.38.85.60", WORKFLOW)

    def test_remote_shell_syntax(self):
        block = WORKFLOW.split("      - name: Manage fixed public mapping\n", 1)[1]
        script = block.split("        run: |\n", 1)[1]
        script = "\n".join(line[10:] for line in script.splitlines()) + "\n"
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
