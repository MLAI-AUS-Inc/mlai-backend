"""Database-free acceptance checks for shared journey and inventory contracts."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import resolve

from .website_discovery import discovery_snapshot, normalize_scan_callback
from .website_journey import project_journey
from .website_contract import WebsiteAuthorityError, evidence_digest


def blocked_fixture():
    """Synthetic rollout-paused MLAI-shaped evidence without production values."""
    return project_journey(company_id="42", domain="example.test", website={
        "connectionId": "b761b209-8673-4843-9a48-90b3177bde26", "connectionGeneration": 3,
        "configurationVersion": 5, "status": "connected", "repositoryId": 123,
        "githubRepo": "example/site", "branch": "main", "appRoot": "", "writePolicy": {"allowed": False, "mode": "canary"}},
        capabilities={"repositoryAccessVerified": True, "canGenerateArticle": False, "reasonCode": "website_writes_paused", "reason": "Website changes are temporarily paused."},
        discovery={"version": 1, "complete": True, "sourceSha": "a" * 40, "candidates": [], "publishReady": False})


class WebsiteJourneyContractTests(SimpleTestCase):
    def test_account_is_complete_before_repository_selection(self):
        result = project_journey(company_id="42", domain="", capabilities={"accountAccessVerified": True, "repositoryAccessVerified": False})
        self.assertEqual(result["prerequisites"]["account"]["status"], "complete")
        self.assertEqual(result["prerequisites"]["repository"]["status"], "needs_action")
        self.assertIn("github-revoke", result["allowedActions"])
        self.assertTrue(result["capabilities"]["canGeneratePortableDraft"])

    def test_read_only_repository_can_scan_but_cannot_prepare(self):
        result = project_journey(company_id="42", domain="example.test", website={"status": "connected", "repositoryId": 123, "writePolicy": {"allowed": True}},
            capabilities={"accountAccessVerified": True, "repositoryAccessVerified": True, "repositoryWriteVerified": False})
        self.assertTrue(result["capabilities"]["canScan"])
        self.assertFalse(result["capabilities"]["canPrepare"])

    def test_newly_certified_source_does_not_reuse_old_inventory_fact(self):
        result = project_journey(company_id="42", domain="example.test", website={"verifiedSha": "b" * 40},
            discovery={"complete": True, "sourceSha": "a" * 40})
        self.assertEqual(result["prerequisites"]["inventory"]["status"], "stale")
        self.assertEqual(result["prerequisites"]["inventory"]["reasonCode"], "scan_stale")
        self.assertEqual(result["repository"]["sourceSha"], "b" * 40)

    def test_stale_build_proof_preserves_installed_fact_and_requests_ci_refresh(self):
        result = project_journey(company_id="42", domain="example.test", website={"status": "connected", "repositoryId": 123,
            "verifiedSha": "a" * 40, "writePolicy": {"allowed": True}},
            capabilities={"repositoryAccessVerified": True, "canGenerateArticle": False, "reasonCode": "verification_stale"},
            discovery={"complete": True, "sourceSha": "a" * 40},
            proof={"integrationVerified": True, "buildVerified": False, "verificationStale": True})
        self.assertEqual(result["prerequisites"]["integration"]["status"], "complete")
        self.assertEqual(result["prerequisites"]["verification"]["status"], "stale")
        self.assertEqual(result["nextAction"]["id"], "ci-attestation")
        self.assertNotIn("verify", result["allowedActions"])

    def test_proof_age_source_and_policy_are_independent(self):
        from datetime import timedelta
        from django.utils import timezone
        from .website_journey import build_proof_fresh
        now = timezone.now()
        connection = SimpleNamespace(verified_sha="a" * 40, branch="main", last_verified_at=now)
        target = SimpleNamespace(source_sha="a" * 40, verified_at=now)
        self.assertTrue(build_proof_fresh(connection, target, {"reasonCode": "website_writes_paused"}, now=now))
        target.verified_at = now - timedelta(days=8)
        self.assertFalse(build_proof_fresh(connection, target, {}, now=now))
        target.verified_at = now
        self.assertFalse(build_proof_fresh(connection, target, {"repositorySourceSha": "b" * 40}, now=now))
        target.verified_at, target.updated_at = None, now
        target.contract = {"verification": {"status": "preview_verified"}}
        self.assertTrue(build_proof_fresh(connection, target, {}, now=now))

    def test_observed_new_head_marks_inventory_stale_independently(self):
        result = project_journey(company_id="42", domain="example.test", website={"verifiedSha": "a" * 40},
            capabilities={"repositorySourceSha": "b" * 40}, discovery={"complete": True, "sourceSha": "a" * 40})
        self.assertEqual(result["prerequisites"]["inventory"]["status"], "stale")
        self.assertEqual(result["repository"]["observedSourceSha"], "b" * 40)

    def test_unknown_framework_never_advertises_automatic_native_support(self):
        result = project_journey(company_id="42", domain="example.test", website={"status": "connected", "appRoot": ""})
        self.assertFalse(result["support"]["native"]["available"])
        self.assertFalse(result["support"]["native"]["certified"])

    def test_custom_proof_certifies_only_the_custom_support_path(self):
        result = project_journey(company_id="42", domain="example.test", website={"status": "connected"},
            target={"adapter": "custom_contract_v1"}, proof={"buildVerified": True}, capabilities={"canGenerateArticle": False})
        self.assertTrue(result["support"]["customContract"]["certified"])
        self.assertFalse(result["support"]["native"]["certified"])

    def test_discovery_support_paths_preserve_bounded_framework_evidence(self):
        discovery = discovery_snapshot({"repository_discovery": {"discovery_complete": True, "source_sha": "a" * 40,
            "support_level": "supported", "support_paths": [{"id": "native", "status": "available", "reason": "Framework detected", "private_key": "not public"}]}})
        self.assertNotIn("private_key", discovery["supportPaths"][0])
        result = project_journey(company_id="42", domain="example.test", discovery=discovery)
        self.assertTrue(result["support"]["native"]["available"])
        self.assertFalse(result["support"]["native"]["certified"])

    def test_account_probe_has_finite_cache_and_does_not_require_repository(self):
        from .website_github_access import verify_account_access
        from django.core.cache import cache
        cache.clear()
        user = SimpleNamespace(pk=123)
        with patch("integrations.services.github_app.probe_installation_liveness", return_value="live") as provider:
            first = verify_account_access(user, installations=[SimpleNamespace(installation_id="45")])
            second = verify_account_access(user, installations=[SimpleNamespace(installation_id="45")])
        self.assertTrue(first["verified"])
        self.assertEqual(first, second)
        provider.assert_called_once_with("45")
    def test_shared_fixture_is_actual_projection(self):
        fixture = Path(__file__).parent / "fixtures" / "website_journey_v2.json"
        self.assertEqual(json.loads(fixture.read_text()), blocked_fixture())

    def test_independent_access_inventory_and_rollout_facts(self):
        journey = blocked_fixture()
        self.assertEqual(journey["prerequisites"]["account"]["status"], "complete")
        self.assertEqual(journey["prerequisites"]["inventory"]["status"], "complete")
        self.assertEqual(journey["prerequisites"]["integration"]["reasonCode"], "website_writes_paused")
        self.assertFalse(journey["capabilities"]["canPublishArticle"])
        self.assertTrue(journey["capabilities"]["canGeneratePortableDraft"])
        self.assertNotIn("setup", journey["allowedActions"])
        self.assertIn("purge", journey["allowedActions"])

    def test_target_readiness_needs_exact_live_deployment_proof(self):
        website = {"status": "connected", "verifiedSha": "a" * 40, "connectionGeneration": 2, "writePolicy": {"allowed": True}}
        caps = {"canGenerateArticle": True, "repositoryAccessVerified": True}
        partial = project_journey(company_id="42", domain="example.test", website=website, capabilities=caps)
        self.assertTrue(partial["capabilities"]["canOpenPublicationPr"])
        self.assertFalse(partial["capabilities"]["canPublishArticle"])
        proof = {"status": "passed", "source_sha": "a" * 40, "connection_generation": 2, "target_id": "native", "public_url": "https://example.test/articles", "checked_at": "2026-10-05T00:00:00Z"}
        ready = project_journey(company_id="42", domain="example.test", website=website, capabilities=caps, target={"key": "native", "contract": {}, "deployment_receipt": proof})
        self.assertTrue(ready["capabilities"]["canPublishArticle"])
        proof["source_sha"] = "b" * 40
        self.assertFalse(project_journey(company_id="42", domain="example.test", website=website, capabilities=caps, target={"key": "native", "contract": {}, "deployment_receipt": proof})["capabilities"]["canPublishArticle"])

    def test_write_pause_preserves_exact_deployment_fact_without_publication_grant(self):
        website = {"status": "connected", "verifiedSha": "a" * 40, "connectionGeneration": 2, "writePolicy": {"allowed": False}}
        receipt = {"status": "passed", "source_sha": "a" * 40, "connection_generation": 2, "target_id": "native",
            "public_url": "https://example.test/articles", "checked_at": "2026-10-05T00:00:00Z"}
        result = project_journey(company_id="42", domain="example.test", website=website,
            capabilities={"canGenerateArticle": False, "reasonCode": "website_writes_paused"},
            proof={"integrationVerified": True, "buildVerified": True}, target={"key": "native", "deployment_receipt": receipt})
        self.assertEqual(result["prerequisites"]["deployment"]["status"], "complete")
        self.assertFalse(result["capabilities"]["canPublishArticle"])

    def test_nested_inventory_candidates_display_without_synthesizing_readiness(self):
        payload = {"result": {"repository_inventory": {"discovery_complete": True, "source_sha": "a" * 40,
            "article_surface_resolution": {"ranked_candidates": {"listing_surface_candidates": [{"kind": "native", "path_or_locator": "app/articles/page.tsx", "metadata": {"route_path": "/articles"}}]}}}}}
        normalized = normalize_scan_callback(payload)
        self.assertEqual(normalized["detected_candidates"][0]["route"], "/articles")
        self.assertTrue(discovery_snapshot(payload)["complete"])
        self.assertFalse(discovery_snapshot(payload)["publishReady"])
        self.assertNotIn("publish_targets", normalized)

    def test_lifecycle_facade_and_receipt_routes_resolve(self):
        prefix = "/api/v1/my-startup/vibe-marketing/website-connection"
        for suffix in ("", "/", "/disconnect", "/disconnect/", "/purge", "/cancel-operation", "/operations/b761b209-8673-4843-9a48-90b3177bde26"):
            self.assertTrue(resolve(prefix + suffix).func.view_class.__name__.__contains__("WebsiteConnection"))

    def test_account_probe_runs_when_integration_is_blocked(self):
        from .vibe_marketing_views import _article_capabilities_for_context
        config = SimpleNamespace(github_repo="example/site", website_connection=object())
        with patch("content_factory.vibe_marketing_views._github_account_for_context", return_value={"saved": True, "owned": True}), \
             patch("content_factory.vibe_marketing_views._article_system_setup_gate", return_value={}), \
             patch("content_factory.vibe_marketing_views.resolve_article_system", return_value={}), \
             patch("content_factory.vibe_marketing_views.integration_evidence", return_value={"verified": False, "reasonCode": "website_writes_paused"}), \
             patch("content_factory.vibe_marketing_views._verify_github_repository_access", return_value={"verified": True}) as probe:
            result = _article_capabilities_for_context(SimpleNamespace(organization=SimpleNamespace(domain="example.test")), config, latest_runs=[])
        probe.assert_called_once()
        self.assertEqual(result["accountStatus"], "connected")
        self.assertEqual(result["reasonCode"], "website_writes_paused")


class ArticlePortableExportTests(SimpleTestCase):
    def review(self):
        return {"revision": "current-saved-revision", "previewPending": False, "latestRunId": None,
            "articleExport": {"version": 1, "status": "ready", "runId": "article-1", "revision": "current-saved-revision", "format": "markdown",
                "markdown": "# Saved edited title\n\nThe owner's exact saved edit.",
                "metadata": {"title": "Saved edited title", "slug": "saved-edit", "description": "Saved description", "privatePrompt": "must not export"},
                "media": [{"url": "https://assets.example.test/hero.png", "alt": "Reviewed image", "caption": "Saved caption", "api_key": "must not export"}]}}

    def test_exact_saved_export_allowlists_metadata_and_assets(self):
        from .article_export import article_export
        review = self.review()
        exported = article_export(review, run_id="article-1")
        self.assertEqual(exported["status"], "ready")
        self.assertEqual(exported["markdown"], review["articleExport"]["markdown"])
        self.assertEqual(exported["metadata"], {"title": "Saved edited title", "slug": "saved-edit", "description": "Saved description"})
        self.assertEqual(set(exported["media"][0]), {"url", "alt", "caption"})

    def test_pending_superseded_foreign_revision_and_unknown_version_deny_export(self):
        from .article_export import article_export
        for mutate in (lambda row: row.update(previewPending=True), lambda row: row.update(latestRunId="newer"),
                lambda row: row["articleExport"].update(runId="foreign"), lambda row: row["articleExport"].update(revision="stale"),
                lambda row: row["articleExport"].update(version=2)):
            review = self.review()
            mutate(review)
            result = article_export(review, run_id="article-1")
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["markdown"], "")
        self.assertEqual(article_export(self.review(), run_id="article-1", latest_run_id="ready-child")["reasonCode"], "revision_superseded")

    def test_credential_urls_and_missing_snapshot_never_get_result_fallback(self):
        from .article_export import article_export
        review = self.review()
        review["articleExport"]["media"] += [{"url": "https://credential:private@assets.example.test/image.png"}, {"url": "file:///private/image.png"}]
        self.assertEqual(len(article_export(review, run_id="article-1")["media"]), 1)
        self.assertEqual(article_export({"result": {"markdown": "private internal prompt"}}, run_id="article-1")["status"], "unavailable")


class WebsiteVerificationContractTests(SimpleTestCase):
    def test_inverse_review_groups_preserve_exact_delete_and_restore_changes(self):
        from .website_restoration import restoration_change_groups
        changes = [{"path": "app/articles/page.tsx", "operation": "delete", "expected_sha": "a" * 40},
            {"path": "app/layout.tsx", "operation": "restore", "original_sha": "b" * 40}, {"path": "shared.ts", "operation": "retain"}]
        grouped = restoration_change_groups({"changes": changes})
        self.assertEqual(grouped["deletions"], [changes[0]])
        self.assertEqual(grouped["restorations"], [changes[1]])

    def test_generic_verify_retains_original_sealed_workflow_identity(self):
        from unittest.mock import MagicMock
        from .website_support import current_verification_data
        website = SimpleNamespace(pk="b761b209-8673-4843-9a48-90b3177bde26", generation=3, repository_id=123, github_repo="example/site", app_root="", branch="main",
            organization=SimpleNamespace(domain="example.test"), verified_sha="b" * 40, operations=MagicMock())
        payload = {"website_connection_id": "b761b209-8673-4843-9a48-90b3177bde26", "connection_generation": 3, "repository_id": 123,
            "operation_id": "original-workflow", "operation_attempt": 1, "deletion_epoch": 0, "evidence_digest": "e" * 64}
        website.operations.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(payload=payload)
        config = SimpleNamespace(website_connection=website, default_publish_target_id="custom")
        result = current_verification_data(config, {"website_connection_id": "b761b209-8673-4843-9a48-90b3177bde26", "connection_generation": 3, "repository_id": 123,
            "operation_id": "latest-ci-receipt", "operation_attempt": 7, "deletion_epoch": 9, "configuration_revision": 4, "idempotency_key": "live-verify"})
        self.assertEqual(result["operation_id"], "original-workflow")
        self.assertEqual(result["operation_attempt"], 1)
        self.assertEqual(result["deletion_epoch"], 0)
        self.assertEqual(result["configuration_revision"], 4)

    def test_ci_sources_reject_ordinary_article_children_and_wrong_binding(self):
        from .website_journey import ci_source_run
        connection = SimpleNamespace(pk="connection", generation=3, repository_id=123, blockers=[])
        operation = SimpleNamespace(pk="verifier", connection_id="connection", generation=3, state="blocked",
            payload={"workflow": "native_verification", "attempt": 1, "run_id": "child", "source_run_id": "original"})
        row = SimpleNamespace(run_id="child", status="blocked", run_request={"source_run_id": "original", "operation_id": "verifier", "operation_attempt": 1, "deletion_epoch": 0})
        self.assertEqual(ci_source_run(row, connection, operation)["runId"], "child")
        operation.payload["workflow"] = "article_revision"
        self.assertIsNone(ci_source_run(row, connection, operation))
        operation.payload["workflow"] = "native_verification"
        row.run_id = "ordinary-article-child"
        self.assertIsNone(ci_source_run(row, connection, operation))

    def test_new_source_retains_marker_only_when_exact_proof_tests_it(self):
        from .website_support import custom_target_contract
        contract = {"content_path_pattern": "content/{slug}.md", "listing_route": "/articles", "route_template": "/articles/{slug}"}
        proof = {"target_id": "custom", "source_sha": "b" * 40, "contract_digest": "d" * 64, "artifact_digest": "e" * 64}
        previous = {"contract_digest": "d" * 64, "live_marker": {"kind": "artifact_digest", "schema_version": 1, "meta_name": "mlai-artifact-digest", "value": "e" * 64}}
        self.assertEqual(custom_target_contract(contract=contract, proof=proof, previous=previous)["live_marker"], previous["live_marker"])
        self.assertNotIn("live_marker", custom_target_contract(contract=contract, proof={**proof, "artifact_digest": "f" * 64}, previous=previous))
        self.assertNotIn("live_marker", custom_target_contract(contract=contract, proof=proof, previous={**previous, "contract_digest": "c" * 64}))

    def test_worker_canonical_native_evidence_fixture_matches_backend(self):
        from .website_verification import validated_ci_identity
        proof = json.loads((Path(__file__).parent / "fixtures" / "native_evidence_v2.json").read_text())
        self.assertEqual(validated_ci_identity(proof, proof), proof)

    def proof(self):
        from .website_verification import BOOLEAN_PROOFS
        body = {"schema_version": 2, "repository_id": 123, "github_repo": "example/site", "source_sha": "a" * 40,
            "website_connection_id": "b761b209-8673-4843-9a48-90b3177bde26", "connection_generation": 3,
            "operation_id": "edb7f203-e2e8-40ba-82a2-48923364f29d", "operation_attempt": 1, "deletion_epoch": 0,
            "target_id": "native", "adapter_id": "static_html", "adapter_version": 1, "contract_digest": "b" * 64,
            "source_tree_sha": "c" * 40, "environment_fingerprint": "d" * 64, "lockfile_digests": {},
            "baseline_build": "passed", "patched_build": "passed", **{key: True for key in BOOLEAN_PROOFS}}
        return {**body, "evidence_digest": evidence_digest(body)}

    def test_ci_requires_all_exact_scope_and_rendering_proof(self):
        from .website_verification import validated_ci_identity
        proof = self.proof()
        self.assertEqual(validated_ci_identity(proof, proof), proof)
        for mutation in ({"operation_attempt": 2}, {"source_sha": "e" * 40}, {"detail": False}, {"evidence_digest": "f" * 64}):
            with self.subTest(mutation=mutation), self.assertRaises(WebsiteAuthorityError):
                validated_ci_identity(proof, {**proof, **mutation})

    def test_ci_receipt_is_read_from_github_owned_check_not_supplied_boolean(self):
        from .website_verification import read_ci_proof, CHECK_NAME
        import base64
        from unittest.mock import MagicMock
        proof = self.proof()
        check = {"name": CHECK_NAME, "head_sha": proof["source_sha"], "status": "completed", "conclusion": "success",
            "app": {"slug": "github-actions"}, "output": {"summary": "MLAI_ARTICLES_ATTESTATION:" + base64.b64encode(json.dumps(proof).encode()).decode()}}
        response = MagicMock()
        response.json.return_value = {"check_runs": [check]}
        website = SimpleNamespace(installation_id="45", github_repo="example/site", repository_id=123)
        with patch("integrations.services.github_app.create_installation_access_token", return_value=SimpleNamespace(token="synthetic")), \
             patch("integrations.http_client.get", return_value=response), patch("integrations.http_client.delete"):
            self.assertEqual(read_ci_proof(website, proof), proof)
            check["app"]["slug"] = "untrusted-app"
            with self.assertRaises(WebsiteAuthorityError):
                read_ci_proof(website, proof)


class WebsiteLiveFetchTests(SimpleTestCase):
    def dns(self, address="93.184.216.34"):
        return [(2, 1, 6, "", (address, 443))]

    def test_private_and_foreign_origins_are_rejected_before_connection(self):
        from .website_live_fetch import fetch_live_route
        for url, answer in (("https://other.test/articles", self.dns()), ("https://example.test/articles", self.dns("127.0.0.1"))):
            with patch("socket.getaddrinfo", return_value=answer), patch("content_factory.website_live_fetch.PinnedHTTPSConnection") as client:
                with self.assertRaises(WebsiteAuthorityError):
                    fetch_live_route(url, "example.test")
                client.assert_not_called()

    def test_response_is_bounded_closed_and_redirects_are_not_followed(self):
        from .website_live_fetch import fetch_live_route, MAX_BYTES
        from unittest.mock import MagicMock
        for status, content in ((302, b"redirect"), (200, b"a" * (MAX_BYTES + 1))):
            response = MagicMock(status=status)
            response.getheader.return_value = None
            response.read.return_value = content
            client = MagicMock()
            client.getresponse.return_value = response
            with patch("socket.getaddrinfo", return_value=self.dns()) as dns, patch("content_factory.website_live_fetch.PinnedHTTPSConnection", return_value=client) as constructor:
                with self.assertRaises(WebsiteAuthorityError):
                    fetch_live_route("https://example.test/articles", "example.test")
                dns.assert_called_once()
                constructor.assert_called_once_with("example.test", "93.184.216.34")
                client.close.assert_called_once()
                response.close.assert_called_once()
                if status == 200:
                    response.read.assert_called_once_with(MAX_BYTES + 1)

    def test_pin_keeps_tls_host_and_does_not_resolve_the_domain_again(self):
        from .website_live_fetch import PinnedHTTPSConnection
        from unittest.mock import MagicMock
        raw, tls = MagicMock(), MagicMock()
        with patch("socket.create_connection", return_value=raw) as connect, patch("ssl.create_default_context") as context:
            context.return_value.wrap_socket.return_value = tls
            client = PinnedHTTPSConnection("example.test", "93.184.216.34")
            client.connect()
            connect.assert_called_once_with(("93.184.216.34", 443), timeout=3)
            context.return_value.wrap_socket.assert_called_once_with(raw, server_hostname="example.test")
            tls.settimeout.assert_called_once_with(15)
            client.close()
