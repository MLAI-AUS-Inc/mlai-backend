"""Verified websites stay verified while their source is unchanged.

TalaThrive (16-25 Oct 2026): setup finished on 16 Oct and nothing changed in
its repository, yet seven days later every article was blocked with
``verification_stale``. The only refresh paths were a pushed source or the
developer CI-evidence form ("Recheck verification"), so the founder could not
recover on their own. A sweep now re-attests an unchanged source before its
proof lapses, an expired journey offers the server's own re-check, and a
retried completed preparation re-verifies instead of replaying. These run
without a database or provider access.
"""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import vibe_marketing_views  # noqa: F401 -- decorates with transaction.atomic at import; load before it is patched
from . import website_prepare as prepare
from . import website_reconciliation as reconciliation
from .activation import VERIFICATION_MAX_AGE, VERIFICATION_REFRESH_AFTER
from .website_contract import WebsiteAuthorityError
from .website_journey import deployment_receipt_current
from . import tests_one_click_operations_unit as fixtures

SOURCE = "a" * 40
TARGET = "static_markdown_stories_slug"


def rows(*, first=None, items=()):
    """A chainable stand-in for the few queryset calls under test."""
    query = MagicMock()
    for method in ("filter", "exclude", "order_by", "select_related"):
        getattr(query, method).return_value = query
    query.first.return_value = first
    query.__getitem__.side_effect = lambda _: list(items)
    query.update.return_value = 1
    return query


class RefreshEligibilityTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        verified = self.now - timedelta(days=9)
        self.target = SimpleNamespace(target_key=TARGET, source_sha=SOURCE, verified_at=verified,
            capabilities={"publishingReady": True}, adapter="static_markdown", contract={"contract_digest": "d" * 64})
        self.accepted = SimpleNamespace(receipt={"attestation_origin": "authenticated_ci", "allowed": True})
        self.connection = SimpleNamespace(pk=uuid4(), state="connected", branch="main", app_root="", verified_sha=SOURCE,
            generation=3, configuration_version=7, last_verified_at=verified, capabilities={"publishingReady": True}, blockers=[],
            targets=rows(first=self.target), operations=rows(first=self.accepted))
        self.config = SimpleNamespace(default_publish_target_id=TARGET, website_connection=self.connection)

    def target_for(self, *, observed=SOURCE, sealed=False):
        with patch("content_factory.website_connections.observed_source_sha", return_value=observed), \
                patch("content_factory.website_sealed_workflow.required", return_value=sealed):
            return reconciliation._refresh_target(self.connection, self.config)

    def test_an_unchanged_ci_verified_source_is_refreshed(self):
        self.assertIs(self.target_for(), self.target)
        self.assertIs(self.target_for(observed=""), self.target)
        self.connection.operations.filter.assert_called_with(generation=3, action="ci-verify", state="completed",
            payload__source_sha=SOURCE, payload__target_id=TARGET)

    def test_a_pushed_or_unverifiable_source_is_left_to_its_own_flow(self):
        cases = {
            "newer observed head": lambda: {"observed": "b" * 40},
            "push blocker": lambda: self.connection.blockers.append({"code": "repository_source_changed"}) or {},
            "paused": lambda: setattr(self.connection, "state", "paused") or {},
            "readiness revoked": lambda: self.connection.capabilities.update(publishingReady=False) or {},
            "application root": lambda: setattr(self.connection, "app_root", "web") or {},
            "target at another source": lambda: setattr(self.target, "source_sha", "c" * 40) or {},
            "unverified target": lambda: setattr(self.target, "verified_at", None) or {},
            "custom contract": lambda: setattr(self.target, "adapter", "custom_contract_v1") or {},
            "sealed execution proof": lambda: {"sealed": True},
            "hosted attestation": lambda: self.accepted.receipt.update(attestation_origin="authenticated_hosted") or {},
            "no accepted CI receipt": lambda: setattr(self.connection, "operations", rows(first=None)) or {},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                self.assertIsNone(self.target_for(**change()))

    def test_one_check_is_queued_per_accepted_verification(self):
        created = SimpleNamespace(pk=uuid4())
        configs = rows(items=[self.config])
        with patch.object(reconciliation.OrganizationContentConfig.objects, "select_related", return_value=configs), \
                patch.object(reconciliation, "_refresh_target", return_value=self.target), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create", return_value=(created, True)) as reserve:
            queued = reconciliation.queue_verification_refreshes(now=self.now)
        self.assertEqual(queued, [str(created.pk)])
        due = configs.filter.call_args_list[0].kwargs
        self.assertEqual(due, {"website_connection__state": "connected",
            "website_connection__last_verified_at__lte": self.now - VERIFICATION_REFRESH_AFTER})
        self.assertEqual(VERIFICATION_MAX_AGE - VERIFICATION_REFRESH_AFTER, timedelta(days=2))
        stamp = self.target.verified_at.isoformat()
        kwargs = reserve.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], f"{self.connection.pk}:verification-refresh:3:{SOURCE}:{stamp}")
        defaults = kwargs["defaults"]
        self.assertEqual((defaults["action"], defaults["generation"]), ("source-reverify", 3))
        self.assertEqual(defaults["payload"], {"source_sha": SOURCE, "target_id": TARGET, "scan_required": False,
            "owned_merge_run_id": "", "purpose": "verification_refresh", "verified_at": stamp, "configuration_revision": 7})

    def test_an_existing_check_for_this_verification_is_not_queued_again(self):
        configs = rows(items=[self.config])
        with patch.object(reconciliation.OrganizationContentConfig.objects, "select_related", return_value=configs), \
                patch.object(reconciliation, "_refresh_target", return_value=self.target), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create", return_value=(Mock(), False)):
            self.assertEqual(reconciliation.queue_verification_refreshes(now=self.now), [])

    def test_a_failed_sweep_never_stops_other_website_work(self):
        def filtered(*args, **kwargs):
            query = Mock()
            query.exclude.return_value.order_by.return_value.values_list.return_value = ["op-1"]
            query.exists.return_value = kwargs.get("action") == "superseded-setup-cleanup"
            return query
        with patch.object(reconciliation, "queue_verification_refreshes", side_effect=RuntimeError("provider down")), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter", side_effect=filtered), \
                patch.object(reconciliation, "monitor_cleanup_pull_requests"), \
                patch("content_factory.website_support.reconcile_support_verifications"), \
                patch.object(reconciliation, "_process_superseded_setup_cleanup", return_value="completed") as processed:
            report = reconciliation.process_website_connection_operations(limit=5)
        processed.assert_called_once()
        self.assertEqual(report["verification_refreshes_queued"], 0)


