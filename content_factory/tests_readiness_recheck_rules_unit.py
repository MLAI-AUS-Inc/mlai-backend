"""Readiness recheck rules (Stage B): footprint decisions, verify-only checks, pin and compare; no database."""
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from . import website_readiness_rules as rules
from .website_contract import WebsiteAuthorityError

NOW = datetime(2026, 11, 26, 1, 0, tzinfo=dt_timezone.utc)
VERIFIED = "e204852e" + "0" * 32
PUSHED = "14507e59" + "0" * 32
# TalaThrive's static listing: the page, its data store, the publisher and the card script.
TALA_FOOTPRINT = ["stories/index.html", "data/articles.json", "scripts/publish_articles.py", "assets/js/articles.js",
                  "scripts/templates/publication.html"]
RULES_ON = {"ARTICLES_READINESS_RECHECK_RULES_ENABLED": True, "ARTICLES_READINESS_DOMAINS": "talathrive.com"}


def compare(*files, status="ahead"):
    return {"status": status, "total_commits": 1, "files": [{"filename": name, "status": "modified"} for name in files]}


def website(**overrides):
    values = {"pk": "conn-1", "generation": 2, "state": "connected", "verified_sha": VERIFIED, "github_repo": "tala/site",
              "branch": "main", "site_url": "https://talathrive.com", "blockers": [], "capabilities": {"publishingReady": True},
              "organization": SimpleNamespace(domain="talathrive.com", pk=5), "organization_id": 5, "repository_id": 99,
              "app_root": "", "configuration_version": 4}
    values.update(overrides)
    return SimpleNamespace(**values)


def record_row(state="ready", **pin):
    contract = {"setupRunId": "e3f55f7e", "adapterMode": "model_synthesized", "routePath": "/stories",
                "footprint": TALA_FOOTPRINT, "targetKey": "stories"}
    contract.update(pin)
    return SimpleNamespace(state=state, pinned_contract=contract, recheck={}, last_look={}, verified_at=NOW - timedelta(days=8),
                           updated_at=NOW - timedelta(days=8), generation=2, pk="r-1", legacy={})


class FootprintDecisionTests(SimpleTestCase):
    """The TalaThrive shape: only the pinned footprint decides whether a push needs a check."""

    def test_a_push_outside_the_footprint_needs_no_check(self):
        decision = rules.footprint_decision(TALA_FOOTPRINT, compare("index.html", "about/index.html", "assets/css/site.css"))
        self.assertEqual(decision["rule"], "no_footprint")
        self.assertEqual(decision["changed"], [])

    def test_touching_the_listing_page_needs_a_check(self):
        decision = rules.footprint_decision(TALA_FOOTPRINT, compare("index.html", "stories/index.html"))
        self.assertEqual((decision["rule"], decision["changed"]), ("footprint", ["stories/index.html"]))

    def test_unreadable_or_diverged_compares_need_a_check(self):
        self.assertEqual(rules.footprint_decision(TALA_FOOTPRINT, None)["reason"], "compare_unreadable")
        self.assertEqual(rules.footprint_decision(TALA_FOOTPRINT, compare("a.html", status="diverged"))["rule"], "footprint")

    def test_the_pinned_footprint_wins_over_a_later_setup_cache(self):
        config = SimpleNamespace(article_system_setup_cache={"managed_files": ["src/other.ts"]})
        self.assertEqual(rules.footprint_for(record_row(), config), TALA_FOOTPRINT)
        self.assertEqual(rules.footprint_for(record_row(footprint=[]), config), ["src/other.ts"])


