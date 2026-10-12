"""Bounded pilot rollout and secret transport contracts, without infrastructure."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.validate_native_agent_deploy_config import FLAGS, validate, validate_single

ROOT = Path(__file__).resolve().parents[1]
COMMUNITIES = json.dumps({"9277de1c-c526-47cb-876c-7402b087b3d8": {
    "relay_url": "https://chat.mlai.au", "channel_ids": ["3cf76a33-f55c-4b7f-b6b8-a8fa3dec5d64"]}})


def active():
    return {**{key: "true" for key in FLAGS}, "MLAI_MCP_ENABLED": "true",
        "AGENT_RUNTIME_NATIVE_COMMUNITIES": COMMUNITIES, "AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT": "1",
        "AGENT_RUNTIME_DOT_SETUP_URL": "https://provider.example/actual-draft",
        "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY": "1" * 64,
        "COMMUNITY_CHAT_ROLE_SERVICE_TOKEN": "synthetic-independent-relay-role-credential",
        "AGENT_RUNTIME_NATIVE_REGISTRY_KEY": "synthetic-native-registry-credential-at-least-32-bytes"}


class NativeAgentDeployConfigTests(unittest.TestCase):
    def test_dormant_configuration_and_complete_explicit_activation(self):
        validate({})
        validate({"AGENT_RUNTIME_NATIVE_COMMUNITIES": "{}"})
        validate(active())
        for key in (FLAGS | {"MLAI_MCP_ENABLED", "AGENT_RUNTIME_NATIVE_COMMUNITIES",
                "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", "AGENT_RUNTIME_NATIVE_REGISTRY_KEY", "AGENT_RUNTIME_DOT_SETUP_URL"}) - {"MLAI_MCP_NATIVE_AGENTS_ENABLED"}:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**active(), key: ""})
        with self.assertRaises(ValueError):
            validate({**active(), "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED": "false"})
        with self.assertRaises(ValueError):
            validate({**active(), "AGENT_RUNTIME_NATIVE_COMMUNITIES": "{}"})

    def test_invalid_payloads_and_reused_credentials_never_echo_values(self):
        cases = (("AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", "sensitive-private-key"),
            ("AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", "0" * 64),
            ("AGENT_RUNTIME_NATIVE_REGISTRY_KEY", "sensitive-short"),
            ("COMMUNITY_CHAT_ROLE_SERVICE_TOKEN", "sensitive spaced credential" + "x" * 32),
            ("AGENT_RUNTIME_DOT_SETUP_URL", "https://provider.example:sensitive/"),
            ("AGENT_RUNTIME_DOT_SETUP_URL", "https://name:sensitive@provider.example/"),
            ("AGENT_RUNTIME_DOT_SETUP_URL", "https://provider.example/?secret=sensitive"),
            ("AGENT_RUNTIME_NATIVE_COMMUNITIES", COMMUNITIES.replace("https://chat.mlai.au", "http://name:sensitive@relay.example")),
            ("AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT", "20"))
        for key, value in cases:
            with self.subTest(key=key), self.assertRaises(ValueError) as error:
                validate_single(key, value)
            self.assertNotIn("sensitive", str(error.exception))
        with self.assertRaises(ValueError):
            validate({**active(), "ROO_API_KEY": active()["AGENT_RUNTIME_NATIVE_REGISTRY_KEY"]})
        with self.assertRaises(ValueError):
            validate({**active(), "COMMUNITY_CHAT_ROLE_SERVICE_TOKEN": active()["AGENT_RUNTIME_NATIVE_REGISTRY_KEY"]})

    def test_native_stdin_writes_validate_before_atomic_replace_and_do_not_log_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "upsert_env_secret_from_stdin.sh", "validate_native_agent_deploy_config.py"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            env_file = root / ".env"
            env_file.write_text("UNRELATED_SETTING=preserved\n")
            def upsert(script, key, value):
                return subprocess.run(["bash", str(root / "scripts" / script), key], input=value,
                    text=True, capture_output=True, env={"PATH": os.defpath}, check=False)
            valid = upsert("upsert_env_secret_from_stdin.sh", "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", "1" * 64)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertNotIn("1" * 64, valid.stdout + valid.stderr)
            before = env_file.read_bytes()
            invalid = upsert("upsert_env_secret_from_stdin.sh", "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", "sensitive" * 9)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertNotIn("sensitive", invalid.stdout + invalid.stderr)
            self.assertEqual(env_file.read_bytes(), before)
            self.assertEqual(upsert("upsert_env_value_from_stdin.sh", "AGENT_RUNTIME_NATIVE_COMMUNITIES", COMMUNITIES).returncode, 0)
            self.assertIn("UNRELATED_SETTING=preserved\n", env_file.read_text())

    def test_native_worker_is_explicit_and_always_in_writer_pause_inventory(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        worker = compose.split("  native-agent-worker:\n", 1)[1].split("\n  db:", 1)[0]
        self.assertIn("profiles: [native-agent]", worker)
        self.assertIn("migrate --check --noinput", worker)
        self.assertNotIn("migrate --noinput", worker)
        self.assertIn("run_agent_scheduler --native --webhooks", worker)
        runtime = (ROOT / "scripts/runtime-services.sh").read_text()
        self.assertIn("native_agent_runtime_services=(native-agent-worker)", runtime)
        self.assertIn('"${native_agent_runtime_services[@]}"', runtime.split("all_runtime_writer_services=(", 1)[1])
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertLess(deploy.index("python3 scripts/validate_native_agent_deploy_config.py"), deploy.index("rsync -avz"))
        self.assertLess(deploy.index("compose_run_web python scripts/validate_native_agent_deploy_config.py"),
                        deploy.index('echo "⏸️ Pausing all runtime writers'))
        self.assertIn('read_env_value MLAI_MCP_NATIVE_AGENTS_ENABLED', deploy)
        self.assertIn('docker compose stop native-agent-worker', deploy)
        workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
        for key in active():
            self.assertIn(key, workflow)