class LiveCheckRefreshTests(SimpleTestCase):
    """A passed live check ages out on its own while the CI proof is current (Vyavos, 25 Oct 2026)."""

    def setUp(self):
        self.now = timezone.now()
        self.threshold = self.now - VERIFICATION_REFRESH_AFTER
        self.target = SimpleNamespace(target_key=TARGET, source_sha=SOURCE, verified_at=self.now - timedelta(hours=3))
        self.live = SimpleNamespace(receipt={"status": "passed", "checked_at": (self.now - timedelta(days=8)).isoformat()})
        self.connection = SimpleNamespace(pk=uuid4(), generation=3, configuration_version=7, verified_sha=SOURCE,
            last_verified_at=self.now - timedelta(hours=3), operations=rows(first=self.live))
        self.config = SimpleNamespace(default_publish_target_id=TARGET, website_connection=self.connection)

    def test_only_a_passed_live_check_older_than_the_refresh_age_is_due(self):
        stamp = reconciliation._aged_live_check(self.connection, self.target, self.threshold)
        self.assertEqual(stamp.isoformat(), self.live.receipt["checked_at"])
        # Publication reads the latest live check of this exact source and target.
        self.connection.operations.filter.assert_called_with(generation=3, action="deployment-verify", state="completed",
            payload__source_sha=SOURCE, payload__target_id=TARGET)
        self.connection.operations.order_by.assert_called_with("-created_at")
        cases = {
            "recent": {"status": "passed", "checked_at": (self.now - timedelta(days=4)).isoformat()},
            "not passed": {"status": "failed", "checked_at": self.live.receipt["checked_at"]},
            "no stamp": {"status": "passed"},
            "naive stamp": {"status": "passed", "checked_at": "2026-10-17T00:00:00"},
        }
        for name, receipt in cases.items():
            with self.subTest(name):
                self.live.receipt = receipt
                self.assertIsNone(reconciliation._aged_live_check(self.connection, self.target, self.threshold))
        with self.subTest("never passed"):
            self.connection.operations = rows(first=None)
            self.assertIsNone(reconciliation._aged_live_check(self.connection, self.target, self.threshold))

    def test_an_aged_live_check_is_queued_for_its_live_routes(self):
        created = SimpleNamespace(pk=uuid4())
        configs = rows(items=[self.config])
        with patch.object(reconciliation.OrganizationContentConfig.objects, "select_related", return_value=configs), \
                patch.object(reconciliation, "_refresh_target", return_value=self.target), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create", return_value=(created, True)) as reserve:
            queued = reconciliation._queue_live_check_refreshes(limit=5, threshold=self.threshold)
        self.assertEqual(queued, [str(created.pk)])
        # Only sites with current CI proof; the CI-age sweep renews both checks of the rest.
        self.assertEqual(configs.filter.call_args_list[0].kwargs, {"website_connection__state": "connected",
            "website_connection__last_verified_at__gt": self.threshold})
        stamp = self.live.receipt["checked_at"]
        kwargs = reserve.call_args.kwargs
        self.assertEqual(kwargs["idempotency_key"], f"{self.connection.pk}:verification-refresh:3:{SOURCE}:deployment:{stamp}")
        self.assertEqual(kwargs["defaults"]["payload"], {"source_sha": SOURCE, "target_id": TARGET, "scan_required": False,
            "owned_merge_run_id": "", "purpose": "verification_refresh", "verified_at": stamp, "configuration_revision": 7,
            "renew": "deployment"})

    def test_a_current_live_check_or_ineligible_site_is_not_queued(self):
        configs = rows(items=[self.config])
        for name, target, live in (("current", self.target, {"status": "passed", "checked_at": self.now.isoformat()}),
                ("ineligible", None, self.live.receipt)):
            with self.subTest(name):
                self.live.receipt = live
                with patch.object(reconciliation.OrganizationContentConfig.objects, "select_related", return_value=configs), \
                        patch.object(reconciliation, "_refresh_target", return_value=target), \
                        patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create") as reserve:
                    self.assertEqual(reconciliation._queue_live_check_refreshes(limit=5, threshold=self.threshold), [])
                reserve.assert_not_called()

    def test_the_sweep_gives_the_live_pass_what_is_left_of_its_limit(self):
        configs = rows(items=[])
        with patch.object(reconciliation.OrganizationContentConfig.objects, "select_related", return_value=configs), \
                patch.object(reconciliation, "_queue_live_check_refreshes", return_value=["live-1"]) as live:
            self.assertEqual(reconciliation.queue_verification_refreshes(limit=3, now=self.now, connection_id="c-1"), ["live-1"])
        live.assert_called_once_with(limit=3, threshold=self.threshold, connection_id="c-1")