@override_settings(**RULES_ON)
class PushPlanTests(SimpleTestCase):
    def plan(self, site=None, *, row=None, comparison=None, sealed=False, owned=False):
        site = site or website()
        target = SimpleNamespace(target_key="stories", source_sha=VERIFIED)
        config = SimpleNamespace(default_publish_target_id="stories", article_system_setup_cache={})
        with patch("content_factory.website_readiness.stored", return_value=row if row is not None else record_row()), \
                patch.object(rules, "_default_target", return_value=target), \
                patch("content_factory.website_sealed_workflow.required", return_value=sealed), \
                patch("content_factory.website_source_carry.compare_sources", return_value=comparison) as read:
            return rules.push_plan(site, config, PUSHED, owned_merge=owned), read

    def test_a_held_site_is_judged_by_one_compare(self):
        plan, read = self.plan(comparison=compare("index.html"))
        self.assertEqual((plan["rule"], plan["from_sha"], plan["target_key"]), ("no_footprint", VERIFIED, "stories"))
        read.assert_called_once()
        self.assertEqual(read.call_args.kwargs["setup_files"], TALA_FOOTPRINT)

    def test_owned_merges_sealed_targets_and_unheld_sites_keep_todays_handling(self):
        self.assertIsNone(self.plan(owned=True)[0])
        self.assertIsNone(self.plan(sealed=True, comparison=compare("index.html"))[0])
        self.assertIsNone(self.plan(row=record_row(state="needs_user"))[0])
        self.assertIsNone(self.plan(row=record_row(setupRunId="", footprint=[]))[0])
        self.assertIsNone(self.plan(website(verified_sha=""))[0])

    def test_a_fixing_site_checks_every_push_without_a_compare(self):
        plan, read = self.plan(row=record_row(state="fixing"), comparison=compare("README.md"))
        self.assertEqual((plan["rule"], plan["reason"]), ("footprint", "record_fixing"))
        read.assert_not_called()

    def test_rules_off_or_another_domain_keep_todays_handling(self):
        with override_settings(ARTICLES_READINESS_RECHECK_RULES_ENABLED=False):
            self.assertIsNone(self.plan(comparison=compare("index.html"))[0])
        other = website(organization=SimpleNamespace(domain="elsewhere.test", pk=6))
        self.assertIsNone(self.plan(other, comparison=compare("index.html"))[0])


class ApplyPushTests(SimpleTestCase):
    def setUp(self):
        self.target = SimpleNamespace(target_key="stories", source_sha=VERIFIED, contract={"verification": {"status": "passed"}},
                                      capabilities={"publishingReady": True}, save=MagicMock())
        self.config = SimpleNamespace(default_publish_target_id="stories")
        patcher = patch.object(rules, "_default_target", return_value=self.target)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_footprint_moves_the_verified_commit_without_a_lock_or_check(self):
        site = website()
        plan = {"rule": "no_footprint", "from_sha": VERIFIED, "changed": []}
        with patch.object(rules, "_queue") as queue:
            trigger = rules.apply_push(site, self.config, plan, PUSHED, previous_revision=3)
        self.assertEqual(trigger, "push_no_footprint")
        queue.assert_not_called()
        self.assertEqual((site.verified_sha, self.target.source_sha), (PUSHED, PUSHED))
        self.assertEqual(self.target.capabilities["proofCarriedFrom"], VERIFIED)
        self.assertEqual(self.target.contract["verification"]["carried_from"], VERIFIED)
        self.assertTrue(site.capabilities["publishingReady"])
        self.assertEqual(site.blockers, [])

    def test_no_footprint_clears_an_earlier_lock_it_supersedes(self):
        site = website(blockers=[{"code": "repository_source_changed"}], capabilities={"publishingReady": False})
        rules.apply_push(site, self.config, {"rule": "no_footprint", "from_sha": VERIFIED}, PUSHED, previous_revision=3)
        self.assertEqual(site.blockers, [])
        self.assertTrue(site.capabilities["publishingReady"])

    def test_footprint_pauses_publishing_and_queues_one_verify_only_check(self):
        site = website()
        plan = {"rule": "footprint", "from_sha": VERIFIED, "changed": ["stories/index.html"], "footprint": TALA_FOOTPRINT}
        with patch.object(rules, "_queue") as queue:
            trigger = rules.apply_push(site, self.config, plan, PUSHED, previous_revision=3)
        self.assertEqual(trigger, "push_footprint")
        self.assertEqual(site.verified_sha, VERIFIED)
        self.assertFalse(site.capabilities["publishingReady"])
        self.assertEqual([item["code"] for item in site.blockers], ["repository_source_changed"])
        kwargs = queue.call_args.kwargs
        self.assertEqual((kwargs["trigger"], kwargs["coalesce"]), ("push_footprint", True))
        self.assertEqual(kwargs["key"], f"conn-1:source-reverify:2:{PUSHED}")
        self.assertEqual(kwargs["extra"]["changed_files"], ["stories/index.html"])

    def test_a_target_that_moved_meanwhile_falls_back_to_a_check(self):
        self.target.source_sha = "f" * 40
        with patch.object(rules, "_queue") as queue:
            trigger = rules.apply_push(website(), self.config, {"rule": "no_footprint", "from_sha": VERIFIED}, PUSHED,
                                       previous_revision=3)
        self.assertEqual(trigger, "push_footprint")
        queue.assert_called_once()


