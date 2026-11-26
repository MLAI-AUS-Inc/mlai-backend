"""Website readiness record: state mapping, projection, capability reads and shadow hooks; no database."""
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from . import website_readiness as readiness

NOW = datetime(2026, 11, 26, 1, 0, tzinfo=dt_timezone.utc)
SHA = "a" * 40
NEW = "b" * 40


def connection(**overrides):
    values = {"pk": "conn-1", "generation": 3, "state": "connected", "verified_sha": SHA, "last_verified_at": NOW,
              "blockers": [], "installation_id": "42", "repository_id": 7, "github_repo": "owner/site", "branch": "main",
              "updated_at": NOW, "site_url": "https://fixture.test/", "organization_id": 9,
              "organization": SimpleNamespace(domain="talathrive.com", pk=9)}
    values.update(overrides)
    return SimpleNamespace(**values)


def legacy(verified=False, code="", publishing=True):
    return {"verified": verified, "publishingVerified": publishing if verified else None, "reasonCode": code}


def op(state, payload=None, created_at=NOW, **receipt):
    return SimpleNamespace(state=state, receipt=receipt, payload=payload or {}, created_at=created_at, pk="op-1")


def row(**overrides):
    values = {"state": "ready", "can_draft": True, "can_publish": True, "reason_code": "", "reason": "Ready to write",
              "verified_sha": SHA, "checks": {}, "pinned_contract": {"routePath": "/stories"}, "recheck": {},
              "updated_at": NOW, "generation": 3, "last_known_good_at": None}
    values.update(overrides)
    return SimpleNamespace(**values)


class ClassifyTests(SimpleTestCase):
    def test_verified_and_publishing_verified_is_ready(self):
        self.assertEqual(readiness.classify(connection(), legacy(True)), ("ready", ""))

    def test_verified_without_publishing_proof_is_checking(self):
        self.assertEqual(readiness.classify(connection(), legacy(True, publishing=False)), ("checking", "live_check_pending"))

    def test_stale_left_after_the_age_waiver_is_never_ready(self):
        # derive() re-judges aged proof with the age bound waived; a stale code
        # left over is a missing or future stamp, never age alone.
        self.assertEqual(readiness.classify(connection(), legacy(code="verification_stale")), ("fixing", "verification_stale"))
        self.assertEqual(readiness.classify(connection(last_verified_at=None), legacy(code="verification_stale"))[0], "not_set_up")

    def test_a_verify_only_access_failure_stays_needs_user(self):
        self.assertEqual(readiness.classify(connection(), legacy(code="integration_required"), failed_recheck="github_access_required"),
                         ("needs_user", "github_access_required"))
        self.assertEqual(readiness.classify(connection(), legacy(code="integration_required"), failed_recheck="failed"),
                         ("fixing", "integration_required"))

    def test_disconnected_and_revoked_are_not_set_up(self):
        self.assertEqual(readiness.classify(connection(state="disconnected"), legacy())[0], "not_set_up")
        self.assertEqual(readiness.classify(connection(state="revoked"), legacy()), ("not_set_up", "github_access_required"))

    def test_founder_asks_are_needs_user(self):
        for code in ("github_access_required", "repository_changed", "CONNECTION_READ_ONLY"):
            self.assertEqual(readiness.classify(connection(), legacy(code=code)), ("needs_user", code))
        blocked = connection(blockers=[{"code": "github_write_required"}])
        self.assertEqual(readiness.classify(blocked, legacy(code="integration_required")), ("needs_user", "github_write_required"))

    def test_source_change_with_open_recheck_is_checking(self):
        state = readiness.classify(connection(), legacy(code="repository_source_changed"), recheck={"sourceSha": NEW})
        self.assertEqual(state, ("checking", "repository_source_changed"))
        running = readiness.prepare_outcome(op("running"))
        self.assertEqual(readiness.classify(connection(), legacy(code="repository_source_changed"), prepare=running)[0], "checking")

    def test_failed_recheck_or_prepare_after_a_source_change_is_fixing(self):
        failed = readiness.prepare_outcome(op("needs_attention", failure_class="repairable", reason_code="DIRECTORY_ROUTE_SOURCE_MISSING"))
        self.assertEqual(readiness.classify(connection(), legacy(code="repository_source_changed"), prepare=failed),
                         ("fixing", "DIRECTORY_ROUTE_SOURCE_MISSING"))
        self.assertEqual(readiness.classify(connection(), legacy(code="integration_required"), failed_recheck=True)[0], "fixing")

    def test_never_verified_site_is_not_set_up(self):
        fresh = connection(verified_sha="", last_verified_at=None)
        self.assertEqual(readiness.classify(fresh, legacy(code="integration_required")), ("not_set_up", "integration_required"))
        failed = readiness.prepare_outcome(op("needs_attention", failure_class="repairable"))
        self.assertEqual(readiness.classify(fresh, legacy(code="integration_required"), prepare=failed)[0], "not_set_up")

    def test_prepare_waiting_on_the_founder_is_needs_user(self):
        waiting = readiness.prepare_outcome(op("needs_user", reason_code="deploy_site", user_action={"label": "Deploy your site"}))
        self.assertEqual(readiness.classify(connection(verified_sha=""), legacy(code="integration_required"), prepare=waiting),
                         ("needs_user", "deploy_site"))
        self.assertEqual(readiness._needs_user_reason("deploy_site", waiting[1]), "Deploy your site to keep publishing.")

    def test_prepare_outcomes(self):
        self.assertEqual(readiness.prepare_outcome(None), (None, {}))
        self.assertEqual(readiness.prepare_outcome(op("completed"))[0], "completed")
        self.assertEqual(readiness.prepare_outcome(op("stopped", status="cant_be_done_yet"))[0], "closed")
        self.assertEqual(readiness.prepare_outcome(op("needs_attention", status="staff_fixing"))[0], "fixing")

    def test_legacy_state_words(self):
        self.assertEqual(readiness.legacy_state(legacy(True)), "ready")
        self.assertEqual(readiness.legacy_state(legacy(code="verification_stale")), "not_ready")
        self.assertEqual(readiness.legacy_state(legacy(code="github_access_required")), "needs_user")