class RefreshCheckTests(SimpleTestCase):
    """The queued refresh is the ordinary source check at an unchanged commit."""

    def setUp(self):
        self.now = timezone.now()
        self.target = SimpleNamespace(target_key=TARGET, generation=3, contract={"contract_digest": "d" * 64})
        self.website = SimpleNamespace(pk=uuid4(), generation=3, github_repo="synthetic/site", repository_id=42, app_root="",
            branch="main", organization=SimpleNamespace(domain="synthetic.test"),
            targets=SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(first=lambda: self.target)))

    def process(self, *, purpose="verification_refresh", age=timedelta(minutes=1), error=None, renew=None, current=None):
        from . import website_connections as authority
        from . import website_verification as verification
        op = SimpleNamespace(pk=uuid4(), connection=self.website, generation=3, attempts=0, next_attempt_at=None, save=Mock(),
            created_at=self.now - age, receipt={"status": "verification_queued", "repository_modified": False},
            payload={"source_sha": SOURCE, "target_id": TARGET, "scan_required": False, "owned_merge_run_id": "", "purpose": purpose,
                **({"renew": renew} if renew else {})})
        proof = {"source_sha": SOURCE, "evidence_digest": "e" * 64}
        ci = SimpleNamespace(pk=uuid4(), receipt={**proof, "checked_at": self.now.isoformat()})
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as stored, \
                patch.object(authority, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(self.website)), \
                patch.object(authority, "require_unlocked_remote_call"), \
                patch.object(verification, "discover_source_attestation", return_value=proof, side_effect=error) as discover, \
                patch.object(verification, "record_ci_attestation", return_value=ci) as record, \
                patch.object(verification, "current_ci_attestation", return_value=current) as reused, \
                patch.object(verification, "verify_live_deployment", return_value=SimpleNamespace(pk=uuid4())) as live, \
                patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=SimpleNamespace()), \
                patch("integrations.http_client.post", side_effect=AssertionError("A same-source refresh never rescans")):
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            state = reconciliation._process_source_reverification(op.pk, self.now)
        self.reused = reused
        return state, stored.return_value.update.call_args.kwargs, discover, record, live, ci

    def test_a_refresh_reads_new_provider_proof_at_the_same_commit(self):
        state, written, discover, record, live, ci = self.process()
        self.assertEqual((state, written["state"], written["receipt"]["status"]), ("completed", "completed", "verified"))
        # The sealed GitHub check and the live route are read again; no saved stamp is copied.
        discover.assert_called_once_with(self.website, self.target, SOURCE)
        record.assert_called_once_with(discover.return_value)
        live.assert_called_once()
        self.assertEqual(live.call_args.kwargs["data"], ci.receipt)

    def test_a_live_check_refresh_reuses_the_current_ci_receipt(self):
        current = {"source_sha": SOURCE, "evidence_digest": "f" * 64, "checked_at": self.now.isoformat()}
        state, written, discover, record, live, _ = self.process(renew="deployment", current=current)
        self.assertEqual((state, written["receipt"]["status"], written["receipt"]["ci_reused"]), ("completed", "verified", True))
        self.reused.assert_called_once_with(self.website, self.target)
        discover.assert_not_called()
        record.assert_not_called()
        self.assertEqual(live.call_args.kwargs["data"], current)

    def test_a_live_check_refresh_reads_ci_again_when_its_receipt_no_longer_counts(self):
        state, written, discover, record, live, ci = self.process(renew="deployment", current=None)
        self.assertEqual((state, written["receipt"]["ci_reused"]), ("completed", False))
        discover.assert_called_once_with(self.website, self.target, SOURCE)
        self.assertEqual(live.call_args.kwargs["data"], ci.receipt)

    def test_a_full_refresh_never_reuses_the_ci_receipt(self):
        *_, discover, record, live, ci = self.process(current={"source_sha": SOURCE})
        self.reused.assert_not_called()
        discover.assert_called_once()

    def test_a_refresh_that_cannot_succeed_stops_instead_of_retrying_hourly(self):
        denied = WebsiteAuthorityError("github_ci_evidence_permission_required", "Approve Checks: read.", retryable=False)
        state, written, *_ = self.process(error=denied)
        self.assertEqual((state, written["state"]), ("needs_attention", "needs_attention"))
        self.assertEqual((written["receipt"]["code"], written["receipt"]["retryable"]), ("github_ci_evidence_permission_required", False))

    def test_a_waiting_refresh_retries_for_a_day_then_stops(self):
        waiting = WebsiteAuthorityError("ci_attestation_required", "Waiting for native CI proof.", retryable=True)
        state, written, *_ = self.process(error=waiting, age=timedelta(hours=2))
        self.assertEqual((state, written["receipt"]["status"], written["receipt"]["retryable"]), ("pending", "verification_pending", True))
        state, written, *_ = self.process(error=waiting, age=timedelta(hours=25))
        self.assertEqual((state, written["receipt"]["status"], written["receipt"]["retryable"]), ("needs_attention", "needs_attention", False))

    def test_a_push_check_keeps_its_existing_retry_policy(self):
        denied = WebsiteAuthorityError("github_ci_evidence_permission_required", "Approve Checks: read.", retryable=False)
        state, written, *_ = self.process(purpose=None, error=denied, age=timedelta(days=3))
        self.assertEqual((state, written["state"]), ("pending", "pending"))