class VerifyOutcomeTests(SimpleTestCase):
    def checks(self, **statuses):
        return {key: {"status": value} for key, value in statuses.items()}

    def test_blog_files_and_a_build_pass(self):
        self.assertEqual(rules.verify_outcome(self.checks(blog_files="passed", build_render="passed", live_page="not_run")), "passed")

    def test_a_new_commit_needs_a_build_not_only_the_old_live_page(self):
        checks = {"blog_files": {"status": "passed"}, "build_render": {"status": "not_run"}, "live_page": {"status": "passed"}}
        self.assertEqual(rules.verify_outcome(checks, require_build=True), "unproven")
        self.assertTrue(rules.retry_worthy("unproven", checks))
        built = {**checks, "build_render": {"status": "passed"}}
        self.assertEqual(rules.verify_outcome(built, require_build=True), "passed")

    def test_without_a_build_the_live_page_must_answer(self):
        self.assertEqual(rules.verify_outcome(self.checks(blog_files="passed", build_render="not_run", live_page="passed")), "passed")
        self.assertEqual(rules.verify_outcome(self.checks(blog_files="passed", build_render="not_run", live_page="not_run")), "unproven")

    def test_file_presence_alone_never_passes_and_any_failure_fails(self):
        self.assertEqual(rules.verify_outcome(self.checks(blog_files="not_run", build_render="not_run", live_page="passed")), "unproven")
        self.assertEqual(rules.verify_outcome(self.checks(blog_files="failed", build_render="not_run", live_page="passed")), "failed")
        self.assertEqual(rules.verify_outcome(self.checks(blog_files="passed", build_render="failed", live_page="passed")), "failed")

    def test_only_a_lagging_live_page_is_retried(self):
        self.assertTrue(rules.retry_worthy("failed", self.checks(blog_files="passed", live_page="failed")))
        self.assertFalse(rules.retry_worthy("failed", self.checks(blog_files="failed", live_page="passed")))
        self.assertTrue(rules.retry_worthy("unproven", {}))