class FlagTests(SimpleTestCase):
    @override_settings(ARTICLES_READINESS_READS_ENABLED=True, ARTICLES_READINESS_DOMAINS="talathrive.com, mlai.au")
    def test_domain_flags_need_the_listed_domain(self):
        self.assertTrue(readiness.readiness_enabled("reads", SimpleNamespace(domain="TalaThrive.com")))
        self.assertFalse(readiness.readiness_enabled("reads", SimpleNamespace(domain="other.test")))
        self.assertFalse(readiness.readiness_enabled("reads", None))
        self.assertFalse(readiness.readiness_enabled("pin", SimpleNamespace(domain="mlai.au")))

    @override_settings(ARTICLES_READINESS_SHADOW_ENABLED=False, ARTICLES_READINESS_NIGHTLY_COMPARE_ENABLED=True)
    def test_global_flags(self):
        self.assertFalse(readiness.readiness_enabled("shadow"))
        self.assertTrue(readiness.readiness_enabled("nightly_compare"))
        self.assertFalse(readiness.readiness_enabled("unknown"))


class DeriveTests(SimpleTestCase):
    def derive(self, evidence, aged=None, **patches):
        target = SimpleNamespace(target_key="articles", adapter="static_dropin_v1", adapter_version="1", contract={},
                                 source_sha=SHA, verified_at=NOW - timedelta(days=9), capabilities={})

        def legacy_answer(config, site, now, *, ignore_age=False):
            return aged if ignore_age else evidence
        with patch.object(readiness, "_legacy", side_effect=legacy_answer) as self.legacy_calls, \
                patch.object(readiness, "_default_target", return_value=target), \
                patch.object(readiness, "_latest_op", return_value=patches.get("prepare")), \
                patch.object(readiness, "_open_recheck", return_value=patches.get("recheck", {})), \
                patch.object(readiness, "_failed_recheck", return_value=False), \
                patch.object(readiness, "pinned_contract", return_value={"footprint": ["stories/index.html"], "routePath": "/stories"}), \
                patch("content_factory.website_live_verification.build_proof", return_value=True):
            return readiness.derive(SimpleNamespace(), connection(), now=NOW)

    def test_stale_proof_derives_ready_and_notes_age_on_the_check(self):
        record = self.derive(legacy(code="verification_stale"), aged=legacy(True))
        self.assertEqual((record["state"], record["can_draft"], record["can_publish"]), ("ready", True, True))
        self.assertEqual(record["legacy"]["reasonCode"], "verification_stale")
        self.assertEqual(record["legacy"]["state"], "not_ready")
        self.assertTrue(record["checks"]["build_render"]["evidence"]["stale"])
        self.assertEqual(set(record["checks"]), set(readiness.CHECK_KEYS))

    def test_aged_proof_still_meets_every_other_check(self):
        # A sealed-CI target with no current attestation is not ready once its age is waived.
        record = self.derive(legacy(code="verification_stale"), aged=legacy(code="verification_required"))
        self.assertEqual((record["state"], record["can_publish"]), ("fixing", False))
        # Aged proof that is not yet publishing-verified is only checking.
        record = self.derive(legacy(code="verification_stale"), aged=legacy(True, publishing=False))
        self.assertEqual((record["state"], record["can_publish"]), ("checking", False))
        # A missing or future stamp stays stale even with the age waived: never ready.
        record = self.derive(legacy(code="verification_stale"), aged=legacy(code="verification_stale"))
        self.assertNotEqual(record["state"], "ready")
        self.assertEqual(record["legacy"]["reasonCode"], "verification_stale")

    def test_fresh_proof_is_judged_once(self):
        self.derive(legacy(True))
        self.assertEqual(self.legacy_calls.call_count, 1)

    def test_checking_keeps_drafts_and_pauses_publishing(self):
        recheck = {"reason": "source_changed", "sourceSha": NEW, "queuedAt": NOW.isoformat(), "operationId": "op-2"}
        record = self.derive(legacy(code="repository_source_changed"), recheck=recheck)
        self.assertEqual((record["state"], record["can_draft"], record["can_publish"]), ("checking", True, False))
        self.assertEqual(record["recheck"]["sourceSha"], NEW)
        self.assertEqual(record["reason"], readiness.FOUNDER_COPY["checking"][0])

    def test_needs_user_reason_names_the_ask(self):
        record = self.derive(legacy(code="github_access_required"))
        self.assertEqual(record["reason"], "GitHub access to your website was removed.")
        self.assertFalse(record["can_draft"])


