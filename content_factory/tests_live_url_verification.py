"""P1.13: new-journey websites verify by their live URL; legacy websites keep CI proof."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from .activation import durable_integration_evidence, live_deployment_verified
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from .models import WrittenArticle
from .website_contract import WebsiteAuthorityError
from .website_live_verification import accepted, selected, verify_live_url
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget
from .website_setup_consent import required_grants, setup_plan

BASE, MERGED = "a" * 40, "b" * 40
DIGEST = "d" * 64
CONTRACT_DIGEST = "c" * 64
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
LIVE_OFF = {**ALL_P1_FLAGS_ON, "ARTICLES_LIVE_URL_VERIFICATION_ENABLED": False}
LISTING = f'<html><head><meta name="mlai-artifact-digest" content="{DIGEST}"></head><body>Blog</body></html>'.encode()
ARTICLE_TEXT = " ".join(f"word{index}" for index in range(40))


def _article_html(text=ARTICLE_TEXT, canonical="https://{domain}/blog/first-post"):
    return (f'<html><head><link rel="canonical" href="{canonical}"></head><body><article><h1>First post</h1>'
            f"<p>{text}</p></article></body></html>")


SCAN_PROOF = {"status": "passed", "source_sha": MERGED, "preview_capable": True}


def _next_js_target(connection, *, source_sha=MERGED, verified_at=None, build_proof=True):
    """The default target as the scan of the merged source records it (with MLAI's build proof by default)."""
    contract = {"target_id": "articles", "delivery_adapter": "next_app_router", "route_path": "/blog",
        "route_template": "/blog/{slug}", "contract_digest": CONTRACT_DIGEST,
        "live_marker": {"kind": "artifact_digest", "value": DIGEST}}
    if build_proof:
        contract["verification"] = {**SCAN_PROOF, "source_sha": source_sha}
    return WebsiteConnectionTarget.objects.create(
        connection=connection, target_key="articles", generation=connection.generation, adapter="next_app_router",
        source_sha=source_sha, verified_at=verified_at or (timezone.now() - timedelta(minutes=5) if build_proof else None),
        capabilities={"publishingReady": build_proof}, contract=contract)


def _connect(built, *, verified_sha=BASE):
    connection = WebsiteConnection.objects.create(
        organization=built.organization, github_repo="banksia/website", repository_id=777, installation_id="900",
        branch="main", site_url=f"https://{built.organization.domain}", state=WebsiteConnection.State.CONNECTED,
        verified_sha=verified_sha, capabilities={"inventoryReady": True})
    config = built.config
    config.website_connection = connection
    config.github_repo = connection.github_repo
    config.default_publish_target_id = "articles"
    config.save(update_fields=["website_connection", "github_repo", "default_publish_target_id", "updated_at"])
    return connection


class LiveUrlFixture:
    def setUp(self):
        super().setUp()
        head = patch("content_factory.website_connections.verify_repository_head", side_effect=lambda connection, sha: sha)
        head.start()
        self.addCleanup(head.stop)
        lineage = patch("content_factory.website_verification._setup_merged_at", return_value=True)
        self.lineage = lineage.start()
        self.addCleanup(lineage.stop)

    def new_journey_site(self, domain="banksia.studio", **target_options):
        built = make_new_journey_org(domain=domain)
        connection = _connect(built)
        target = _next_js_target(connection, **target_options)
        built.config.refresh_from_db()
        return built, connection, target

    def fetch(self, pages):
        """Patch the pinned fetcher with ``{url: body}``; anything else is unreachable."""
        def fetch_live_route(url, domain, *, expected_status=200):
            if url not in pages:
                raise WebsiteAuthorityError("deployment_route_transient", "The public articles route could not be reached.", retryable=True)
            return pages[url], {}
        return patch("content_factory.website_live_fetch.fetch_live_route", side_effect=fetch_live_route)


@override_settings(**ALL_P1_FLAGS_ON)
class LiveUrlVerificationTests(LiveUrlFixture, TestCase):
    def test_a_new_journey_next_site_verifies_by_its_live_blog_page(self):
        built, connection, target = self.new_journey_site()
        with self.fetch({"https://banksia.studio/blog": LISTING}) as fetch:
            receipt = verify_live_url(built.config, target, setup_run_id="setup-1")
        fetch.assert_called_once_with("https://banksia.studio/blog", "banksia.studio", expected_status=200)
        target.refresh_from_db()
        connection.refresh_from_db()
        # The scan's build proof is kept; the live proof is written beside it.
        self.assertEqual(target.contract["verification"], SCAN_PROOF)
        live = target.contract["live_verification"]
        self.assertEqual((live["status"], live["attestation_origin"], live["source_sha"]), ("verified", "live_url", MERGED))
        self.assertTrue(live["observed_at"])
        self.assertIsNotNone(target.verified_at)
        self.assertTrue(target.capabilities["publishingReady"])
        self.assertEqual(connection.verified_sha, MERGED)
        self.assertTrue(connection.capabilities["publishingReady"])
        operation = WebsiteConnectionOperation.objects.get(pk=receipt["operation_id"])
        self.assertEqual((operation.action, operation.state), ("deployment-verify", "completed"))
        self.assertEqual(operation.receipt["method"], "live_url")
        self.assertIs(operation.receipt["provider_verified"], False)
        self.assertNotIn("ci_evidence_digest", operation.receipt)
        # Activation accepts the live receipt in place of CI execution proof.
        self.assertTrue(accepted(connection, target))
        evidence = durable_integration_evidence(built.config, connection)
        self.assertTrue(evidence["verified"], evidence)
        self.assertTrue(evidence["publishingVerified"])
        self.assertTrue(live_deployment_verified(connection, target))
        from .website_sealed_workflow import required
        self.assertFalse(required(connection, target))
        with patch("content_factory.website_sealed_workflow.required", return_value=True):
            self.assertTrue(durable_integration_evidence(built.config, connection)["verified"])
            self.assertTrue(live_deployment_verified(connection, target))

    def test_one_live_article_is_compared_with_its_saved_body(self):
        built, connection, target = self.new_journey_site()
        url = "https://banksia.studio/blog/first-post"
        run = ContentFactoryRun.objects.create(run_id="article-1", workflow="article_generation", domain="banksia.studio",
            organization=built.organization, status="completed",
            result={"content_package": {"article_html": _article_html(canonical=url)}})
        WrittenArticle.objects.create(organization=built.organization, title="First post", slug="first-post",
            category="guides", primary_keyword="first post", live_url=url, published_at=timezone.now() - timedelta(days=1),
            source_run_id=run.run_id)
        with self.fetch({"https://banksia.studio/blog": LISTING, url: _article_html(canonical=url).encode()}):
            receipt = verify_live_url(built.config, target, setup_run_id="setup-1")
        self.assertEqual([row["kind"] for row in receipt["verified_routes"]], ["listing", "detail"])
        self.assertTrue(receipt["verified_routes"][1]["body_sha256"])
        self.assertIs(WebsiteConnectionTarget.objects.get(pk=target.pk).contract["live_verification"]["detail_compared"], True)

    def test_a_revised_or_removed_article_never_blocks_the_live_proof(self):
        built, connection, target = self.new_journey_site()
        older, newer = "https://banksia.studio/blog/older-post", "https://banksia.studio/blog/first-post"
        for run_id, url, days in (("article-old", older, 3), ("article-new", newer, 1)):
            run = ContentFactoryRun.objects.create(run_id=run_id, workflow="article_generation", domain="banksia.studio",
                organization=built.organization, status="completed",
                result={"content_package": {"article_html": _article_html(canonical=url)}})
            WrittenArticle.objects.create(organization=built.organization, title=run_id, slug=run_id, category="guides",
                primary_keyword=run_id, live_url=url, published_at=timezone.now() - timedelta(days=days), source_run_id=run.run_id)
        edited = _article_html(text=ARTICLE_TEXT.replace("word3", "other"), canonical=newer).encode()
        # The newest article was edited on the site: the next one is compared instead.
        with self.fetch({"https://banksia.studio/blog": LISTING, newer: edited, older: _article_html(canonical=older).encode()}):
            receipt = verify_live_url(built.config, target, setup_run_id="setup-1")
        self.assertEqual(receipt["verified_routes"][1]["path"], "/blog/older-post")
        target.refresh_from_db()
        # Every article edited or taken down: the listing marker alone proves the deployed setup.
        def fetch_live_route(url, domain, *, expected_status=200):
            if url == "https://banksia.studio/blog":
                return LISTING, {}
            if url == newer:
                return edited, {}
            raise WebsiteAuthorityError("live_route_unavailable", "The exact public articles route has not deployed successfully.")
        with patch("content_factory.website_live_fetch.fetch_live_route", side_effect=fetch_live_route):
            receipt = verify_live_url(built.config, target, setup_run_id="setup-1")
        self.assertEqual([row["kind"] for row in receipt["verified_routes"]], ["listing"])
        target.refresh_from_db()
        connection.refresh_from_db()
        self.assertTrue(accepted(connection, target))
        self.assertIs(target.contract["live_verification"]["detail_compared"], False)
        # An unreachable host is still transient, so nothing is recorded for it.
        def unreachable_detail(url, domain, *, expected_status=200):
            if url == "https://banksia.studio/blog":
                return LISTING, {}
            raise WebsiteAuthorityError("deployment_route_transient", "The public articles route could not be reached.", retryable=True)
        with patch("content_factory.website_live_fetch.fetch_live_route", side_effect=unreachable_detail), \
                self.assertRaises(WebsiteAuthorityError) as raised:
            verify_live_url(built.config, target, setup_run_id="setup-1")
        self.assertEqual(raised.exception.code, "deployment_route_transient")

    def test_a_target_without_mlai_build_proof_is_never_certified_by_its_live_page(self):
        """A page carrying the kit's marker never stands in for a failed or uncertified build."""
        from .website_sealed_workflow import REQUIRED_VERSION
        failed = {"status": "failed", "source_sha": MERGED}
        cases = {
            "failed build": dict(verification=failed, verified_at=None, capabilities={"publishingReady": False}),
            "no proof yet": dict(verification=None, verified_at=None, capabilities={"publishingReady": False}),
            "proof of another source": dict(verification={**SCAN_PROOF, "source_sha": BASE}),
            "uncertified candidate": dict(contract_extra={"contract": {"publishing_inputs_v1": {"schema_version": 1}}},
                verified_at=None, capabilities={"publishingReady": False}),
            "certified candidate": dict(contract_extra={"contract": {"publishing_inputs_v1": {"schema_version": 1}}}),
            "sealed v3 target": dict(capabilities={"publishingReady": True, REQUIRED_VERSION: 3}),
        }
        for label, case in cases.items():
            with self.subTest(label):
                built, connection, target = self.new_journey_site(domain=f"{label.replace(' ', '-')}.banksia.studio")
                contract = {**target.contract, **case.get("contract_extra", {})}
                if "verification" in case:
                    contract.pop("verification", None)
                    if case["verification"] is not None:
                        contract["verification"] = case["verification"]
                target.contract = contract
                if "verified_at" in case:
                    target.verified_at = case["verified_at"]
                target.capabilities = case.get("capabilities", target.capabilities)
                target.save(update_fields=["contract", "verified_at", "capabilities"])
                before = (target.verified_at, dict(target.contract), dict(target.capabilities))
                with self.fetch({f"https://{built.organization.domain}/blog": LISTING}) as fetch, \
                        self.assertRaises(WebsiteAuthorityError) as raised:
                    verify_live_url(built.config, target, setup_run_id="setup-1")
                self.assertEqual(raised.exception.code, "verified_target_required")
                fetch.assert_not_called()
                target.refresh_from_db()
                connection.refresh_from_db()
                self.assertEqual((target.verified_at, target.contract, target.capabilities), before)
                self.assertEqual(connection.verified_sha, BASE)
                self.assertFalse(connection.operations.filter(action="deployment-verify").exists())
                # A stored live proof never makes such a target acceptable either.
                target.contract = {**target.contract, "live_verification": {"status": "verified", "attestation_origin": "live_url",
                    "source_sha": MERGED, "contract_digest": CONTRACT_DIGEST, "artifact_digest": DIGEST}}
                target.save(update_fields=["contract"])
                connection.verified_sha = MERGED
                connection.save(update_fields=["verified_sha"])
                self.assertFalse(accepted(connection, target))

    def test_a_missing_marker_or_a_transient_fetch_leaves_the_site_unverified(self):
        built, connection, target = self.new_journey_site()
        for pages, code in (({"https://banksia.studio/blog": b"<html><body>Blog without the setup</body></html>"}, "live_route_unavailable"),
                            ({}, "deployment_route_transient")):
            with self.subTest(code=code), self.fetch(pages), self.assertRaises(WebsiteAuthorityError) as raised:
                verify_live_url(built.config, target, setup_run_id="setup-1")
            self.assertEqual(raised.exception.code, code)
            target.refresh_from_db()
            connection.refresh_from_db()
            self.assertEqual(target.contract["verification"], SCAN_PROOF)
            self.assertNotIn("live_verification", target.contract)
            self.assertFalse(accepted(connection, target))
            self.assertEqual(connection.verified_sha, BASE)
            self.assertFalse(WebsiteConnectionOperation.objects.filter(connection=connection, action="deployment-verify").exists())
        from .website_failure_contract import load_registry
        registered = {entry["code"] for entry in load_registry()["codes"]}
        self.assertTrue({"live_route_unavailable", "deployment_route_transient", "deployment_origin_invalid"} <= registered)

    def test_a_changed_source_needs_a_reviewed_setup_merge(self):
        built, connection, target = self.new_journey_site()
        self.lineage.return_value = False
        with self.fetch({"https://banksia.studio/blog": LISTING}) as fetch, \
                self.assertRaises(WebsiteAuthorityError) as raised:
            verify_live_url(built.config, target, setup_run_id="setup-1")
        self.assertEqual(raised.exception.code, "website_source_review_required")
        fetch.assert_not_called()

    def test_setup_asks_only_for_contents_and_pull_requests(self):
        built, connection, target = self.new_journey_site()
        context = SimpleNamespace(organization=built.organization, company=built.company, profile=None)
        with patch("content_factory.website_setup_consent.enabled", return_value=True), \
                patch("content_factory.billing.get_content_factory_article_cost_points", return_value=20):
            plan = setup_plan(context, built.config, {"githubPermissions": {"contents": True, "pull_requests": True}})
        self.assertEqual(plan["ciMode"], "hosted")
        self.assertEqual(plan["github"]["required"], {"contents": "write", "pull_requests": "write"})
        self.assertEqual(plan["github"]["missing"], [])

    def test_workflow_file_credentials_are_refused(self):
        from .website_connections import contract_for
        from .website_tokens import mint_website_token
        built, connection, target = self.new_journey_site()
        with patch("content_factory.website_connections.verify_repository_native_target"), \
                patch("content_factory.website_tokens._workflow_operation") as operation, \
                patch("integrations.services.github_app.create_installation_access_token") as mint, \
                self.assertRaises(WebsiteAuthorityError) as raised:
            mint_website_token({**contract_for(connection), "permission_profile": "workflow_files"},
                permission_mode="write", action="setup")
        self.assertEqual(raised.exception.code, "invalid_permission_profile")
        operation.assert_not_called()
        mint.assert_not_called()

    def test_prepare_verifies_new_journey_sites_by_live_url_and_never_asks_for_permissions(self):
        from . import website_prepare
        built, connection, target = self.new_journey_site()
        op = SimpleNamespace(receipt={"setup_run_id": "setup-1"})
        context = SimpleNamespace(organization=built.organization, company=built.company)
        self.assertEqual(website_prepare._dispatch_prepare_step(op, context, built.config, "permissions", user=built.owner,
            journey={"prerequisites": {"permissions": {"reasonCode": "github_workflow_permission_required"}}}),
            {"status": "completed"})
        for step in ("ci-attestation", "verify"):
            with self.subTest(step=step), \
                    patch("content_factory.website_live_verification.verify_live_url", return_value={"operation_id": "op-1"}) as live, \
                    patch("content_factory.website_verification.discover_source_attestation") as discover, \
                    patch("content_factory.website_verification.verify_live_deployment") as deployment:
                outcome = website_prepare._dispatch_prepare_step(op, context, built.config, step, user=built.owner, journey={})
            self.assertEqual(outcome, {"status": "completed", "operation_id": "op-1"})
            self.assertEqual(live.call_args.kwargs["setup_run_id"], "setup-1")
            discover.assert_not_called()
            deployment.assert_not_called()

    def test_the_journey_needs_no_workflow_or_ci_permission(self):
        from .website_journey import project_journey
        journey = project_journey(company_id="c1", domain="banksia.studio",
            website={"status": "connected", "repositoryId": 777, "connectionId": "x", "writePolicy": {"allowed": True}},
            capabilities={"repositoryAccessVerified": True, "repositoryWriteVerified": True},
            proof={"liveVerificationSelected": True, "integrationVerified": False, "buildVerified": False,
                "ciAttestationRequired": True},
            permissions={"workflows": False, "checks": False, "statuses": False})
        self.assertEqual(journey["prerequisites"]["permissions"]["status"], "complete")
        self.assertNotIn("workflows_permission", journey["proofContract"]["requirements"])
        self.assertNotIn("ci_artifact_digest", journey["proofContract"]["requirements"])
        self.assertNotEqual((journey["nextAction"] or {}).get("id"), "permissions")


@override_settings(**ALL_P1_FLAGS_ON)
class LiveUrlRenewalTests(LiveUrlFixture, TestCase):
    """A live-URL proof is renewed automatically before it ages out, like CI proof is."""

    def verified_site(self, *, age=timedelta(days=6)):
        built, connection, target = self.new_journey_site()
        with self.fetch({"https://banksia.studio/blog": LISTING}):
            verify_live_url(built.config, target, setup_run_id="setup-1")
        aged = timezone.now() - age
        WebsiteConnectionTarget.objects.filter(pk=target.pk).update(verified_at=aged)
        WebsiteConnection.objects.filter(pk=connection.pk).update(last_verified_at=aged)
        WebsiteConnectionOperation.objects.filter(connection=connection).update(created_at=aged, updated_at=aged)
        connection.refresh_from_db()
        target.refresh_from_db()
        return built, connection, target

    def test_the_verification_refresh_renews_a_live_url_proof_before_day_seven(self):
        from . import website_reconciliation as recon
        built, connection, target = self.verified_site()
        self.assertEqual(recon._refresh_target(connection, built.config), target)
        queued = recon.queue_verification_refreshes(now=timezone.now())
        self.assertEqual(len(queued), 1)
        with self.fetch({"https://banksia.studio/blog": LISTING}), \
                patch("content_factory.website_verification.discover_source_attestation") as discover, \
                patch("content_factory.website_verification.verify_live_deployment") as deployment:
            state = recon._process_source_reverification(queued[0], timezone.now())
        self.assertEqual(state, "completed")
        discover.assert_not_called()
        deployment.assert_not_called()
        operation = WebsiteConnectionOperation.objects.get(pk=queued[0])
        self.assertEqual((operation.receipt["status"], operation.receipt["method"]), ("verified", "live_url"))
        connection.refresh_from_db()
        target.refresh_from_db()
        self.assertGreater(target.verified_at, timezone.now() - timedelta(minutes=1))
        later = timezone.now() + timedelta(days=3)
        evidence = durable_integration_evidence(built.config, connection, now=later)
        self.assertTrue(evidence["verified"], evidence)
        self.assertTrue(evidence["publishingVerified"])

    def test_a_pushed_source_waits_for_its_build_proof_and_never_takes_the_ci_path(self):
        from . import website_reconciliation as recon
        built, connection, target = self.verified_site(age=timedelta(days=1))
        operation = WebsiteConnectionOperation.objects.create(connection=connection, generation=connection.generation,
            action="source-reverify", payload={"source_sha": "e" * 40, "target_id": "articles", "scan_required": False},
            receipt={"status": "verification_queued"}, idempotency_key="push-check")
        with self.fetch({"https://banksia.studio/blog": LISTING}) as fetch, \
                patch("content_factory.website_verification.discover_source_attestation") as discover:
            state = recon._process_source_reverification(operation.pk, timezone.now())
        self.assertEqual(state, "pending")
        operation.refresh_from_db()
        self.assertEqual(operation.receipt["code"], "verified_target_required")
        fetch.assert_not_called()
        discover.assert_not_called()


@override_settings(**ALL_P1_FLAGS_ON)
class SealedTargetTests(LiveUrlFixture, TestCase):
    """A target that needs sealed execution proof keeps its CI requirements, even for a new-journey site."""

    def sealed_site(self):
        from .website_sealed_workflow import REQUIRED_VERSION
        built, connection, target = self.new_journey_site()
        target.capabilities = {**target.capabilities, REQUIRED_VERSION: 3}
        target.save(update_fields=["capabilities"])
        connection.verified_sha = MERGED
        connection.last_verified_at = timezone.now()
        connection.save(update_fields=["verified_sha", "last_verified_at"])
        return built, connection, target

    def test_prepare_takes_the_ci_path_for_a_sealed_target(self):
        from . import website_prepare
        built, connection, target = self.sealed_site()
        op = SimpleNamespace(receipt={"setup_run_id": "setup-1"})
        context = SimpleNamespace(organization=built.organization, company=built.company)
        ci = SimpleNamespace(pk="ci-op", receipt={"evidence_digest": "e"})
        with patch("content_factory.website_live_verification.verify_live_url") as live, \
                patch("content_factory.website_verification.discover_source_attestation", return_value={}) as discover, \
                patch("content_factory.website_verification.record_ci_attestation", return_value=ci):
            outcome = website_prepare._dispatch_prepare_step(op, context, built.config, "ci-attestation", user=built.owner, journey={})
        self.assertEqual(outcome, {"status": "completed", "operation_id": "ci-op"})
        discover.assert_called_once()
        live.assert_not_called()

    def test_the_journey_keeps_asking_for_ci_proof_a_sealed_target_needs(self):
        from .website_journey import journey_for_context
        built, connection, target = self.sealed_site()
        context = SimpleNamespace(organization=built.organization, company=built.company, profile=None)
        with patch("content_factory.website_verification.current_ci_attestation", return_value=None):
            journey = journey_for_context(context, built.config, capabilities={})
        self.assertIn("ci_artifact_digest", journey["proofContract"]["requirements"])
        self.assertEqual(journey["prerequisites"]["verification"]["reasonCode"], "ci_attestation_required")
        plain_built, plain_connection, plain_target = self.new_journey_site(domain="plain.banksia.studio")
        plain_connection.verified_sha = MERGED
        plain_connection.last_verified_at = timezone.now()
        plain_connection.save(update_fields=["verified_sha", "last_verified_at"])
        context = SimpleNamespace(organization=plain_built.organization, company=plain_built.company, profile=None)
        with patch("content_factory.website_verification.current_ci_attestation", return_value=None):
            journey = journey_for_context(context, plain_built.config, capabilities={})
        self.assertNotIn("ci_artifact_digest", journey["proofContract"]["requirements"])
        self.assertEqual(journey["prerequisites"]["verification"]["status"], "complete")


class LegacyCiProofTests(LiveUrlFixture, TestCase):
    """With every P1 flag on, legacy organisations still verify by CI proof (P1.13)."""

    def test_legacy_orgs_are_never_selected_and_keep_their_grants(self):
        self.assertEqual(required_grants(ci_mode="repository"),
            {"contents": "write", "pull_requests": "write", "workflows": "write", "checks": "read", "statuses": "read"})
        for name in ("talathrive", "vyavos", "pawshare", "la_vie", "mlai"):
            with self.subTest(org=name), override_settings(**ALL_P1_FLAGS_ON):
                built = make_named_legacy_org(name)
                self.assertFalse(selected(built.organization))
                context = SimpleNamespace(organization=built.organization, company=built.company, profile=None)
                with patch("content_factory.website_setup_consent.enabled", return_value=True), \
                        patch("content_factory.billing.get_content_factory_article_cost_points", return_value=20):
                    plan = setup_plan(context, built.config, {})
                self.assertEqual(plan["ciMode"], "repository")
                self.assertEqual(plan["github"]["required"], required_grants(ci_mode="repository"))
                self.assertIn("workflows", plan["github"]["missing"])

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_a_legacy_live_receipt_never_replaces_ci_proof(self):
        built = make_named_legacy_org("talathrive")
        connection, target = built.connection, built.target
        target.contract = {**target.contract, "verification": {"status": "verified", "attestation_origin": "live_url",
            "source_sha": target.source_sha}}
        target.capabilities = {**target.capabilities, "publishingReady": True}
        target.save(update_fields=["contract", "capabilities"])
        connection.capabilities = {**connection.capabilities, "publishingReady": True}
        connection.save(update_fields=["capabilities"])
        built.config.default_publish_target_id = target.target_key
        built.config.save(update_fields=["default_publish_target_id"])
        self.assertFalse(accepted(connection, target))
        self.assertTrue(durable_integration_evidence(built.config, connection)["verified"])
        with patch("content_factory.website_sealed_workflow.required", return_value=True), \
                patch("content_factory.website_verification.current_ci_attestation", return_value=None):
            self.assertEqual(durable_integration_evidence(built.config, connection)["reasonCode"], "verification_required")

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_legacy_prepare_keeps_the_ci_path(self):
        from . import website_prepare
        built = make_named_legacy_org("vyavos")
        built.config.default_publish_target_id = built.target.target_key
        built.config.save(update_fields=["default_publish_target_id"])
        op = SimpleNamespace(receipt={})
        context = SimpleNamespace(organization=built.organization, company=built.company)
        with self.assertRaises(WebsiteAuthorityError) as raised:
            website_prepare._dispatch_prepare_step(op, context, built.config, "permissions", user=built.owner,
                journey={"prerequisites": {"permissions": {"reasonCode": "github_workflow_permission_required", "reason": "x"}}})
        self.assertEqual(raised.exception.code, "github_workflow_permission_required")
        ci = SimpleNamespace(pk="ci-op", receipt={"evidence_digest": "e"})
        with patch("content_factory.website_live_verification.verify_live_url") as live, \
                patch("content_factory.website_verification.discover_source_attestation", return_value={}) as discover, \
                patch("content_factory.website_verification.record_ci_attestation", return_value=ci):
            outcome = website_prepare._dispatch_prepare_step(op, context, built.config, "ci-attestation", user=built.owner, journey={})
        self.assertEqual(outcome, {"status": "completed", "operation_id": "ci-op"})
        discover.assert_called_once()
        live.assert_not_called()

    def test_v2_off_or_the_flag_off_keeps_ci_proof_for_a_new_journey_site(self):
        for label, flags in (("v2-off", V2_OFF), ("flag-off", LIVE_OFF)):
            with self.subTest(label), override_settings(**flags):
                built, connection, target = self.new_journey_site(domain=f"{label}.banksia.studio")
                self.assertFalse(selected(built.organization))
                context = SimpleNamespace(organization=built.organization, company=built.company, profile=None)
                with patch("content_factory.website_setup_consent.enabled", return_value=True), \
                        patch("content_factory.billing.get_content_factory_article_cost_points", return_value=20):
                    self.assertEqual(setup_plan(context, built.config, {})["ciMode"], "repository")