class CheckTests(SimpleTestCase):
    def tree(self, *paths, truncated=False, status=200):
        response = MagicMock(status_code=status)
        response.json.return_value = {"truncated": truncated, "tree": [{"path": path, "type": "blob"} for path in paths]}
        return response

    def test_blog_files_reads_one_tree_at_the_new_commit(self):
        with patch("integrations.services.github_connections.read_bound_repository", return_value=self.tree(*TALA_FOOTPRINT, "index.html")) as read:
            check = rules.check_blog_files(website(), PUSHED, TALA_FOOTPRINT, now=NOW)
        self.assertEqual(check["status"], "passed")
        self.assertIn(f"/git/trees/{PUSHED}?recursive=1", read.call_args.args[1])

    def test_a_missing_listing_page_fails_blog_files(self):
        present = [path for path in TALA_FOOTPRINT if path != "stories/index.html"]
        with patch("integrations.services.github_connections.read_bound_repository", return_value=self.tree(*present)):
            check = rules.check_blog_files(website(), PUSHED, TALA_FOOTPRINT, now=NOW)
        self.assertEqual((check["status"], check["evidence"]["missing"]), ("failed", ["stories/index.html"]))

    def test_an_unreadable_tree_is_not_run(self):
        for response in (self.tree(truncated=True), self.tree(status=502)):
            with patch("integrations.services.github_connections.read_bound_repository", return_value=response):
                self.assertEqual(rules.check_blog_files(website(), PUSHED, TALA_FOOTPRINT, now=NOW)["status"], "not_run")

    def test_live_page_fetches_the_pinned_blog_address(self):
        fetch = MagicMock(return_value=(b"<html>", {}))
        check = rules.check_live_page(website(), record_row(), source_sha=PUSHED, now=NOW, fetch=fetch)
        self.assertEqual(check["status"], "passed")
        fetch.assert_called_once_with("https://talathrive.com/stories", "talathrive.com")

    def test_live_page_failures(self):
        missing = MagicMock(side_effect=WebsiteAuthorityError("live_route_unavailable", "gone"))
        self.assertEqual(rules.check_live_page(website(), record_row(), now=NOW, fetch=missing)["status"], "failed")
        lagging = MagicMock(side_effect=WebsiteAuthorityError("deployment_route_transient", "later"))
        self.assertEqual(rules.check_live_page(website(), record_row(), now=NOW, fetch=lagging)["status"], "not_run")
        self.assertEqual(rules.check_live_page(website(site_url=""), record_row(), now=NOW)["status"], "not_run")

    def remote(self, enabled=True):
        return patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                     return_value={"enabled": enabled, "base_url": "https://cf.test"})

    def test_build_render_asks_content_factory_to_verify_the_pinned_setup(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "passed", "durationMs": 900,
                                      "checks": {"blog_files": {"status": "passed"}, "build_render": {"status": "passed"}}}
        post = MagicMock(return_value=response)
        op = SimpleNamespace(pk="op-9")
        with self.remote():
            check, files = rules.check_build_render(website(), record_row(), op, source_sha=PUSHED, attempt=2, now=NOW, post=post)
        self.assertEqual((check["status"], files), ("passed", "passed"))
        url, body = post.call_args.args[0], post.call_args.kwargs["json"]
        self.assertEqual(url, "https://cf.test/api/runs/e3f55f7e/verify-installed")
        self.assertEqual(body, {"head_sha": PUSHED, "github_repo": "tala/site", "website_connection_id": "conn-1",
                                "connection_generation": 2, "repository_id": 99, "operation_id": "op-9",
                                "operation_attempt": 2, "connection_target_id": "stories"})

    def test_build_render_is_not_run_when_content_factory_cannot_answer(self):
        op = SimpleNamespace(pk="op-9")
        refused = MagicMock(return_value=MagicMock(status_code=404))
        with self.remote():
            self.assertEqual(rules.check_build_render(website(), record_row(), op, source_sha=PUSHED, post=refused)[0]["status"], "not_run")
            unavailable = MagicMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={
                "status": "unavailable", "checks": {"build_render": {"status": "not_run", "evidence": {"reason": "no_contract"}}}})))
            check = rules.check_build_render(website(), record_row(), op, source_sha=PUSHED, post=unavailable)[0]
            self.assertEqual((check["status"], check["evidence"]["reason"]), ("not_run", "no_contract"))
            self.assertEqual(rules.check_build_render(website(), record_row(), op, source_sha=PUSHED,
                                                      post=MagicMock(side_effect=OSError))[0]["status"], "not_run")
        with self.remote(enabled=False):
            self.assertEqual(rules.check_build_render(website(), record_row(), op, source_sha=PUSHED, post=refused)[0]["status"], "not_run")
        self.assertEqual(rules.check_build_render(website(), record_row(setupRunId=""), op, source_sha=PUSHED)[0]["status"], "not_run")

    def test_run_checks_skips_the_build_when_files_are_missing(self):
        with patch("content_factory.website_connections.require_unlocked_remote_call"), \
                patch("content_factory.website_connections.verify_repository_head", return_value=PUSHED), \
                patch.object(rules, "check_blog_files", return_value={"status": "failed"}), \
                patch.object(rules, "check_live_page", return_value={"status": "passed"}), \
                patch.object(rules, "check_build_render") as build:
            checks = rules.run_checks(website(), record_row(), SimpleNamespace(pk="op"), source_sha=PUSHED, footprint=TALA_FOOTPRINT)
        build.assert_not_called()
        self.assertEqual(checks["build_render"]["status"], "not_run")

    def test_run_checks_stops_on_a_moved_branch(self):
        moved = WebsiteAuthorityError("website_source_changed", "moved")
        with patch("content_factory.website_connections.require_unlocked_remote_call"), \
                patch("content_factory.website_connections.verify_repository_head", side_effect=moved), \
                patch.object(rules, "check_blog_files") as files:
            with self.assertRaises(WebsiteAuthorityError):
                rules.run_checks(website(), record_row(), SimpleNamespace(pk="op"), source_sha=PUSHED, footprint=TALA_FOOTPRINT)
        files.assert_not_called()


