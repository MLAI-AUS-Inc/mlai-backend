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


class MaekSafetyConfigTests(unittest.TestCase):
    # This is the reviewed rollout contract, independent of runtime imports.
    defaults = {
        "AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT": "false",
        "AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES": "1440",
        "AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP": "200",
        "AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE": "Australia/Melbourne",
        "AGENT_RUNTIME_NATIVE_BACKLOG_REPLAY": "false",
        "AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY": "24h",
        "AGENT_RUNTIME_NATIVE_DURABLE_WATCH": "false",
        "AGENT_RUNTIME_NATIVE_DENIAL_GRACE_SECONDS": "600",
        "AGENT_RUNTIME_NATIVE_UNIFIED_GRACE_SECONDS": "86400",
        "AGENT_RUNTIME_NATIVE_LAPSE_ALERTS": "false",
        "AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED": "false",
        "MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED": "false",
        "MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS": "0",
    }

    def test_optional_flags_can_stay_off_while_pilot_is_on(self):
        from scripts.validate_native_agent_deploy_config import OPTIONAL_FLAGS
        self.assertTrue(FLAGS.isdisjoint(OPTIONAL_FLAGS))
        validate({**active(), **self.defaults})
        validate({**active(), "AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT": "false"})
        for key in OPTIONAL_FLAGS:
            for value in ("true", "false"):
                with self.subTest(key=key, value=value):
                    validate_single(key, value)
            for value in ("yes", "TRUE", "1", "false\nINJECT=true"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_single(key, value)

    def test_integer_enum_and_timezone_contracts(self):
        from scripts.validate_native_agent_deploy_config import BOUNDED_INTEGERS
        for key, (low, high) in BOUNDED_INTEGERS.items():
            for value in (str(low), str(high), self.defaults[key]):
                validate_single(key, value)
            for value in (str(low - 1), str(high + 1), "1.5", "yes", "1\nINJECT=true"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate_single(key, value)
        for value in ("24h", "grant"):
            validate_single("AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY", value)
        for value in ("forever", "24H", "null"):
            with self.assertRaises(ValueError):
                validate_single("AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY", value)
        for value in ("Australia/Melbourne", "UTC", "Etc/GMT+1"):
            validate_single("AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE", value)
        for value in ("../UTC", "/UTC", "Australia//Melbourne", "Australia/Melbourne\nX=true", "Australia Melbourne"):
            with self.assertRaises(ValueError):
                validate_single("AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE", value)

    def test_all_defaults_reach_every_deployment_surface(self):
        deploy = (ROOT / "deploy.sh").read_text()
        loop = deploy.split("for native_flag in ", 1)[1].split("; do", 1)[0]
        upsert = (ROOT / "scripts/upsert_env_value_from_stdin.sh").read_text()
        for key, default in self.defaults.items():
            with self.subTest(key=key):
                self.assertIn(key + ": ${{ vars." + key + " || '" + default + "' }}", (ROOT / ".github/workflows/deploy.yml").read_text())
                self.assertIn(key + '="${' + key + ':-' + default + '}"', deploy)
                self.assertIn("export " + key, deploy)
                self.assertIn(key, loop)
                self.assertGreaterEqual(upsert.count(key), 2)
                self.assertIn(key, (ROOT / "mlai/settings.py").read_text())
                self.assertIn(key + "=" + default, (ROOT / ".env.example").read_text())
                self.assertIn(key, (ROOT / "docs/mlai-mcp-native-dot.md").read_text())
        worker = (ROOT / "docker-compose.yml").read_text().split("  native-agent-worker:\n", 1)[1].split("\n  db:", 1)[0]
        self.assertIn("--health-file /tmp/native-agent-heartbeat", worker)
        self.assertIn("healthcheck:", worker)
        self.assertIn("<= 300", worker)

    def test_optional_stdin_values_are_atomic_and_route_to_native_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "validate_native_agent_deploy_config.py"):
                shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
            for key, default in self.defaults.items():
                with self.subTest(key=key):
                    env_file = root / ".env"
                    env_file.write_text("KEEP_ME=yes\n")
                    command = ["bash", str(root / "scripts/upsert_env_value_from_stdin.sh"), key]
                    result = subprocess.run(command, input=default, text=True, capture_output=True,
                                            env={"PATH": os.defpath}, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(key + "=" + default + "\n", env_file.read_text())
                    before = env_file.read_bytes()
                    result = subprocess.run(command, input="invalid value\nINJECT=true", text=True,
                                            capture_output=True, env={"PATH": os.defpath}, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(before, env_file.read_bytes())

class CommunityDeployConfigTests(unittest.TestCase):
    def test_community_is_off_by_default_and_requires_gateway_and_host_provider(self):
        validate({})
        with self.assertRaises(ValueError):
            validate({"AGENT_RUNTIME_COMMUNITY_ENABLED": "true"})
        enabled = {**active(), "AGENT_RUNTIME_COMMUNITY_ENABLED": "true"}
        validate(enabled)
        with self.assertRaises(ValueError):
            validate(enabled, check_provider=True)
        configured = {**enabled, "AGENT_RUNTIME_MODEL_BASE_URL": "https://model.example/v1",
            "AGENT_RUNTIME_MODEL_API_KEY": "synthetic-only-provider-key", "AGENT_RUNTIME_MODEL_NAME": "test-model",
            "AGENT_RUNTIME_MODEL_INPUT_MICROUNITS_PER_MILLION": "1000000",
            "AGENT_RUNTIME_MODEL_OUTPUT_MICROUNITS_PER_MILLION": "1000000"}
        validate(configured, check_provider=True)
        for change in ({"AGENT_RUNTIME_MODEL_BASE_URL": "http://model.example"},
                       {"AGENT_RUNTIME_MODEL_INPUT_MICROUNITS_PER_MILLION": "0"},
                       {"AGENT_RUNTIME_MODEL_API_KEY": "invalid\nkey"}):
            with self.assertRaises(ValueError):
                validate({**configured, **change}, check_provider=True)

    def test_community_worker_is_separate_and_in_shutdown_inventory(self):
        compose = (ROOT / "docker-compose.yml").read_text().split("  community-agent-worker:\n", 1)[1].split("\n  db:", 1)[0]
        self.assertIn("profiles: [community-agent]", compose)
        self.assertIn("run_community_agent", compose)
        self.assertIn("migrate --check --noinput", compose)
        self.assertNotIn("migrate --noinput", compose)
        inventory = (ROOT / "scripts/runtime-services.sh").read_text()
        self.assertIn('"${community_agent_runtime_services[@]}"', inventory.split("all_runtime_writer_services=(", 1)[1])
        self.assertIn('AGENT_RUNTIME_COMMUNITY_ENABLED=false', (ROOT / ".env.example").read_text())
        self.assertIn("scripts/validate_native_agent_deploy_config.py --provider", (ROOT / "deploy.sh").read_text())
