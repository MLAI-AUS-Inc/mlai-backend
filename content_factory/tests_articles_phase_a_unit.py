"""Phase A incident replays; no database construction or provider writes."""
from contextlib import ExitStack, nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from . import website_prepare as prepare, tests_prepare_journey_unit as journey_fixtures
from . import tests_prepare_permissions_unit as verification_fixtures
from .website_contract import WebsiteAuthorityError


class ChildFailurePolicyTests(SimpleTestCase):
    def replay(self, result, *, status="blocked", setup_status="preview_failed"):
        fixture = journey_fixtures.PrepareJourneyReplayTests()
        fixture.setUp()
        fixture.op.receipt = {"child_run_id": "vyavos", "current_step": "setup"}
        fixture.op.payload["run_id"] = "vyavos"
        child = SimpleNamespace(run_id="vyavos", workflow="article_system_setup", status=status,
            result={**result, "article_system_setup": {**result.get("article_system_setup", {}), "status": setup_status}},
            error="Style repair exhausted.")
        dispatch = Mock()
        fixture.advance({"setup": dispatch}, child)
        dispatch.assert_not_called()
        return fixture.op

    def test_vyavos_legacy_retryable_style_failure_never_restarts(self):
        op = self.replay({"error_code": "DIRECTORY_STYLE_REVIEW_FAILED", "retryable": True})
        self.assertEqual(op.state, "needs_attention")
        self.assertEqual(op.receipt["failure_class"], "repairable")
        self.assertEqual(op.receipt["child_run_id"], "vyavos")

    def test_class_truth_table(self):
        for status in ("failed", "blocked"):
            for classification, expected in (("repairable", "needs_attention"), ("strategy", "needs_attention"),
                    ("unsupported", "stopped"), ("needs_user", "needs_user"), ("transient", "pending"), ("waiting", "pending")):
                with self.subTest(status=status, classification=classification):
                    op = self.replay({"failure_class": classification, "user_action": {"id": "authorize_github"}}, status=status)
                    self.assertEqual(op.state, expected)
                    if expected == "pending":
                        self.assertIsNotNone(op.next_attempt_at)
                        self.assertEqual(op.receipt["child_run_id"], "vyavos")
                    else:
                        self.assertIsNone(op.next_attempt_at)

    def test_code_review_ready_asks_once_without_polling(self):
        op = self.replay({}, status="awaiting_approval", setup_status="code_review_ready")
        self.assertEqual(op.state, "needs_user")
        self.assertEqual(op.receipt["user_action"]["id"], "review_setup")
        self.assertIsNone(op.next_attempt_at)

    def test_manual_merge_has_pr_action(self):
        op = self.replay({"pr_number": 42, "pr_url": "https://github.com/fixture/site/pull/42"}, setup_status="manual_merge_required")
        self.assertEqual(op.state, "needs_user")
        self.assertEqual(op.receipt["user_action"]["id"], "merge_pr")
        self.assertEqual(op.receipt["user_action"]["url"], "https://github.com/fixture/site/pull/42")

    def test_github_outage_is_transient_despite_exception_default(self):
        fixture = journey_fixtures.fixtures.PrepareJourneyTests()
        op = fixture.operation()
        prepare.prepare_failure(op, "verify-access", WebsiteAuthorityError("github_unavailable", "Provider unavailable."))
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt["failure_class"], "transient")


class RetryIdentityTests(SimpleTestCase):
    def test_backend_route_choice_accepts_trailing_slash_and_root(self):
        from .website_connections import contract_for
        for saved, candidate in (("/stories/", "/stories"), ("/", "/")):
            fixture = journey_fixtures.PrepareJourneyReplayTests()
            fixture.setUp()
            op = fixture.op
            op.connection.pk = uuid4()
            op.connection_id = fixture.config.website_connection_id = op.connection.pk
            op.receipt = {"user_action": {"id": "choose_articles_page", "candidates": [candidate]}}
            fixture.config.article_system = {"pending_article_system_setup": {
                "mode": "existing", "route_path": saved, "setup_run_id": "chosen"}}
            child = SimpleNamespace(run_id="chosen", workflow="article_system_setup", organization_id=7, domain="fixture.test",
                run_request={**contract_for(op.connection), "article_surface_hint": {"route_path": candidate}})
            with self.subTest(route=saved):
                self.assertIs(prepare._selected_setup_child(op, fixture.config, run=child), child)

    def test_owner_retry_cancels_old_child_and_resets_deployment_budget(self):
        fixture = journey_fixtures.PrepareJourneyReplayTests()
        fixture.setUp()
        op = fixture.op
        op.state = "needs_user"
        op.receipt = {"child_run_id": "stale-proof", "deployment_attempts": 7, "current_step": "setup"}
        with patch.object(prepare, "authority_guard", return_value=nullcontext(op.connection)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(op, False)), \
                patch.object(prepare, "_cancel_prepare_child") as cancel:
            prepare.start_prepare(fixture.config, expected={}, retry=True)
        cancel.assert_called_once()
        self.assertIsNone(op.receipt["child_run_id"])
        self.assertEqual(op.receipt["deployment_attempts"], 0)
        self.assertTrue(op.payload["retry_epoch"])
        request = prepare._owner_request(op, fixture.context, fixture.config, "setup", user=fixture.user, journey=fixture.journey())
        self.assertIn(op.payload["retry_epoch"], request.data["idempotency_key"])
        replay = prepare._owner_request(op, fixture.context, fixture.config, "setup", user=fixture.user, journey=fixture.journey())
        self.assertEqual(request.data, replay.data)