class FootprintTests(SimpleTestCase):
    def test_cache_first_then_applied_setup_mutation(self):
        config = SimpleNamespace(article_system_setup_cache={"managed_files": ["stories/index.html", "data/articles.json"]})
        self.assertEqual(readiness.footprint(config, connection(), "run-1")[1], "setup_cache")
        mutation = SimpleNamespace(files=[{"path": "scripts/publish_articles.py"}, {"path": "assets/js/articles.js"}])
        site = connection(repository_mutations=MagicMock())
        site.repository_mutations.filter.return_value.order_by.return_value.first.return_value = mutation
        paths, source = readiness.footprint(SimpleNamespace(article_system_setup_cache={}), site, "run-1")
        self.assertEqual((paths, source), (["assets/js/articles.js", "scripts/publish_articles.py"], "setup_mutation"))
        self.assertEqual(readiness.footprint(SimpleNamespace(article_system_setup_cache={}), connection(), ""), ([], "derived"))

    def test_adapter_mode_reads_nested_setup_result(self):
        self.assertEqual(readiness.adapter_mode({"article_system_setup": {"adapter_mode": "model_synthesized"}}), "model_synthesized")
        self.assertEqual(readiness.adapter_mode({"adapter_mode": "other"}), "")


class RecordValuesTests(SimpleTestCase):
    def derived(self, state="ready", pin=None):
        return {"state": state, "reason": readiness.FOUNDER_COPY[state][0], "pinned_contract": pin or {"setupRunId": "new"},
                "checks": {key: {"status": "unknown"} for key in readiness.CHECK_KEYS}}

    def test_stored_pin_survives_everything_but_a_completed_setup(self):
        stored = row(pinned_contract={"setupRunId": "e3f55f7e", "adapterMode": "model_synthesized"})
        kept = readiness._next_values(stored, self.derived(), trigger="push_footprint", state=None, checks=None, now=NOW)
        self.assertEqual(kept["pinned_contract"]["setupRunId"], "e3f55f7e")
        failed = readiness._next_values(stored, self.derived(), trigger="setup_completed", state=None, checks=None, now=NOW,
                                        detail={"prepare_state": "needs_attention"})
        self.assertEqual(failed["pinned_contract"]["setupRunId"], "e3f55f7e")
        repinned = readiness._next_values(stored, self.derived(), trigger="setup_completed", state=None, checks=None, now=NOW)
        self.assertEqual(repinned["pinned_contract"]["setupRunId"], "new")

    def test_a_new_generation_drops_the_old_pin(self):
        old = row(generation=3, pinned_contract={"setupRunId": "e3f55f7e", "generation": 3})
        reset = {**self.derived(pin={"setupRunId": "", "footprint": []}), "generation": 4}
        values = readiness._next_values(old, reset, trigger="access_change", state=None, checks=None, now=NOW)
        self.assertEqual(values["pinned_contract"]["setupRunId"], "")
        # A pin stamped with another generation is dropped even on a current-generation row.
        stale = row(generation=4, pinned_contract={"setupRunId": "e3f55f7e", "generation": 3})
        values = readiness._next_values(stale, reset, trigger="manual", state=None, checks=None, now=NOW)
        self.assertEqual(values["pinned_contract"]["setupRunId"], "")

    def test_explicit_state_and_checks_for_rule_paths(self):
        values = readiness._next_values(row(), self.derived(), trigger="push_footprint", state="checking",
                                        checks={"blog_files": {"status": "passed"}, "bogus": {}}, now=NOW)
        self.assertEqual((values["state"], values["can_draft"], values["can_publish"]), ("checking", True, False))
        self.assertEqual(values["checks"]["blog_files"]["status"], "passed")
        self.assertNotIn("bogus", values["checks"])
        self.assertIsNone(values["last_known_good_at"])
        self.assertEqual(readiness._next_values(row(), self.derived(), trigger="manual", state=None, checks=None,
                                                now=NOW)["last_known_good_at"], NOW)

    def test_record_rejects_unknown_vocabulary(self):
        with self.assertRaises(ValueError):
            readiness.record("conn-1", trigger="push")
        with self.assertRaises(ValueError):
            readiness.record("conn-1", trigger="manual", result="ok")
        with self.assertRaises(ValueError):
            readiness.record("conn-1", trigger="manual", state="blocked")


