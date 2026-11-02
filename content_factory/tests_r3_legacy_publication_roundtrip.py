"""Feed the real authenticated CF descriptor unchanged into actual PG admission."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun
from .self_heal_testing import make_p2_org
from .tests_r3_revision_child_admission import FLAGS
from .tests_article_publish_approval_unit import _review_run
from .run_revision_child_admission import prepare_publish_child
from .run_legacy_source import validate_legacy_source
from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, owner_receipt
from .website_models import WebsiteConnection, WebsiteConnectionTarget, WebsiteConnectionOperation


@override_settings(**FLAGS, ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False)
class CompleteLegacyPublicationRoundTripTests(TransactionTestCase):
    def test_exact_full_cf_request_descriptor_seals_a_new_child_without_restamping_parent(self):
        fixture = json.loads((Path(__file__).with_name("testdata") / "r3_cf_legacy_publication_source_complete.json").read_text())
        request = deepcopy(fixture["backend_parent_request"])
        original_bytes = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        org_fixture = make_p2_org(domain=request["domain"])
        org, user, config = org_fixture.organization, org_fixture.owner, org_fixture.config
        payer_id = int(request["article_publish_approval_receipt"]["actor_id"].removeprefix("mlai_user:"))
        payer = get_user_model().objects.filter(pk=payer_id).first()
        if payer is None:
            payer = get_user_model().objects.create_user(pk=payer_id, email="retained-payer@synthetic.example")
        ledger = Ledger.objects.create(pk=int(request["roo_points_ledger_id"]), user=payer, kind="SPEND", source="CONTENT_FACTORY",
            delta=-request["roo_points_cost"], idempotency_key="real-fixture-retained-charge")
        connection = WebsiteConnection.objects.create(pk=request["website_connection_id"], organization=org, authorized_by=user,
            generation=request["connection_generation"], github_repo=request["github_repo"], repository_id=request["repository_id"],
            installation_id="1001", branch=request["branch"], app_root=request["app_root"], verified_sha=request["expected_source_sha"],
            last_verified_at=timezone.now(), capabilities={"publishingReady": True})
        contract = {"route_template": "/articles/{slug}", "contract_digest": "c" * 64, "live_marker": {"value": "d" * 64}}
        WebsiteConnectionTarget.objects.create(connection=connection, generation=connection.generation, target_key="articles",
            source_sha=connection.verified_sha, verified_at=timezone.now(), capabilities={"publishingReady": True}, contract=contract)
        config.website_connection, config.github_repo, config.default_publish_target_id = connection, connection.github_repo, "articles"
        config.save(update_fields=["website_connection", "github_repo", "default_publish_target_id"])
        WebsiteConnectionOperation.objects.create(pk=request["operation_id"], connection=connection, generation=connection.generation,
            action="workflow", state="completed", idempotency_key="retained-source-operation",
            payload={"attempt": request["operation_attempt"], "deletion_epoch": request["deletion_epoch"], "run_id": fixture["source_run_id"]})
        WebsiteConnectionOperation.objects.create(connection=connection, generation=connection.generation, action="deployment-verify",
            state="completed", idempotency_key="fresh-destination-proof", payload={"source_sha": connection.verified_sha, "target_id": "articles"},
            receipt={"status": "passed", "source_sha": connection.verified_sha, "connection_generation": connection.generation,
                "target_id": "articles", "contract_digest": "c" * 64, "artifact_digest": "d" * 64,
                "public_url": f"https://{org.domain}/articles", "checked_at": timezone.now().isoformat()})
        source = ContentFactoryRun.objects.create(run_id=fixture["source_run_id"], organization=org, workflow="article_generation",
            domain=request["domain"], github_repo=request["github_repo"], status="completed", approval_state="approved",
            run_request=deepcopy(request), result=deepcopy(_review_run().result))
        self.assertEqual(validate_legacy_source(source, fixture["legacy_source_v1"]), fixture["legacy_source_v1"])
        before = ContentFactoryRun.objects.values().get(pk=source.pk)
        money_before = list(Ledger.objects.values())
        def descriptor_get(url, **kwargs):
            self.assertTrue(url.endswith(f"/api/runs/{source.run_id}/continuation-source"))
            return Mock(status_code=200, headers={}, iter_content=Mock(return_value=iter([
                json.dumps(fixture["legacy_source_v1"]).encode()])))
        payload = {"auto_merge": False}
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                patch("content_factory.vibe_marketing_views.http_client.get", side_effect=descriptor_get), \
                patch("content_factory.article_review_views.remote_review", return_value=deepcopy(fixture["article_review"])), \
                patch("content_factory.website_connections.verify_repository_head", return_value=connection.verified_sha), \
                patch("content_factory.website_connections.verify_repository_native_target", return_value=None):
            child = prepare_publish_child(run=source, user=user, payload=payload, action="promote-bundle")
        source.refresh_from_db()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=source.pk), before)
        self.assertEqual(json.dumps(source.run_request, sort_keys=True, separators=(",", ":")).encode(), original_bytes)
        self.assertIsNone(owner_receipt(source))
        self.assertIsNotNone(owner_receipt(child))
        self.assertNotIn(WIRE_REQUEST_KEY, payload)
        self.assertNotIn(WIRE_EXECUTION_KEY, payload)
        self.assertEqual(payload["child_dispatch_v1"]["legacy_source_v1"], fixture["legacy_source_v1"])
        self.assertEqual(child.run_request["legacy_source_v1"], fixture["legacy_source_v1"])
        self.assertEqual(child.run_request["roo_points_ledger_id"], str(ledger.pk))
        self.assertEqual(list(Ledger.objects.values()), money_before)
        self.assertNotEqual(fixture["source_request"], source.run_request, "CF defaults must not be reconstructed from backend fields")
        if os.environ.get("R3_COMPLETE_PAIRED_PUBLICATION_OUTPUT"):
            Path(os.environ["R3_COMPLETE_PAIRED_PUBLICATION_OUTPUT"]).write_text(json.dumps({
                "schema_version": 1, "cf_fixture": fixture, "backend_parent_request": source.run_request,
                "dispatch": payload, "child_request": child.run_request}, sort_keys=True, indent=2) + "\n")