class DeploymentPolicyTests(SimpleTestCase):
    def test_real_prepare_rejects_wrong_marker_without_saving_live_receipt(self):
        fixture = verification_fixtures.PrepareVerificationRuntimeTests()
        fixture.setUp()
        with fixture.boundaries():
            fixture.fetch.side_effect = [(b"", {"x-mlai-artifact-digest": "e" * 64}),
                (b"", {"x-mlai-artifact-digest": "f" * 64})]
            prepare.advance_prepare(fixture.prepare.pk, now=fixture.now)
            self.assertEqual(fixture.prepare.receipt["reason_code"], "live_route_unavailable")
            self.assertEqual(fixture.prepare.receipt["failure_class"], "waiting")
            self.assertEqual(fixture.prepare.state, "pending")
            self.assertIsNone(fixture.live)
            self.assertEqual(fixture.fetch.call_count, 2)
            next_poll = fixture.prepare.next_attempt_at
            fixture.fetch.side_effect = None
            fixture.fetch.return_value = (b"", {"x-mlai-artifact-digest": "e" * 64})
            prepare.advance_prepare(fixture.prepare.pk, now=next_poll)
            self.assertEqual(fixture.live.receipt["status"], "passed")
            self.assertEqual(fixture.live.receipt["artifact_digest"], "e" * 64)
            self.assertEqual(fixture.fetch.call_count, 5)

    def test_24_hour_deploy_window_ends_in_one_owner_action(self):
        op = journey_fixtures.fixtures.PrepareJourneyTests().operation()
        now = timezone.now()
        failure = WebsiteAuthorityError("live_route_unavailable", "The site is deploying.")
        for minutes in (2, 5, 10, 20, 40, 60, 120, 120):
            prepare.prepare_failure(op, "verify", failure, now=now)
            self.assertEqual(op.state, "pending")
            self.assertEqual(op.next_attempt_at, now + timedelta(minutes=minutes))
            now = op.next_attempt_at
        prepare.prepare_failure(op, "verify", failure, now=now + timedelta(hours=24))
        self.assertEqual(op.state, "needs_user")
        self.assertEqual(op.receipt["user_action"]["id"], "deploy_site")

    def test_real_prepare_verification_chain_marker_appears_on_third_poll(self):
        from . import website_live_fetch as live
        actual_fetch = live.fetch_live_route
        fixture = verification_fixtures.PrepareVerificationRuntimeTests()
        fixture.setUp()
        fixture.prepare.save = Mock()
        now = fixture.now
        attempts = 0
        def connection(host, address):
            client = Mock()
            path = ""
            def request(method, route, **kwargs):
                nonlocal path, attempts
                path = route
                if route == "/articles":
                    attempts += 1
            def response():
                status = 404 if attempts < 3 or path.endswith("/missing") else 200
                observed = Mock(status=status)
                observed.getheader.return_value = None
                observed.getheaders.return_value = [("x-mlai-artifact-digest", "e" * 64)]
                observed.read.return_value = b"fixture"
                return observed
            client.request.side_effect = request
            client.getresponse.side_effect = response
            return client
        with fixture.boundaries(), patch.object(live.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]), \
                patch.object(live, "PinnedHTTPSConnection", side_effect=connection):
            fixture.fetch.side_effect = actual_fetch
            for _ in range(2):
                prepare.advance_prepare(fixture.prepare.pk, now=now)
                self.assertEqual(fixture.prepare.state, "pending")
                self.assertEqual(fixture.prepare.receipt["status"], "waiting_for_deployment")
                now = fixture.prepare.next_attempt_at
            prepare.advance_prepare(fixture.prepare.pk, now=now)
            self.assertIsNotNone(fixture.live)
            prepare.advance_prepare(fixture.prepare.pk, now=fixture.prepare.next_attempt_at)
            self.assertEqual(fixture.prepare.state, "completed")
            self.assertEqual(fixture.prepare.receipt["status"], "ready")


