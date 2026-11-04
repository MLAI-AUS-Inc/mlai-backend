"""The live check requires the reviewed marker only where the merged kit sealed it.

Vyavos (17 Oct): setup PR #7 adopted the site's existing articles route. Its kit
config (``artifact_marker_routes: ["/articles"]``) marks only the listing, since the
article detail is the site's own renderer. CI verified the listing marker and
MLAI merged the PR. The live listing then served the reviewed marker, but the
detail served the original setup's marker, so ``verify_live_deployment`` raised
``live_route_unavailable`` on every retry.
"""
import base64
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import website_verification as verification
from .website_contract import WebsiteAuthorityError

SOURCE = "a" * 40
DIGEST = "07aa9822940770ad9be8619b054130c089faca0bae7d138202d4f1e476d0ae3e"
OTHER = "09f06336a5aa1499f4f9c5d029d20e28f10195446f0c064fc9b38dac7e839898"
ROUTES = {"listing": "/articles", "detail": "/articles/how-workflow-automation-removes-manual-bottlenecks",
          "unknown_slug": "/articles/mlai-nonexistent-0123456789ab"}


def page(marker):
    return f'<html><head><meta name="mlai-artifact-digest" content="{marker}"></head><body><h1>Articles</h1></body></html>'.encode()


def contents_response(config, status=200):
    body = {"content": base64.b64encode(json.dumps(config).encode()).decode()} if config is not None else {}
    return SimpleNamespace(status_code=status, json=lambda: body)


class KitMarkerRoutesTests(SimpleTestCase):
    def setUp(self):
        self.website = SimpleNamespace(installation_id=7, github_repo="Vyavos/site", repository_id=42)
        self.token = patch("integrations.services.github_app.create_installation_access_token",
                           return_value=SimpleNamespace(token="read-only")).start()
        self.http = patch("integrations.http_client.get").start()
        self.addCleanup(patch.stopall)

    def test_reads_the_exact_commits_kit_config_with_a_read_only_repository_token(self):
        self.http.return_value = contents_response({"artifact_digest": DIGEST, "artifact_marker_routes": ["/articles"]})
        self.assertEqual(verification.kit_marker_routes(self.website, SOURCE, DIGEST), {"/articles"})
        self.assertIn(f"/contents/.mlai/articles-verification.json?ref={SOURCE}", self.http.call_args.args[0])
        # Live checks never need the optional CI evidence grants.
        self.assertEqual(self.token.call_args.kwargs.get("permission_mode"), "read")
        self.assertNotIn("permission_profile", self.token.call_args.kwargs)

    def test_unreadable_or_foreign_configs_keep_every_route_marked(self):
        cases = {
            "missing": contents_response(None, status=404),
            "other_marker": contents_response({"artifact_digest": OTHER, "artifact_marker_routes": ["/articles"]}),
            "no_routes": contents_response({"artifact_digest": DIGEST}),
            "empty_routes": contents_response({"artifact_digest": DIGEST, "artifact_marker_routes": []}),
            "relative_route": contents_response({"artifact_digest": DIGEST, "artifact_marker_routes": ["articles"]}),
            "not_an_object": contents_response(["/articles"]),
        }
        for name, response in cases.items():
            with self.subTest(name):
                self.http.return_value = response
                self.assertIsNone(verification.kit_marker_routes(self.website, SOURCE, DIGEST))
        self.http.side_effect = ValueError("not json")
        self.assertIsNone(verification.kit_marker_routes(self.website, SOURCE, DIGEST))
        self.token.side_effect = RuntimeError("installation unavailable")
        self.assertIsNone(verification.kit_marker_routes(self.website, SOURCE, DIGEST))

    def test_listing_always_requires_the_marker(self):
        self.assertTrue(verification.route_requires_marker("listing", "/articles", {"/elsewhere"}))
        self.assertTrue(verification.route_requires_marker("detail", ROUTES["detail"], None))
        self.assertFalse(verification.route_requires_marker("detail", ROUTES["detail"], {"/articles"}))
        self.assertTrue(verification.route_requires_marker("detail", ROUTES["detail"] + "/", {ROUTES["detail"]}))


class LiveDeploymentMarkerTests(SimpleTestCase):
    def run_check(self, *, marker_routes, listing=DIGEST, detail=OTHER):
        target = SimpleNamespace(contract={"route_path": "/articles", "route_template": "/articles/{slug}",
            "live_marker": {"kind": "artifact_digest", "value": DIGEST}})
        website = SimpleNamespace(pk="551c9311", generation=2, organization=SimpleNamespace(domain="vyavos.com"),
            targets=SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(first=lambda: target)))
        bodies = {"listing": page(listing), "detail": page(detail), "unknown_slug": b"not found"}
        fetches = []

        def fetch(url, domain, expected_status):
            kind = next(key for key, path in ROUTES.items() if url.endswith(path))
            fetches.append(kind)
            return bodies[kind], {}

        data = {"target_id": "articles", "source_sha": SOURCE, "evidence_digest": "e" * 64, "contract_digest": "c" * 64}
        with patch.object(verification, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(website)), \
                patch.object(verification, "ci_attestation_for", return_value={"artifact_digest": DIGEST, "verified_routes": ROUTES}), \
                patch.object(verification, "contract_for", return_value={}), \
                patch.object(verification, "require_unlocked_remote_call"), \
                patch.object(verification, "kit_marker_routes", return_value=marker_routes), \
                patch("content_factory.website_connections.verify_repository_head"), \
                patch("content_factory.website_deployment.ensure_fingerprint"), \
                patch("content_factory.website_deployment.observe_headers"), \
                patch("content_factory.website_live_fetch.fetch_live_route", side_effect=fetch), \
                patch("content_factory.website_prepare.wake_prepare_after_commit"), \
                patch.object(verification.WebsiteConnectionOperation.objects, "update_or_create",
                             side_effect=lambda **kwargs: (SimpleNamespace(**kwargs), True)):
            operation = verification.verify_live_deployment(None, data=data)
        return operation, fetches

    def test_adopted_listing_only_kit_passes_with_the_sites_own_detail_marker(self):
        operation, fetches = self.run_check(marker_routes={"/articles"})
        self.assertEqual(operation.defaults["receipt"]["status"], "passed")
        self.assertEqual(fetches, ["listing", "detail", "unknown_slug"])

    def test_a_kit_that_sealed_the_detail_still_requires_its_marker(self):
        for routes in (None, {"/articles", ROUTES["detail"]}):
            with self.subTest(routes=routes), self.assertRaises(WebsiteAuthorityError) as caught:
                self.run_check(marker_routes=routes)
            self.assertEqual(caught.exception.code, "live_route_unavailable")

    def test_the_listing_must_serve_the_reviewed_marker(self):
        with self.assertRaises(WebsiteAuthorityError) as caught:
            self.run_check(marker_routes={"/articles"}, listing=OTHER)
        self.assertEqual(caught.exception.code, "live_route_unavailable")