class PageOpenRefreshTests(SimpleTestCase):
    """Opening Articles runs a due refresh instead of waiting for the scheduler."""

    def setUp(self):
        self.now = timezone.now()
        self.organization = SimpleNamespace(pk=7)

    def open_page(self, *, connection_id="c-1", fresh_claim=True, due="op-1"):
        configs = rows()
        configs.values_list.return_value.first.return_value = connection_id
        pending = rows()
        pending.values_list.return_value.first.return_value = due
        with patch.object(reconciliation.OrganizationContentConfig.objects, "filter", return_value=configs), \
                patch("django.core.cache.cache.add", return_value=fresh_claim) as claim, \
                patch.object(reconciliation, "queue_verification_refreshes", return_value=[]) as queue, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter", return_value=pending) as find, \
                patch.object(reconciliation, "_process_source_reverification", return_value="completed") as run:
            result = reconciliation.refresh_verification_for_page(self.organization, now=self.now)
        return result, claim, queue, find, run

    def test_a_due_refresh_runs_while_the_page_loads(self):
        result, claim, queue, find, run = self.open_page()
        self.assertEqual(result, "completed")
        claim.assert_called_once_with("website-verification-refresh:page:c-1", "1", 600)
        queue.assert_called_once_with(limit=1, now=self.now, connection_id="c-1")
        find.assert_called_once_with(connection_id="c-1", action="source-reverify", state="pending",
            payload__purpose="verification_refresh")
        run.assert_called_once_with("op-1", self.now)

    def test_nothing_runs_without_a_due_refresh_a_website_or_a_free_claim(self):
        for name, kwargs in {"nothing due": {"due": None}, "no website": {"connection_id": None},
                "opened in the last ten minutes": {"fresh_claim": False}}.items():
            with self.subTest(name):
                result, _claim, _queue, _find, run = self.open_page(**kwargs)
                self.assertIsNone(result)
                run.assert_not_called()

    def test_articles_bootstrap_refreshes_first_and_still_loads_when_that_fails(self):
        context = SimpleNamespace(organization=self.organization)
        calls = Mock()
        calls.serialize.return_value = {"latestRuns": []}
        with patch.object(vibe_marketing_views, "_resolve_profile_company_or_response",
                    return_value=(None, SimpleNamespace(domain="synthetic.test"), None)), \
                patch.object(vibe_marketing_views, "_resolve_context_or_response", return_value=(context, None)), \
                patch.object(vibe_marketing_views, "_serialize_bootstrap", calls.serialize), \
                patch.object(reconciliation, "refresh_verification_for_page", calls.refresh), \
                self.assertLogs(vibe_marketing_views.logger, level="ERROR"):
            calls.refresh.side_effect = RuntimeError("provider down")
            response = vibe_marketing_views.VibeMarketingBootstrapView().get(SimpleNamespace(query_params={}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([name for name, *_ in calls.mock_calls], ["refresh", "serialize"])
        calls.refresh.assert_called_once_with(self.organization)

    @override_settings(WEBSITE_VERIFICATION_PAGE_REFRESH_ENABLED=False)
    def test_the_page_trigger_can_be_switched_off(self):
        result, claim, queue, _find, run = self.open_page()
        self.assertIsNone(result)
        claim.assert_not_called()
        queue.assert_not_called()
        run.assert_not_called()


class DeploymentReceiptAgeTests(SimpleTestCase):
    def test_the_journey_uses_the_publication_age_limit(self):
        now = timezone.now()
        self.assertTrue(deployment_receipt_current({"checked_at": (now - timedelta(days=6)).isoformat()}, now=now))
        self.assertFalse(deployment_receipt_current({"checked_at": (now - timedelta(days=8)).isoformat()}, now=now))
        self.assertFalse(deployment_receipt_current({"checked_at": (now + timedelta(hours=1)).isoformat()}, now=now))
        self.assertFalse(deployment_receipt_current({"checked_at": "2026-10-20T00:00:00"}, now=now))
        self.assertFalse(deployment_receipt_current({}, now=now))


class CheckWebsiteAgainTests(SimpleTestCase):
    """Retrying a completed preparation re-verifies a lapsed website."""

    def setUp(self):
        self.op = fixtures.PrepareJourneyTests().operation()
        self.op.state = "completed"
        self.op.payload = {**self.op.payload, "attempt": 4, "user_clicks": 2, "run_id": "old-child", "repair_setup": True}
        self.op.receipt = {"status": "ready", "current_step": "ready", "message": "Your website is ready for articles.",
            "scan_run_id": "scan-1", "setup_run_id": "setup-1", "completed_steps": ["ci-attestation", "verify"]}
        self.website = self.op.connection
        self.website.operations = rows()
        self.config = SimpleNamespace(website_connection=self.website)

    def start(self, *, retry=True, evidence=None):
        evidence = evidence or {"verified": False, "reasonCode": "verification_stale"}
        with patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "filter", return_value=rows(first=None)), \
                patch("content_factory.activation.durable_integration_evidence", return_value=evidence) as read:
            result = prepare.start_prepare(self.config, expected={}, retry=retry)
        self.assertIs(result, self.op)
        return read

    def test_expired_verification_reopens_the_server_recheck(self):
        read = self.start()
        read.assert_called_once_with(self.config, self.website)
        self.assertEqual((self.op.state, self.op.receipt["status"], self.op.receipt["message"]),
            ("pending", "working", "Checking your website again."))
        self.assertEqual(self.op.payload["attempt"], 5)
        self.assertEqual(self.op.payload["user_clicks"], 3)
        self.assertTrue(self.op.payload["source_reverify"])
        self.assertNotIn("run_id", self.op.payload)
        self.assertNotIn("repair_setup", self.op.payload)
        for key in ("scan_run_id", "setup_run_id", "completed_steps"):
            self.assertNotIn(key, self.op.receipt)
        self.assertIsNone(self.op.receipt["child_run_id"])
        self.op.save.assert_called_once()
        # A queued refresh is now owned by this preparation, as after a push.
        self.website.operations.filter.assert_called_once_with(generation=3, action="source-reverify", state="pending")
        self.assertEqual(self.website.operations.update.call_args.kwargs["receipt"]["status"], "managed_by_prepare")

    def test_an_expired_live_receipt_also_reopens_it(self):
        self.start(evidence={"verified": True, "publishingVerified": False, "reasonCode": ""})
        self.assertEqual(self.op.state, "pending")

    def test_a_ready_website_stays_a_harmless_replay(self):
        self.start(evidence={"verified": True, "publishingVerified": True, "reasonCode": ""})
        self.assertEqual((self.op.state, self.op.receipt["dispatch_outcome"], self.op.payload["attempt"]), ("completed", "replayed", 4))
        self.website.operations.filter.assert_not_called()

    def test_lapses_a_recheck_cannot_repair_stay_replays(self):
        for code in ("website_writes_paused", "website_publishing_paused", "publishing_adapter_required", "repository_changed"):
            with self.subTest(code=code):
                self.setUp()
                self.start(evidence={"verified": False, "reasonCode": code})
                self.assertEqual(self.op.state, "completed")

    def test_a_source_wake_without_retry_is_unchanged(self):
        read = self.start(retry=False)
        read.assert_not_called()
        self.assertEqual((self.op.state, self.op.receipt["dispatch_outcome"]), ("completed", "replayed"))

    def test_the_owner_recheck_runs_the_ci_reattestation_step(self):
        op = fixtures.PrepareJourneyTests().operation()
        config = SimpleNamespace(website_connection=op.connection, website_connection_id=op.connection_id)
        context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=SimpleNamespace(pk=7))
        journey = {"nextAction": {"id": "prepare", "label": "Check website again", "step": "preview"},
            "capabilities": {"canPublishArticle": False}}
        handler = Mock(return_value={"status": "completed"})
        with patch.object(prepare, "_claim_prepare", return_value=op), \
                patch.object(prepare, "_prepare_context", return_value=(context, config, SimpleNamespace(pk=4))), \
                patch.object(prepare, "journey_for_context", return_value=journey), \
                patch.object(prepare, "_persist_prepare", side_effect=lambda operation, **kwargs: operation):
            prepare.advance_prepare(op.pk, handlers={"ci-attestation": handler})
        handler.assert_called_once()
        self.assertEqual(op.receipt["current_step"], "ci-attestation")
        self.assertEqual(op.state, "pending")
