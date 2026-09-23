"""Keep the owner inventory disabled until its schema and sync are ready."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (REPO_ROOT / "deploy.sh").read_text()
WORKFLOW = (REPO_ROOT / ".github/workflows/deploy.yml").read_text()


class SlackOwnerInventoryDeployConfigTests(unittest.TestCase):
    def test_workflow_passes_explicit_flag_and_runs_contract_before_dependencies(self):
        self.assertIn(
            "SLACK_OWNER_INVENTORY_ENABLED: ${{ vars.SLACK_OWNER_INVENTORY_ENABLED || 'false' }}",
            WORKFLOW,
        )
        self.assertLess(
            WORKFLOW.index("tests.test_slack_owner_inventory_deploy_config -v"),
            WORKFLOW.index("pip install -r requirements.txt"),
        )

    def test_flag_is_disabled_before_migration_and_activated_before_runtime_start(self):
        staged = DEPLOY.index('install_remote_env_value SLACK_OWNER_INVENTORY_ENABLED "false"')
        migration = DEPLOY.index("compose_run_web python manage.py migrate --noinput")
        checked = DEPLOY.index("compose_run_web python manage.py migrate --check --noinput")
        postmigrate = DEPLOY.index("compose_run_web python manage.py deploy_postmigrate")
        activated = DEPLOY.index(
            'upsert_env_value SLACK_OWNER_INVENTORY_ENABLED "\\$slack_owner_inventory_enabled"'
        )
        runtime_start = DEPLOY.index('new_runtime_replacement_started=1\n    docker compose up -d')
        self.assertLess(staged, migration)
        self.assertLess(migration, checked)
        self.assertLess(checked, postmigrate)
        self.assertLess(postmigrate, activated)
        self.assertLess(activated, runtime_start)

    def test_failed_deployment_stages_inventory_off_before_recovery(self):
        recovery = DEPLOY.split("restore_runtime_on_error() {", 1)[1].split(
            'echo "⏸️ Pausing all runtime writers', 1
        )[0]
        self.assertIn('upsert_env_value SLACK_OWNER_INVENTORY_ENABLED "false" || true', recovery)
        self.assertLess(
            recovery.index('upsert_env_value SLACK_OWNER_INVENTORY_ENABLED "false" || true'),
            recovery.index('docker compose up -d --force-recreate "\\${runtime_services[@]}" || true'),
        )

    def test_running_web_must_have_reviewed_inventory_flag(self):
        self.assertIn(
            "running_inventory_enabled=\\$(docker compose exec -T web sh -lc ", DEPLOY
        )
        self.assertIn(
            'if [ "\\$running_inventory_enabled" != "$SLACK_OWNER_INVENTORY_ENABLED" ]; then',
            DEPLOY,
        )
        self.assertIn(
            "running_worker_inventory_enabled=\\$(docker compose exec -T bridge-worker sh -lc ",
            DEPLOY,
        )
        self.assertIn('if [ "\\$running_worker_inventory_enabled" != "true" ]; then', DEPLOY)

    def test_managed_value_helper_accepts_only_canonical_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy(
                REPO_ROOT / "scripts/upsert_env_value_from_stdin.sh", root / "scripts"
            )
            env_file = root / ".env"
            env_file.write_text("KEEP_ME=yes\n")
            for value in ("false", "true"):
                with self.subTest(value=value):
                    result = subprocess.run(
                        ["bash", "scripts/upsert_env_value_from_stdin.sh", "SLACK_OWNER_INVENTORY_ENABLED"],
                        cwd=root,
                        input=value,
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        env_file.read_text(),
                        f"KEEP_ME=yes\nSLACK_OWNER_INVENTORY_ENABLED={value}\n",
                    )
            for value in ("yes", "1", "TRUE", "", "true\nfalse"):
                with self.subTest(invalid=value):
                    previous = env_file.read_text()
                    result = subprocess.run(
                        ["bash", "scripts/upsert_env_value_from_stdin.sh", "SLACK_OWNER_INVENTORY_ENABLED"],
                        cwd=root,
                        input=value,
                        text=True,
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(env_file.read_text(), previous)


if __name__ == "__main__":
    unittest.main()
