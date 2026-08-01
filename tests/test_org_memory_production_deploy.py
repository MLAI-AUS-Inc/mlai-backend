from pathlib import Path

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]


class OrgMemoryProductionDeployTests(SimpleTestCase):
    def test_admin_brain_deploy_is_explicit_direct_production_and_non_shadow(self):
        deploy = (ROOT / "deploy.sh").read_text()

        required_settings = {
            'ORG_MEMORY_QUERY_API_ENABLED "true"',
            'ORG_MEMORY_PILOT_ORGANIZATION_DOMAIN "mlai.au"',
            'ORG_MEMORY_ENABLED_PROVIDERS "google_drive"',
            'ORG_MEMORY_PUBLICATION_ENABLED "false"',
            'ORG_MEMORY_ACTIONS_ENABLED "false"',
            'ORG_MEMORY_ACTION_LINEAR_EXECUTION_ENABLED "false"',
            'ORG_MEMORY_SELECTOR_EXPORT_ENABLED "false"',
            'ORG_MEMORY_SELECTOR_SHADOW_ENABLED "false"',
        }
        for setting in required_settings:
            self.assertIn(f"upsert_env_value {setting}", deploy)

        self.assertIn("stage_org_memory_pilot", deploy)
        self.assertIn("activate_org_memory_pilot", deploy)
        self.assertIn("--environment production", deploy)
        self.assertIn("--require-active", deploy)
        self.assertIn("check_org_memory_pilot_access_matrix", deploy)
        self.assertIn("compose_run_web_with_approval", deploy)
        self.assertIn('ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED="${ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED:-false}"', deploy)
        self.assertIn('upsert_env_value ORG_MEMORY_QUERY_API_ENABLED "false"', deploy)
        self.assertIn('if [ "\\$org_memory_production_deploy_enabled" = "true" ]; then', deploy)
        self.assertNotIn("--environment staging", deploy)
        self.assertGreater(
            deploy.index("activate_org_memory_pilot"),
            deploy.index("configure_firebase_storage_cors"),
        )

    def test_main_deploy_requires_approval_key_and_independent_operators(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

        for secret_name in (
            "ORG_MEMORY_PILOT_ALLOWLIST_KEY_VERSION",
            "ORG_MEMORY_PILOT_ALLOWLIST_HMAC_KEY",
            "ORG_MEMORY_PRODUCTION_APPROVAL_MANIFEST",
            "ORG_MEMORY_PRODUCTION_STAGE_OPERATOR_EMAIL",
            "ORG_MEMORY_PRODUCTION_ACTIVATION_OPERATOR_EMAIL",
        ):
            self.assertIn(f"secrets.{secret_name}", workflow)
        self.assertIn(
            "Admin Brain production staging and activation operators must be distinct",
            workflow,
        )
        self.assertIn(
            "ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED: ${{ vars.ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED || 'false' }}",
            workflow,
        )
        self.assertIn(
            'if [ "$ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED" = "true" ]; then',
            workflow,
        )