class ProjectionTests(SimpleTestCase):
    def test_compact_client_shape(self):
        checks = {"build_render": {"status": "passed", "checkedAt": "2026-11-20T00:00:00+00:00", "detail": "Built."},
                  "live_page": {"status": "weird"}}
        recheck = {"reason": "source_changed", "queuedAt": "2026-11-25T00:00:00+00:00"}
        value = readiness.projection(row(state="checking", can_publish=False, checks=checks, recheck=recheck),
                                     site_url="https://talathrive.com/", reads_enabled=True)
        self.assertEqual(set(value), {"version", "state", "canDraft", "canPublish", "reasonCode", "reason", "blogAddress",
                                      "verifiedSha", "lastCheckedAt", "checks", "recheck", "readsEnabled"})
        self.assertEqual(value["blogAddress"], "https://talathrive.com/stories")
        self.assertEqual([item["key"] for item in value["checks"]], list(readiness.CHECK_KEYS))
        self.assertEqual(value["checks"][0]["label"], "GitHub access")
        self.assertEqual(value["checks"][5]["status"], "unknown")
        self.assertEqual(value["lastCheckedAt"], "2026-11-20T00:00:00+00:00")
        self.assertEqual(value["recheck"], {"reason": "source_changed", "since": "2026-11-25T00:00:00+00:00"})
        self.assertTrue(value["readsEnabled"])
        self.assertIsNone(readiness.projection(row(), site_url="")["blogAddress"])
        self.assertIsNone(readiness.projection(row())["recheck"])

    def test_founder_payload_is_none_without_a_current_record(self):
        config = SimpleNamespace(website_connection_id=None, pk=1)
        self.assertIsNone(readiness.founder_payload(config))
        with patch.object(readiness, "stored", side_effect=RuntimeError("boom")), \
                self.assertLogs("content_factory.website_readiness", level="WARNING"):
            self.assertIsNone(readiness.founder_payload(SimpleNamespace(website_connection_id="x", website_connection=connection(), pk=1)))


