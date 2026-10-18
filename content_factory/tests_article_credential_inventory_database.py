"""Real disposable database coverage; run only in authorized remote CI."""
from unittest.mock import patch

from django.test import TestCase

from .tests_website_connections import WebsiteDatabaseFixture
from .website_article_credentials import PRIVATE_ACTIONS
from .website_models import WebsiteConnectionOperation
from .website_reconciliation import process_website_connection_operations


class ArticleCredentialInventoryDatabaseTests(WebsiteDatabaseFixture, TestCase):
    def test_stopped_read_settles_its_recorded_generation_after_owner_revocation(self):
        from copy import deepcopy
        from datetime import timedelta
        from uuid import uuid4
        from django.utils import timezone
        from . import website_goal as goals, website_article_stopped_publications as reads
        from .website_contract import WebsiteAuthorityError
        original_generation = self.website.generation
        snapshot = {"stop_request_digest": "b" * 64, "source_fence_id": str(uuid4()), "goal_operation_id": str(uuid4()),
            "read_scope": goals.scope_for(self.website, "9"), "current_source_sha": "a" * 40,
            "stop_receipt_digest": "c" * 64, "inventory": {"mutations": {}, "pulls": [42]},
            "provider": {"repository": self.website.github_repo, "repository_id": self.website.repository_id,
                "installation_id": str(self.website.installation_id), "branch": self.website.branch}}
        row = WebsiteConnectionOperation.objects.create(connection=self.website, generation=original_generation,
            action=reads.ACTION, state="running", idempotency_key="stopped-read-settlement", payload=snapshot,
            receipt={"schema_version": 1, "observation": None, "credential": {"schema_version": 1,
                "mint_state": "issued", "expires_at": (timezone.now() + timedelta(minutes=20)).isoformat(),
                "delivery_possible": True, "revocation_state": "not_attempted", "revocation_attempts": 0}})
        self.website.generation += 1
        self.website.state = "revoked"
        self.website.save(update_fields=["generation", "state"])
        changed = deepcopy(snapshot)
        changed["inventory"]["pulls"] = [43]
        with self.assertRaises(WebsiteAuthorityError):
            reads._settle(self.website, str(row.pk), changed, revocation_state="confirmed", revocation_attempts=1)
        row.refresh_from_db()
        self.assertEqual(row.receipt["credential"]["revocation_state"], "not_attempted")
        with self.settings(ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED=False):
            reads._settle(self.website, str(row.pk), snapshot, revocation_state="confirmed", revocation_attempts=1)
        row.refresh_from_db()
        self.assertEqual(row.generation, original_generation)
        self.assertEqual(row.payload, snapshot)
        self.assertEqual(row.receipt["credential"]["revocation_state"], "confirmed")
        self.assertIsNone(row.receipt["observation"])
        self.assertEqual(row.state, "running")  # retirement never fabricates a publication observation

    def test_pending_private_metadata_never_falls_into_legacy_cleanup_scheduler(self):
        original = []
        for action in PRIVATE_ACTIONS:
            original.append(WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
                action=action, state="pending", idempotency_key="private-scheduler:" + action, payload={}, receipt={}))
        with patch("integrations.http_client.delete", side_effect=AssertionError("No provider revoke for private metadata")), \
                patch("integrations.services.github_app.create_installation_access_token", side_effect=AssertionError("No provider mint for private metadata")):
            result = process_website_connection_operations(connection_id=self.website.pk)
        self.assertEqual(result["processed"], 0)
        for row in original:
            row.refresh_from_db()
            self.assertEqual(row.state, "pending")

    def test_workflow_and_initial_inventory_roll_back_together_before_dispatch(self):
        from copy import deepcopy
        from django.utils import timezone
        from . import website_goal as goals, website_setup_consent as consent
        from .tests_website_setup_consent_unit import answers
        from .website_article_credentials import MARKER, INVENTORY_ACTION
        from .website_connections import contract_for
        from .website_operations import reserve_workflow_operation
        self.website.verified_sha = "a" * 40
        self.website.save(update_fields=["verified_sha"])
        parent = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="prepare", state="running", idempotency_key="credential-rollback-parent", payload={"company_id": "9"})
        goal = goals.new_goal(str(parent.pk), goals.scope_for(self.website, "9"), target="first_article_live", now=timezone.now())
        goal["current_source_sha"] = self.website.verified_sha
        goal["consent"] = consent.seal(goal, answers(), user_id=4, now=timezone.now())
        parent.receipt = {"goal": goal}; parent.save(update_fields=["receipt"])
        request = {**contract_for(self.website), "expected_source_sha": self.website.verified_sha,
            "client_request_id": "articles-first:" + "b" * 64, "first_article_goal_id": str(parent.pk),
            "first_article_goal_operation_id": str(parent.pk)}
        save = WebsiteConnectionOperation.save
        def interrupted(row, *a, **kw):
            if row.action == "workflow" and MARKER in row.payload:
                raise RuntimeError("Synthetic crash after channel creation")
            return save(row, *a, **kw)
        rollout = {"ARTICLES_ARTICLE_SOURCE_REFRESH_ENABLED": True, "CONTENT_FACTORY_ARTICLE_DELIVERY_V1_APPLY_ENABLED": True,
            "ARTICLES_ARTICLE_RECOVERY_ENABLED": True, "ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED": True,
            "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED": True, "ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED": True,
            "ARTICLES_SETUP_CONSENT_ENABLED": True, "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED": True,
            "ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS": [str(self.org.pk)],
            "ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS": [str(self.org.pk)]}
        with self.settings(**rollout):
            with patch.object(WebsiteConnectionOperation, "save", interrupted), self.assertRaisesRegex(RuntimeError, "Synthetic crash"):
                reserve_workflow_operation(self.website, workflow="direct_generate", payload=deepcopy(request))
            self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.website, action="workflow").exists())
            self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=self.website, action=INVENTORY_ACTION).exists())
            restored = reserve_workflow_operation(self.website, workflow="direct_generate", payload=deepcopy(request))
        self.assertIn(MARKER, restored.payload)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(connection=self.website, action=INVENTORY_ACTION).count(), 1)
        inventory = WebsiteConnectionOperation.objects.get(connection=self.website, action=INVENTORY_ACTION)
        self.assertEqual(inventory.payload["schema_version"], 2)
        self.assertEqual(inventory.payload["source_scope"], goal["scope"])

    def test_source_pages_use_real_json_scope_and_uuid_cursor_without_legacy_or_reverted_sha(self):
        from copy import deepcopy
        from uuid import uuid4
        from . import website_goal as goals, website_article_credentials as credentials
        scope = goals.scope_for(self.website, "9")
        goal_id = str(uuid4())
        captured = str(uuid4())
        binding = {"goal": {"goal_id": goal_id}, "source_scope": scope, "source_sha": "a" * 40,
            "runs": [{"operation_id": captured}]}
        def channel(*, revision=None, version=2, operation=None):
            original = {**scope, "configuration_revision": revision or scope["configuration_revision"]}
            identifier = operation or str(uuid4())
            payload = {"schema_version": version, "channel": "website_token_broker", "workflow_operation_id": identifier,
                "goal_id": goal_id, "connection_id": str(self.website.pk), "connection_generation": self.website.generation,
                "repository_id": str(self.website.repository_id), "deletion_epoch": 0, "source_sha": "a" * 40,
                "client_request_id": "articles-first:" + "c" * 64}
            if version == 2:
                payload.update(source_scope=deepcopy(original), source_scope_digest=goals.digest(original))
            return WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
                action=credentials.INVENTORY_ACTION, state="recorded", idempotency_key="source-scope-page:" + identifier,
                payload=payload, receipt={"schema_version": 1, "from_operation_start": True, "token_count": 0})
        rows = [channel() for _ in range(5)]
        channel(operation=captured)
        channel(version=1)
        channel(revision=scope["configuration_revision"] + 2)
        cursor = None
        observed = []
        for _ in range(3):
            page, cursor = credentials.source_page(self.website.pk, {"binding": binding}, after=cursor)
            self.assertLessEqual(len(page), 2)
            observed.extend(page)
        self.assertEqual(observed, [row.payload["workflow_operation_id"] for row in sorted(rows, key=lambda row: row.pk)])
        self.assertIsNone(cursor)