class GateTests(SimpleTestCase):
    def test_holds_prepare_needs_rules_and_a_held_pinned_record(self):
        with override_settings(**RULES_ON), patch("content_factory.website_readiness.stored", return_value=record_row()), \
                patch.object(rules, "sealed_for", return_value=False):
            self.assertTrue(rules.holds_prepare(website()))
            self.assertTrue(rules.age_waived(website()))
        with override_settings(**RULES_ON), patch("content_factory.website_readiness.stored", return_value=record_row("fixing")), \
                patch.object(rules, "sealed_for", return_value=False):
            # A fixing site keeps the push rules (no prepare wake) but not the age waiver.
            self.assertTrue(rules.holds_prepare(website()))
            self.assertFalse(rules.age_waived(website()))
        with override_settings(**RULES_ON), patch("content_factory.website_readiness.stored", return_value=record_row("needs_user")), \
                patch.object(rules, "sealed_for", return_value=False):
            self.assertFalse(rules.holds_prepare(website()))
        with override_settings(**RULES_ON), patch("content_factory.website_readiness.stored", return_value=record_row()), \
                patch.object(rules, "sealed_for", return_value=True):
            # Sealed-CI targets keep today's preparation re-verification.
            self.assertFalse(rules.holds_prepare(website()))
        with patch("content_factory.website_readiness.stored", return_value=record_row()) as read:
            self.assertFalse(rules.holds_prepare(website()))
            self.assertFalse(rules.age_waived(website()))
        read.assert_not_called()

    def test_verify_only_marker(self):
        self.assertTrue(rules.is_verify_only(SimpleNamespace(payload={"verify_only": True})))
        self.assertFalse(rules.is_verify_only(SimpleNamespace(payload={})))

    def test_delivery_failure_queues_only_for_source_codes_with_rules_on(self):
        with override_settings(**RULES_ON), patch("django.db.transaction.on_commit") as later, \
                patch("content_factory.website_readiness.stored", return_value=record_row("checking")):
            rules.queue_after_delivery_failure(website(), WebsiteAuthorityError("website_source_changed", "moved"))
            rules.queue_after_delivery_failure(website(), WebsiteAuthorityError("website_publish_verification_required", "proof"))
            rules.queue_after_delivery_failure(website(), WebsiteAuthorityError("roo_points_required", "points"))
            rules.queue_after_delivery_failure(None, WebsiteAuthorityError("verified_target_required", "x"))
        self.assertEqual(later.call_count, 2)
        with override_settings(**RULES_ON), patch("django.db.transaction.on_commit") as later, \
                patch("content_factory.website_readiness.stored", return_value=None):
            rules.queue_after_delivery_failure(website(), WebsiteAuthorityError("website_source_changed", "moved"))
        later.assert_not_called()
        with patch("django.db.transaction.on_commit") as later:
            rules.queue_after_delivery_failure(website(), WebsiteAuthorityError("website_source_changed", "moved"))
        later.assert_not_called()

    def test_unknown_verify_only_trigger_is_refused(self):
        with self.assertRaises(ValueError):
            rules.queue_verify_only(website(), trigger="backfill")


class SetupPinTests(SimpleTestCase):
    def pin(self, row, *, fresh=False, **flags):
        settings = {"ARTICLES_READINESS_PIN_ENABLED": True, "ARTICLES_READINESS_DOMAINS": "talathrive.com", **flags}
        with override_settings(**settings), patch("content_factory.website_readiness.stored", return_value=row):
            return rules.setup_pin(website(), fresh=fresh)

    def test_the_pinned_setup_and_its_strategy_are_sent(self):
        self.assertEqual(self.pin(record_row()), {"pinned_setup_run_id": "e3f55f7e", "strategy_pin": "model_adapter"})
        for mode, rung in (("agent", "agent"), ("native", "native_collection"), ("deterministic", "deterministic")):
            self.assertEqual(self.pin(record_row(adapterMode=mode))["strategy_pin"], rung)
        self.assertEqual(self.pin(record_row(adapterMode="")), {"pinned_setup_run_id": "e3f55f7e"})

    def test_a_pin_from_another_generation_is_never_sent(self):
        self.assertEqual(self.pin(record_row(generation=1)), {})
        self.assertEqual(self.pin(record_row(generation=2))["pinned_setup_run_id"], "e3f55f7e")

    def test_no_pin_when_off_fresh_or_unpinned(self):
        self.assertEqual(self.pin(record_row(), ARTICLES_READINESS_PIN_ENABLED=False), {})
        self.assertEqual(self.pin(record_row(), fresh=True), {})
        self.assertEqual(self.pin(record_row(setupRunId="")), {})
        self.assertEqual(self.pin(None), {})