class CapabilityTests(SimpleTestCase):
    BASE = {"canGenerateArticle": False, "canPublishArticle": False, "stage": "verifying", "reasonCode": "verification_stale",
            "reason": "Check your website integration again.", "publishingReasonCode": "verification_stale", "publishingReason": "x"}

    def test_ready_record_answers_draft_and_publish(self):
        value = readiness.apply_to_capabilities(self.BASE, row(), account={"owned": True})
        self.assertTrue(value["canGenerateArticle"] and value["canPublishArticle"])
        self.assertEqual((value["stage"], value["reasonCode"], value["publishingReasonCode"]), ("ready", "", ""))

    def test_fixing_record_allows_drafts_only(self):
        fixing = row(state="fixing", can_publish=False, reason_code="DIRECTORY_ROUTE_SOURCE_MISSING",
                     reason=readiness.FOUNDER_COPY["fixing"][0])
        value = readiness.apply_to_capabilities(self.BASE, fixing, account={"owned": True})
        self.assertTrue(value["canGenerateArticle"])
        self.assertFalse(value["canPublishArticle"])
        self.assertEqual(value["publishingReasonCode"], "DIRECTORY_ROUTE_SOURCE_MISSING")

    def test_record_never_grants_a_founder_without_their_own_github(self):
        value = readiness.apply_to_capabilities(self.BASE, row(), account={"owned": False})
        self.assertFalse(value["canGenerateArticle"] or value["canPublishArticle"])

    def test_needs_user_for_access_is_the_github_stage(self):
        value = readiness.apply_to_capabilities(self.BASE, row(state="needs_user", can_draft=False, can_publish=False,
                                                               reason_code="github_access_required"), account={"owned": True})
        self.assertEqual(value["stage"], "github")

    def test_a_definite_live_access_denial_keeps_todays_refusal(self):
        for code in ("github_access_required", "repository_changed", "CONNECTION_READ_ONLY"):
            value = readiness.apply_to_capabilities(self.BASE, row(), account={"owned": True},
                                                    repository_access={"verified": False, "reasonCode": code})
            self.assertFalse(value["canGenerateArticle"] or value["canPublishArticle"])
            self.assertEqual(value["reasonCode"], "verification_stale")  # the legacy reason stands
        read_only = readiness.apply_to_capabilities(self.BASE, row(), account={"owned": True},
                                                    repository_access={"verified": True, "writable": False})
        self.assertTrue(read_only["canGenerateArticle"])
        self.assertFalse(read_only["canPublishArticle"])
        self.assertEqual(read_only["publishingReasonCode"], "github_write_required")

    def test_an_unknown_live_answer_leaves_the_record_answer(self):
        value = readiness.apply_to_capabilities(self.BASE, row(), account={"owned": True},
                                                repository_access={"verified": False, "reasonCode": "github_verification_required"})
        self.assertTrue(value["canGenerateArticle"] and value["canPublishArticle"])


class ArticleCapabilitiesReadTests(SimpleTestCase):
    """``_article_capabilities_for_context`` with reads on: no downgrade on a new live HEAD, record answers."""

    def call(self, stored):
        from . import vibe_marketing_views as views
        context = SimpleNamespace(organization=SimpleNamespace(domain="talathrive.com", pk=9), profile=None)
        config = SimpleNamespace(website_connection_id="conn-1", github_repo="owner/site")
        evidence = {"verified": True, "branch": "main", "sha": SHA, "reasonCode": ""}
        access = {"verified": True, "branch": "main", "sha": NEW}
        seen = {}

        def capabilities(config, **kwargs):
            seen.update(kwargs)
            return {"canGenerateArticle": kwargs["evidence"]["verified"], "canPublishArticle": False, "stage": "x",
                    "reasonCode": kwargs["evidence"].get("reasonCode", "")}
        with patch.object(views, "_latest_runs_for_org", return_value=[]), \
                patch.object(views, "_github_account_for_context", return_value={"saved": True, "owned": True}), \
                patch.object(views, "_article_system_setup_gate", return_value={}), \
                patch.object(views, "resolve_article_system", return_value={}), \
                patch.object(views, "integration_evidence", return_value=evidence), \
                patch.object(views, "_verify_github_repository_access", return_value=access), \
                patch.object(views, "article_capabilities", side_effect=capabilities), \
                patch("content_factory.first_article_entitlement.entitlement_state", return_value=None), \
                patch.object(readiness, "for_reads", return_value=stored):
            return views._article_capabilities_for_context(context, config), seen

    def test_without_a_record_a_new_head_still_downgrades(self):
        value, seen = self.call(None)
        self.assertEqual(seen["evidence"]["reasonCode"], "verification_stale")
        self.assertFalse(value["canGenerateArticle"])

    def test_with_a_ready_record_a_new_head_does_not_downgrade(self):
        value, seen = self.call(row())
        self.assertTrue(seen["evidence"]["verified"])
        self.assertTrue(value["canGenerateArticle"] and value["canPublishArticle"])
        self.assertEqual(value["readinessState"], "ready")


