"""Valley release configuration must not break existing host trust boundaries."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.validate_valley_mcp_deploy_config import validate


ROOT = Path(__file__).resolve().parents[1]


class ValleyMcpDeployConfigTests(unittest.TestCase):
    def test_unset_optional_values_preserve_existing_rollout(self):
        validate({})
        validate({"VALLEY_MCP_ENABLED": "true"})
        validate({"VALLEY_MCP_ENABLED": "true", "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED": "true",
                  "VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN": "synthetic-public-proof-123456"})

    def test_invalid_input_is_rejected_without_echoing_its_value(self):
        for key, value in (("VALLEY_MCP_ENABLED", "yes"),
                           ("COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", "typo"),
                           ("VALLEY_MCP_PUBLIC_BASE_URL", "https://user:sensitive@example.com"),
                           ("VALLEY_MCP_PUBLIC_BASE_URL", "http://api.mlai.au"),
                           ("VALLEY_MCP_PUBLIC_BASE_URL", "https://api.mlai.au/mcp/valley"),
                           ("VALLEY_MCP_PUBLIC_BASE_URL", "https://api.mlai.au?secret=sensitive"),
                           ("VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN", "proof-one\nproof-two")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError) as error:
                validate({key: value})
            self.assertNotIn(value, str(error.exception))
        with self.assertRaisesRegex(ValueError, "requires the startup update gate"):
            validate({"VALLEY_MCP_ENABLED": "true", "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED": "false"})

    def test_host_upsert_preserves_unrelated_settings_and_refuses_proof_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "validate_valley_mcp_deploy_config.py"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            env_file = root / ".env"
            env_file.write_text("REDIS_URL=redis://synthetic\nCOMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=true\n")

            def upsert(key, value):
                return subprocess.run(["bash", str(root / "scripts/upsert_env_value_from_stdin.sh"), key],
                    input=value, text=True, capture_output=True, env={"PATH": os.defpath}, check=False)

            self.assertEqual(upsert("VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN", "synthetic-public-proof-one").returncode, 0)
            self.assertEqual(upsert("VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN", "synthetic-public-proof-one").returncode, 0)
            before = env_file.read_bytes()
            result = upsert("VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN", "synthetic-public-proof-two")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(env_file.read_bytes(), before)
            self.assertNotIn("synthetic-public-proof", result.stdout + result.stderr)
            self.assertNotEqual(upsert("VALLEY_MCP_PUBLIC_BASE_URL", "https://bad.example/?token=sensitive").returncode, 0)
            self.assertEqual(env_file.read_bytes(), before)
            self.assertEqual(upsert("VALLEY_MCP_ENABLED", "true").returncode, 0)
            self.assertIn("REDIS_URL=redis://synthetic\n", env_file.read_text())
            self.assertIn("COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=true\n", env_file.read_text())

    def test_workflow_validates_and_transmits_public_values_before_runtime_replace(self):
        deploy = (ROOT / "deploy.sh").read_text()
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        self.assertLess(deploy.index("python3 scripts/validate_valley_mcp_deploy_config.py"),
                        deploy.index("rsync -avz"))
        for key in ("VALLEY_MCP_ENABLED", "VALLEY_MCP_PUBLIC_BASE_URL",
                    "VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN", "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED"):
            self.assertIn(f"{key}: ${{{{ vars.{key}", workflow)
            self.assertIn(f'install_remote_env_value {key} "${key}"', deploy)
        self.assertIn('if [ -n "$COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED" ]; then', deploy)
        self.assertIn('if [ -n "$VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN" ]; then', deploy)


if __name__ == "__main__":
    unittest.main()