class LookAndCompareTests(SimpleTestCase):
    def test_a_look_is_due_weekly(self):
        self.assertTrue(rules.look_due(record_row(), now=NOW))
        recent = record_row()
        recent.last_look = {"at": (NOW - timedelta(days=2)).isoformat(), "failures": 0}
        self.assertFalse(rules.look_due(recent, now=NOW))

    def test_one_head_read_and_one_live_fetch(self):
        ok, checks = rules.look(website(), record_row(), now=NOW, head=lambda: PUSHED, fetch=MagicMock())
        self.assertTrue(ok)
        self.assertEqual(set(checks), {"repository", "live_page"})

    def test_a_failed_look_never_raises(self):
        def broken():
            raise OSError("github down")
        ok, checks = rules.look(website(), record_row(), now=NOW, head=broken,
                                fetch=MagicMock(side_effect=WebsiteAuthorityError("live_route_unavailable", "gone")))
        self.assertFalse(ok)
        self.assertEqual((checks["repository"]["status"], checks["live_page"]["status"]), ("failed", "failed"))

    def test_compare_reports_only_flips(self):
        self.assertIsNone(rules.compare_row(record_row(), {"state": "ready"}))
        flip = rules.compare_row(record_row(), {"state": "fixing", "reason_code": "integration_required", "legacy": {"state": "not_ready"}})
        self.assertEqual(flip, {"from": "ready", "to": "fixing", "reasonCode": "integration_required", "legacy": "not_ready"})

    @override_settings(ARTICLES_READINESS_NIGHTLY_COMPARE_ENABLED=False, ARTICLES_READINESS_RECHECK_RULES_ENABLED=False)
    def test_sweeps_off(self):
        self.assertEqual(rules.run_nightly_compare_sweep(now=NOW), {"skipped": "flag_off"})
        self.assertEqual(rules.run_weekly_look_sweep(now=NOW), {"skipped": "flag_off"})

    def test_manual_check_needs_the_rules(self):
        config = SimpleNamespace(website_connection=website())
        with self.assertRaises(WebsiteAuthorityError) as raised:
            rules.request_manual_check(config, now=NOW)
        self.assertEqual(raised.exception.authority_code, "source_reverification_unavailable")