class ShadowTests(SimpleTestCase):
    @override_settings(ARTICLES_READINESS_SHADOW_ENABLED=False)
    def test_flag_off_records_nothing(self):
        with patch.object(readiness.transaction, "on_commit") as on_commit:
            readiness.shadow("conn-1", "manual")
        on_commit.assert_not_called()

    @override_settings(ARTICLES_READINESS_SHADOW_ENABLED=True)
    def test_runs_after_commit_and_only_logs_failures(self):
        with patch.object(readiness.transaction, "on_commit", side_effect=lambda fn: fn()), \
                patch.object(readiness, "record", side_effect=RuntimeError("db down")) as record, \
                self.assertLogs("content_factory.website_readiness", level="WARNING") as logs:
            readiness.shadow(connection(), "push_footprint", source_sha=NEW)
        record.assert_called_once_with("conn-1", trigger="push_footprint", source_sha=NEW)
        self.assertIn("website_readiness_shadow_failed", logs.output[0])

    @override_settings(ARTICLES_READINESS_SHADOW_ENABLED=True)
    def test_unavailable_transaction_only_logs(self):
        with patch.object(readiness.transaction, "on_commit", side_effect=AssertionError("Database access forbidden")), \
                self.assertLogs("content_factory.website_readiness", level="WARNING"):
            readiness.shadow("conn-1", "manual")

    @override_settings(ARTICLES_READINESS_SHADOW_ENABLED=True)
    def test_run_hook_finds_the_bound_connection(self):
        with patch.object(readiness, "shadow") as shadow:
            readiness.shadow_for_run(SimpleNamespace(run_request={"website_connection_id": "conn-9"}), "setup_completed")
            readiness.shadow_for_run(SimpleNamespace(run_request={}), "setup_completed")
        shadow.assert_called_once_with("conn-9", "setup_completed")


class StaffTests(SimpleTestCase):
    def test_check_events_newest_first_shape(self):
        event = SimpleNamespace(id="e1", created_at=NOW, generation=3, trigger="push_footprint", source_sha=NEW,
                                changed_files=["stories/index.html"], checks={}, result="recorded", state_before="ready",
                                state_after="checking", code_version="abc", detail={"carry": "setup_files_changed"})
        site = SimpleNamespace(check_events=MagicMock())
        site.check_events.order_by.return_value.__getitem__.return_value = [event]
        rows = readiness.check_events(site)
        site.check_events.order_by.assert_called_once_with("-created_at", "-pk")
        self.assertEqual(rows[0]["stateAfter"], "checking")
        self.assertEqual(rows[0]["changedFiles"], ["stories/index.html"])

    def test_fleet_counts_every_state(self):
        rows = [{"readiness__state": "ready", "total": 3}, {"readiness__state": None, "total": 2}]
        with patch("content_factory.website_models.WebsiteConnection.objects") as objects:
            objects.filter.return_value.values.return_value.annotate.return_value = rows
            counts = readiness.readiness_fleet()
        self.assertEqual(counts, {"ready": 3, "checking": 0, "needs_user": 0, "fixing": 0, "not_set_up": 0, "none": 2})


class BackfillTests(SimpleTestCase):
    def test_dry_run_counts_states_and_lists_differences(self):
        from .management.commands import backfill_website_readiness as command
        site = connection()
        configs = [SimpleNamespace(website_connection=site), SimpleNamespace(website_connection=connection(pk="conn-2"))]
        derived = [{"state": "ready", "reason_code": "", "legacy": {"state": "not_ready", "reasonCode": "verification_stale"}},
                   {"state": "fixing", "reason_code": "x", "legacy": {"state": "fixing"}}]
        with patch.object(command, "_configs", return_value=configs), \
                patch.object(readiness, "derive", side_effect=derived), \
                patch.object(readiness, "record") as record:
            summary = command.backfill()
        record.assert_not_called()
        self.assertEqual(summary["counts"]["ready"], 1)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual([item["connectionId"] for item in summary["differs"]], ["conn-1"])

    def test_apply_records_with_the_backfill_trigger(self):
        from .management.commands import backfill_website_readiness as command
        with patch.object(command, "_configs", return_value=[SimpleNamespace(website_connection=connection())]), \
                patch.object(readiness, "derive", return_value={"state": "ready", "reason_code": "", "legacy": {"state": "ready"}}), \
                patch.object(readiness, "record") as record:
            summary = command.backfill(apply=True)
        self.assertEqual(record.call_args.kwargs["trigger"], "backfill")
        self.assertEqual(summary["differs"], [])
