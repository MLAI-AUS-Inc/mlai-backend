import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_BINDINGS = (
    '{"T05N9C1QSJC:C0BS0J2Q3M1":{'
    '"display_name":"MLAI_TECH Todo",'
    '"team_name":"MLAI_TECH",'
    '"state_name":"Todo",'
    '"linear_team_id":"def24f5e-2990-4e28-9e06-e89db4a09f9f",'
    '"linear_state_id":"f3591a1e-f7a2-4514-9280-000d43ea60e5"}}'
)


class LinearDeploymentWiringTests(unittest.TestCase):
    def validator_result(self, **overrides):
        environment = {
            **os.environ,
            "LINEAR_API_KEY": "linear-test-key-at-least-32-characters",
            "LINEAR_CHANNEL_ISSUE_BINDINGS_JSON": VALID_BINDINGS,
            "LINEAR_CHANNEL_ISSUE_MAX_COMMENTS": "250",
            **overrides,
        }
        return subprocess.run(
            ["python3", "scripts/validate_linear_channel_issue_deploy_config.py"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validator_accepts_production_binding(self):
        result = self.validator_result()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_validator_rejects_invalid_binding_and_comment_limit(self):
        invalid_binding = self.validator_result(
            LINEAR_CHANNEL_ISSUE_BINDINGS_JSON='{"not-a-slack-binding": {}}'
        )
        self.assertNotEqual(invalid_binding.returncode, 0)

        invalid_limit = self.validator_result(LINEAR_CHANNEL_ISSUE_MAX_COMMENTS="0")
        self.assertNotEqual(invalid_limit.returncode, 0)

    def test_validator_requires_distinct_write_key_when_writes_enabled(self):
        missing = self.validator_result(LINEAR_CHANNEL_ISSUE_WRITES_ENABLED="true")
        self.assertNotEqual(missing.returncode, 0)
        shared = self.validator_result(
            LINEAR_CHANNEL_ISSUE_WRITES_ENABLED="true",
            LINEAR_WRITE_API_KEY="linear-test-key-at-least-32-characters",
        )
        self.assertNotEqual(shared.returncode, 0)
        padded_shared = self.validator_result(
            LINEAR_CHANNEL_ISSUE_WRITES_ENABLED="true",
            LINEAR_WRITE_API_KEY="  linear-test-key-at-least-32-characters  ",
        )
        self.assertNotEqual(padded_shared.returncode, 0)
        whitespace_reader = self.validator_result(LINEAR_API_KEY=" " * 40)
        self.assertNotEqual(whitespace_reader.returncode, 0)
        distinct = self.validator_result(
            LINEAR_CHANNEL_ISSUE_WRITES_ENABLED="true",
            LINEAR_WRITE_API_KEY="distinct-write-key-at-least-32-characters",
        )
        self.assertEqual(distinct.returncode, 0, distinct.stderr)

    def test_workflow_passes_github_settings_to_deploy_script(self):
        workflow = (REPO_ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertIn("LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}", workflow)
        self.assertIn("LINEAR_WRITE_API_KEY: ${{ secrets.LINEAR_WRITE_API_KEY }}", workflow)
        self.assertIn(
            "LINEAR_CHANNEL_ISSUE_BINDINGS_JSON: ${{ vars.LINEAR_CHANNEL_ISSUE_BINDINGS_JSON }}",
            workflow,
        )
        self.assertIn(
            "LINEAR_CHANNEL_ISSUE_MAX_COMMENTS: ${{ vars.LINEAR_CHANNEL_ISSUE_MAX_COMMENTS || '250' }}",
            workflow,
        )
        self.assertIn(
            "LINEAR_CHANNEL_ISSUE_WRITES_ENABLED: ${{ vars.LINEAR_CHANNEL_ISSUE_WRITES_ENABLED || 'false' }}",
            workflow,
        )
        self.assertIn("python scripts/validate_linear_channel_issue_deploy_config.py", workflow)

    def test_deploy_installs_all_linear_values_via_stdin_helpers(self):
        deploy = (REPO_ROOT / "deploy.sh").read_text()
        self.assertIn('install_remote_env_secret LINEAR_API_KEY "$LINEAR_API_KEY"', deploy)
        self.assertIn('install_remote_env_secret LINEAR_WRITE_API_KEY "$LINEAR_WRITE_API_KEY"', deploy)
        self.assertIn(
            'install_remote_env_value LINEAR_CHANNEL_ISSUE_BINDINGS_JSON "$LINEAR_CHANNEL_ISSUE_BINDINGS_JSON"',
            deploy,
        )
        self.assertIn(
            'install_remote_env_value LINEAR_CHANNEL_ISSUE_MAX_COMMENTS "$LINEAR_CHANNEL_ISSUE_MAX_COMMENTS"',
            deploy,
        )
        self.assertIn('1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss]|[Oo][Nn])', deploy)
        self.assertIn(
            'install_remote_env_value LINEAR_CHANNEL_ISSUE_WRITES_ENABLED "$linear_channel_writes_enabled_normalized"',
            deploy,
        )

    def test_managed_value_helper_updates_env_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            for script_name in (
                "upsert_env_value_from_stdin.sh",
                "validate_linear_channel_issue_deploy_config.py",
            ):
                shutil.copy(REPO_ROOT / "scripts" / script_name, root / "scripts" / script_name)
            (root / ".env").write_text("KEEP_ME=yes\nLINEAR_CHANNEL_ISSUE_BINDINGS_JSON=old\n")

            subprocess.run(
                ["bash", "scripts/upsert_env_value_from_stdin.sh", "LINEAR_CHANNEL_ISSUE_BINDINGS_JSON"],
                cwd=root,
                input=VALID_BINDINGS,
                text=True,
                check=True,
                capture_output=True,
            )

            env_lines = (root / ".env").read_text().splitlines()
            self.assertIn("KEEP_ME=yes", env_lines)
            self.assertEqual(
                [line for line in env_lines if line.startswith("LINEAR_CHANNEL_ISSUE_BINDINGS_JSON=")],
                [f"LINEAR_CHANNEL_ISSUE_BINDINGS_JSON={VALID_BINDINGS}"],
            )

    def test_secret_helper_accepts_linear_key_and_redacts_it(self):
        secret = "linear-test-key-at-least-32-characters"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(
                REPO_ROOT / "scripts" / "upsert_env_secret_from_stdin.sh",
                root / "scripts" / "upsert_env_secret_from_stdin.sh",
            )

            result = subprocess.run(
                ["bash", "scripts/upsert_env_secret_from_stdin.sh", "LINEAR_API_KEY"],
                cwd=root,
                input=secret,
                text=True,
                check=False,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertEqual((root / ".env").read_text(), f"LINEAR_API_KEY={secret}\n")

    def test_managed_value_helper_rejects_multiline_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(
                REPO_ROOT / "scripts" / "upsert_env_value_from_stdin.sh",
                root / "scripts" / "upsert_env_value_from_stdin.sh",
            )

            result = subprocess.run(
                ["bash", "scripts/upsert_env_value_from_stdin.sh", "LINEAR_CHANNEL_ISSUE_MAX_COMMENTS"],
                cwd=root,
                input="250\nunexpected",
                text=True,
                check=False,
                capture_output=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / ".env").exists())


if __name__ == "__main__":
    unittest.main()