class UnsupportedBuildTests(SimpleTestCase):
    """Non-static stacks (Next, Astro, React Router): Content Factory cannot build them in place."""

    def answer(self, reason, *, build_status="not_run", status=200, **evidence):
        response = MagicMock(status_code=status)
        response.json.return_value = {"status": "unavailable", "checks": {
            "blog_files": {"status": "passed"},
            "build_render": {"status": build_status, "evidence": {"reason": reason, **evidence}}}}
        return MagicMock(return_value=response)

    def cf_unsupported(self, cause, **overrides):
        """Content Factory's exact answer for a stack it cannot build in place (setup_verify_installed._unsupported)."""
        evidence = {"unsupported": True, "cause": cause, "framework": "nextjs", "runtime_family": "node", **overrides}
        return self.answer(evidence.pop("reason", rules.RENDER_UNSUPPORTED_FOR_STACK), **evidence)

    def build(self, post):
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                   return_value={"enabled": True, "base_url": "https://cf.test"}):
            return rules.check_build_render(website(), record_row(), SimpleNamespace(pk="op-9"), source_sha=PUSHED, now=NOW, post=post)[0]

    def test_an_unsupported_stack_is_marked_with_a_plain_detail(self):
        self.assertEqual(rules.RENDER_UNSUPPORTED_FOR_STACK, "render_unsupported_for_stack")
        for cause in ("no_static_materialization_contract", "witness_not_required"):
            check = self.build(self.cf_unsupported(cause))
            self.assertEqual((check["status"], check["evidence"]["unsupported"], check["evidence"]["reason"]),
                             ("not_run", True, rules.RENDER_UNSUPPORTED_FOR_STACK))
            self.assertEqual((check["evidence"]["cause"], check["evidence"]["framework"]), (cause, "nextjs"))
            self.assertEqual(check["detail"], rules.UNSUPPORTED_BUILD_DETAIL)
            self.assertTrue(rules.build_unsupported(check))

    def test_only_content_factorys_exact_signal_is_unsupported(self):
        """The old reasons, a static site with an incomplete contract and half signals all keep retrying."""
        not_the_signal = [
            # Older Content Factory answers used these as the reason (also for an incomplete static contract).
            self.answer("no_static_materialization_contract"), self.answer("witness_not_required"),
            self.answer("static_contract_incomplete", unsupported=False),
            self.answer("render_unavailable", transient=True),
            self.answer("blog_files_not_passed"), self.answer("access_unconfirmed", transient=True),
            self.answer("website_unreachable", transient=True),
            # The reason without the flag, the flag without the reason, or a non-boolean flag.
            self.answer(rules.RENDER_UNSUPPORTED_FOR_STACK),
            self.answer("no_static_materialization_contract", unsupported=True),
            self.cf_unsupported("witness_not_required", unsupported="true"),
            # The exact signal on anything but not_run, or on a refused answer.
            self.cf_unsupported("no_static_materialization_contract", build_status="unavailable"),
            self.cf_unsupported("no_static_materialization_contract", status=409),
        ]
        for post in not_the_signal:
            check = self.build(post)
            self.assertEqual(check["status"], "not_run")
            self.assertNotIn("unsupported", check["evidence"])
            self.assertFalse(rules.build_unsupported(check))
            self.assertEqual(rules.verify_outcome(self.checks(check), require_build=True), "unproven")

    def test_transient_answers_are_never_unsupported(self):
        transient = [self.answer("no_static_materialization_contract", status=status) for status in (404, 409, 502, 503)]
        transient += [self.answer(""), self.answer("no_contract"), MagicMock(side_effect=TimeoutError),
                      self.answer("no_static_materialization_contract", build_status="unavailable")]
        for post in transient:
            check = self.build(post)
            self.assertEqual(check["status"], "not_run")
            self.assertFalse(rules.build_unsupported(check))

    def checks(self, build, live="passed", files="passed"):
        return {"blog_files": {"status": files}, "build_render": build, "live_page": {"status": live}}

    def test_unsupported_promotes_on_blog_files_and_live_page_even_after_a_push(self):
        unsupported = {"status": "not_run", "evidence": {"unsupported": True}}
        self.assertEqual(rules.verify_outcome(self.checks(unsupported), require_build=True), "passed")
        self.assertEqual(rules.verify_outcome(self.checks(unsupported), require_build=False), "passed")
        self.assertEqual(rules.verify_outcome(self.checks(unsupported, live="not_run"), require_build=True), "unproven")
        self.assertEqual(rules.verify_outcome(self.checks(unsupported, files="not_run"), require_build=True), "unproven")
        self.assertEqual(rules.verify_outcome(self.checks(unsupported, live="failed"), require_build=True), "failed")

    def test_a_transient_not_run_keeps_retrying_after_a_push(self):
        for build in ({"status": "not_run", "evidence": {"reason": "verify_installed_unavailable", "httpStatus": 503}},
                      {"status": "not_run", "evidence": {"reason": "no_static_materialization_contract"}},
                      {"status": "not_run"}):
            checks = self.checks(build)
            self.assertEqual(rules.verify_outcome(checks, require_build=True), "unproven")
            self.assertTrue(rules.retry_worthy("unproven", checks))