class SetupEventRoutingTests(SimpleTestCase):
    def test_setup_failed_routes_to_saved_failure_policy(self):
        from . import service_views
        # Routing remains real; persistence callback is the only replaced seam.
        view = service_views.ContentFactoryCallbackView()
        with patch.object(service_views, "_sync_article_system_setup_callback_to_run") as sync:
            response = view._dispatch_callback_event({"run_id": "failed", "error_code": "DIRECTORY_SLOT_MISSING"},
                event_type="article_system_setup_failed", job_id="failed")
        sync.assert_called_once()
        self.assertEqual(response.status_code, 200)


class LiveRedirectTests(SimpleTestCase):
    def fetch(self, statuses):
        from . import website_live_fetch as live
        clients = []
        def connect(host, address):
            index = len(clients)
            status, location = statuses[index]
            response = Mock(status=status)
            response.getheader.side_effect = lambda key: location if key == "Location" else None
            response.getheaders.return_value = [("x-mlai-artifact-digest", "e" * 64)]
            response.read.return_value = b"fixture"
            client = Mock()
            client.getresponse.return_value = response
            clients.append((host, address, client))
            return client
        stack = ExitStack()
        dns = stack.enter_context(patch.object(live.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        stack.enter_context(patch.object(live, "PinnedHTTPSConnection", side_effect=connect))
        return stack, clients, dns

    def test_apex_www_and_trailing_slash_redirects_repin_every_hop(self):
        from .website_live_fetch import fetch_live_route
        stack, clients, dns = self.fetch([(301, "https://www.fixture.test/articles"), (308, "/articles/"), (200, None)])
        with stack:
            body, _ = fetch_live_route("https://fixture.test/articles", "fixture.test")
        self.assertEqual(body, b"fixture")
        self.assertEqual([host for host, _, _ in clients], ["fixture.test", "www.fixture.test", "www.fixture.test"])
        self.assertEqual(dns.call_count, 3)
        self.assertTrue(all(client.close.called for _, _, client in clients))

    def test_http_origin_is_upgraded_to_pinned_https(self):
        from .website_live_fetch import fetch_live_route
        stack, clients, dns = self.fetch([(200, None)])
        with stack:
            fetch_live_route("http://fixture.test/articles", "fixture.test")
        self.assertEqual(clients[0][:2], ("fixture.test", "8.8.8.8"))
        self.assertEqual(dns.call_args.args[1], 443)

    def test_offsite_path_changes_and_https_downgrade_are_refused(self):
        from .website_live_fetch import fetch_live_route
        for location in ("https://offsite.test/articles", "https://fixture.test/elsewhere", "http://fixture.test/articles"):
            stack, clients, _ = self.fetch([(302, location)])
            with self.subTest(location=location), stack, self.assertRaises(WebsiteAuthorityError) as raised:
                fetch_live_route("https://fixture.test/articles", "fixture.test")
            self.assertEqual(raised.exception.code, "deployment_origin_invalid")
            self.assertEqual(len(clients), 1)

    def test_public_redirect_to_private_address_is_refused_before_connect(self):
        from . import website_live_fetch as live
        stack, clients, dns = self.fetch([(302, "https://www.fixture.test/articles")])
        dns.side_effect = [[(2, 1, 6, "", ("8.8.8.8", 443))], [(2, 1, 6, "", ("127.0.0.1", 443))]]
        with stack, self.assertRaises(WebsiteAuthorityError) as raised:
            live.fetch_live_route("https://fixture.test/articles", "fixture.test")
        self.assertEqual(raised.exception.code, "deployment_origin_invalid")
        self.assertEqual(len(clients), 1)

    def test_404_and_5xx_wait_but_unknown_slug_404_passes(self):
        from .website_live_fetch import fetch_live_route
        for status in (404, 503):
            stack, _, _ = self.fetch([(status, None)])
            with self.subTest(status=status), stack, self.assertRaises(WebsiteAuthorityError) as raised:
                fetch_live_route("https://fixture.test/articles", "fixture.test")
            self.assertEqual(raised.exception.code, "live_route_unavailable")
        stack, _, _ = self.fetch([(404, None)])
        with stack:
            fetch_live_route("https://fixture.test/articles/missing", "fixture.test", expected_status=404)


class SourceProofTests(SimpleTestCase):
    def test_current_head_preview_clears_setup_blocker_without_publishing_authority(self):
        from . import website_connections as authority
        from .models import OrganizationContentConfig
        from .website_models import WebsiteScanSnapshot, WebsiteConnectionTarget
        for proof_type, publish_ready in (("preview_verified", False), ("verified", True), ("missing", False)):
            fixture = verification_fixtures.PrepareVerificationRuntimeTests()
            fixture.setUp()
            website, current = fixture.website, "b" * 40
            website.blockers = [{"code": "repository_source_changed", "source_sha": current}]
            proof = {"status": proof_type, "base_sha": current,
                "source_sha": "c" * 40 if proof_type == "preview_verified" else current, "preview_capable": True}
            targets = Mock()
            targets.filter.side_effect = lambda **kwargs: SimpleNamespace(first=lambda: None) if "target_key" in kwargs else []
            depth = authority._authority_depth.set(1)
            heads = authority._verified_heads.set(frozenset({(str(website.pk), website.generation, current)}))
            native = authority._verified_native_targets.set(frozenset({(str(website.pk), website.generation)}))
            try:
                with self.subTest(proof=proof_type), \
                        patch.object(type(website), "targets", targets), \
                        patch.object(type(website), "scan_snapshots", fixture.rows(first=SimpleNamespace(source_sha=current))), \
                        patch.object(OrganizationContentConfig.objects, "get", return_value=fixture.config), \
                        patch.object(WebsiteScanSnapshot.objects, "get_or_create"), \
                        patch.object(WebsiteConnectionTarget.objects, "update_or_create"):
                    authority.record_scan_evidence(website, {"source_sha": current, "publish_targets": [{
                        "target_id": "native", "delivery_adapter": "react_component", "publish_capability": "direct", "verification": proof}]})
                self.assertEqual(website.capabilities["publishingReady"], publish_ready)
                self.assertEqual(website.verified_sha, "a" * 40 if proof_type == "missing" else current)
                self.assertEqual(bool(website.blockers), proof_type == "missing")
            finally:
                authority._authority_depth.reset(depth)
                authority._verified_heads.reset(heads)
                authority._verified_native_targets.reset(native)

    def test_attestation_targets_observed_head_instead_of_old_verified_sha(self):
        from . import website_verification as verification
        fixture = verification_fixtures.PrepareVerificationRuntimeTests()
        fixture.setUp()
        current = "b" * 40
        fixture.inventory.source_sha = current
        with fixture.boundaries(), patch.object(verification, "discover_source_attestation", side_effect=
                WebsiteAuthorityError("ci_attestation_required", "Waiting for current-head CI.")) as discover:
            with self.assertRaises(WebsiteAuthorityError):
                prepare._dispatch_prepare_step(fixture.prepare, fixture.context, fixture.config, "ci-attestation", user=fixture.user,
                    journey={"repository": {"observedSourceSha": fixture.website.verified_sha, "branch": "main"}})
        self.assertEqual(discover.call_args.args[2], current)

    def test_worker_snapshot_cannot_forge_or_replace_owned_merge_sha(self):
        from .vibe_marketing_views import _merge_django_owned_article_result
        self.assertNotIn("merge_sha", _merge_django_owned_article_result({}, {"merge_sha": "forged"}))
        self.assertEqual(_merge_django_owned_article_result({"merge_sha": "saved"}, {"merge_sha": "forged"})["merge_sha"], "saved")

    def test_pending_source_work_only_closes_after_prepare_reopened(self):
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection
        from founder_tools.models import VibeRaisingCompany
        fixture = journey_fixtures.PrepareJourneyReplayTests()
        fixture.setUp()
        website = fixture.op.connection
        website.authorized_by = fixture.user
        website.authorized_by_id = fixture.user.pk
        website.operations = Mock()
        fixture.op.state = "running"
        with patch.object(WebsiteConnection.objects, "select_related") as connections, \
                patch.object(OrganizationContentConfig.objects, "filter") as configs, \
                patch.object(VibeRaisingCompany.objects, "filter") as companies, \
                patch.object(prepare, "authority_guard", return_value=nullcontext(website)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(fixture.op, False)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "filter") as operations:
            connections.return_value.filter.return_value.first.return_value = website
            configs.return_value.first.return_value = fixture.config
            companies.return_value.first.return_value = fixture.context.company
            operations.return_value.update.return_value = 0
            self.assertIsNone(prepare.wake_prepare_for_source(website.pk, organization_id=7))
        website.operations.filter.assert_not_called()