class RunFailureRecheckTests(SimpleTestCase):
    """Content Factory's article delivery failure callback queues one verify-only check (never a setup)."""

    def run_row(self, **overrides):
        values = {"run_id": "run-1", "workflow": "direct_generate", "domain": "talathrive.com",
                  "run_request": {"website_connection_id": "conn-1"}}
        values.update(overrides)
        return SimpleNamespace(**values)

    def call(self, run, code, *, site=None, row="checking", flags=RULES_ON):
        from .website_models import WebsiteConnection
        site = site or website()
        objects = MagicMock()
        objects.select_related.return_value.filter.return_value.first.return_value = site
        with override_settings(**flags), patch.object(WebsiteConnection, "objects", objects), \
                patch("django.db.transaction.on_commit") as later, \
                patch("content_factory.website_readiness.stored", return_value=record_row(row) if row else None):
            rules.queue_after_run_failure(run, code)
        return later

    def test_source_and_target_codes_queue_after_commit(self):
        for code in ("SOURCE_CHANGED", "WEBSITE_SOURCE_CHANGED", "website_source_changed", "verified_target_required",
                     "PUBLISH_TARGET_UNVERIFIED", "PUBLISHING_ADAPTER_REQUIRED"):
            self.assertEqual(self.call(self.run_row(), code).call_count, 1, code)

    def test_other_codes_setup_runs_and_foreign_domains_are_ignored(self):
        self.call(self.run_row(), "INTERNAL_ERROR").assert_not_called()
        self.call(self.run_row(), "").assert_not_called()
        self.call(self.run_row(workflow="article_system_setup"), "SOURCE_CHANGED").assert_not_called()
        self.call(self.run_row(workflow="repo_scan"), "SOURCE_CHANGED").assert_not_called()
        self.call(self.run_row(run_request={}), "SOURCE_CHANGED").assert_not_called()
        self.call(self.run_row(domain="other.example"), "SOURCE_CHANGED").assert_not_called()
        self.call(None, "SOURCE_CHANGED").assert_not_called()

    def test_flag_domain_and_record_gate(self):
        self.call(self.run_row(), "SOURCE_CHANGED", flags={}).assert_not_called()
        self.call(self.run_row(), "SOURCE_CHANGED", flags={**RULES_ON, "ARTICLES_READINESS_DOMAINS": "other.example"}).assert_not_called()
        self.call(self.run_row(), "SOURCE_CHANGED", row=None).assert_not_called()
        self.call(self.run_row(), "SOURCE_CHANGED", row="needs_user").assert_not_called()

    def test_a_broken_lookup_never_raises(self):
        from .website_models import WebsiteConnection
        with override_settings(**RULES_ON), patch.object(WebsiteConnection, "objects", MagicMock(side_effect=RuntimeError)):
            self.assertIsNone(rules.queue_after_run_failure(self.run_row(), "SOURCE_CHANGED"))


class DirectSetupPinTests(SimpleTestCase):
    """A founder's own POST to the setup view (not a prepare dispatch) forwards the pin too."""

    def pin(self, data, *, route_path="/stories", row=None, **flags):
        settings = {"ARTICLES_READINESS_PIN_ENABLED": True, "ARTICLES_READINESS_DOMAINS": "talathrive.com", **flags}
        with override_settings(**settings), patch("content_factory.website_readiness.stored", return_value=row or record_row()):
            return rules.direct_setup_pin(website(), data, route_path=route_path)

    def test_the_pin_is_forwarded(self):
        expected = {"pinned_setup_run_id": "e3f55f7e", "strategy_pin": "model_adapter"}
        self.assertEqual(self.pin({}), expected)
        self.assertEqual(self.pin({}, route_path="/stories/"), expected)
        self.assertEqual(self.pin({}, route_path=""), expected)

    def test_set_up_again_or_another_route_sends_no_pin(self):
        for data in ({"set_up_again": True}, {"setUpAgain": "true"}, {"repair_setup": 1}, {"repairSetup": "yes"}):
            self.assertEqual(self.pin(data), {}, data)
        self.assertEqual(self.pin({}, route_path="/blog"), {})
        self.assertEqual(self.pin({"set_up_again": False})["pinned_setup_run_id"], "e3f55f7e")

    def test_flag_off_or_no_connection_sends_no_pin(self):
        self.assertEqual(self.pin({}, ARTICLES_READINESS_PIN_ENABLED=False), {})
        self.assertEqual(rules.direct_setup_pin(None, {}, route_path="/stories"), {})
