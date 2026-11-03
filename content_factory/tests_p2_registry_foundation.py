"""The P2 foundation (be-registry): settings, the cohort and stamp, private keys and the test helpers.

Covers the acceptance lines for Settings, IF-P2-26 (including M12 and L3),
IF-P2-28 and the IF-P2-24 seam helper.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from . import p2_foundation
from .self_heal_testing import (
    ALL_FLAGS_ON, ALL_P1_FLAGS_ON, ALL_P2_FLAGS_ON, P2_FLAG_NAMES, P2_SETTING_DEFAULTS, RECEIPT_BUILDERS,
    fake_member_context, fake_provider, make_named_legacy_org, make_p2_org, make_stamped_run, make_unstamped_run,
    missing_provider, ops_alert_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_BLOCK_MARKER = "# P2 founder journey (\"self-healing\")."


def _settings_block() -> str:
    text = (ROOT / "mlai/settings.py").read_text()
    index = text.index(SETTINGS_BLOCK_MARKER)
    return text[index:]


def _user(email=None):
    from uuid import uuid4
    from core.models import User
    return User.objects.create_user(email=email or f"p2-{uuid4().hex[:10]}@synthetic.example.test")


class P2SettingsTests(SimpleTestCase):
    """Acceptance: Settings."""

    def test_every_setting_exists_with_its_documented_default(self):
        for name, default in P2_SETTING_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertTrue(hasattr(settings, name), name)
                self.assertEqual(getattr(settings, name), default)
        for name in P2_FLAG_NAMES:
            self.assertIs(getattr(settings, name), False)

    def test_all_p2_flags_on_lists_every_boolean_in_the_settings_block(self):
        block = _settings_block()
        booleans = set(re.findall(r'^([A-Z][A-Z0-9_]+) = _env_is_true\("\1", False\)$', block, flags=re.M))
        self.assertEqual(booleans, set(ALL_P2_FLAGS_ON))
        self.assertEqual(len(booleans), 23)
        others = set(re.findall(r'^([A-Z][A-Z0-9_]+) = (?:int\()?os\.getenv\("\1"', block, flags=re.M))
        self.assertEqual(booleans | others, set(P2_SETTING_DEFAULTS))
        self.assertTrue(all(value is True for value in ALL_P2_FLAGS_ON.values()))

    def test_the_settings_block_is_appended_at_the_end(self):
        text = (ROOT / "mlai/settings.py").read_text()
        self.assertGreater(text.index(SETTINGS_BLOCK_MARKER), text.index("COMMUNITY_CHAT_INVITE_RESOLVE_RATE = "))

    def test_env_example_lists_every_setting(self):
        text = (ROOT / ".env.example").read_text()
        for name, default in P2_SETTING_DEFAULTS.items():
            with self.subTest(name=name):
                expected = "false" if default is False else "" if default == "" else str(default)
                self.assertRegex(text, rf"(?m)^{name}={re.escape(expected)}$")

    def test_middleware_and_cors_header(self):
        self.assertEqual(settings.MIDDLEWARE[-1], "content_factory.staff_signals.ThrottleSignalMiddleware")
        self.assertEqual(settings.MIDDLEWARE.count("content_factory.staff_signals.ThrottleSignalMiddleware"), 1)
        self.assertEqual([header for header in settings.CORS_ALLOW_HEADERS if header == "idempotency-key"],
                         ["idempotency-key"])


class P2CohortTests(TestCase):
    """IF-P2-26: ``p2_applies`` and ``legacy_opt_in_ids``."""

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_new_journey_orgs_are_in_the_cohort(self):
        self.assertTrue(p2_foundation.p2_applies(make_p2_org().organization))

    @override_settings(**ALL_FLAGS_ON)
    def test_legacy_orgs_only_by_opt_in(self):
        legacy = make_named_legacy_org("la_vie").organization
        self.assertFalse(p2_foundation.p2_applies(legacy))
        with override_settings(ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS=f" {legacy.pk} "):
            self.assertTrue(p2_foundation.p2_applies(legacy))

    def test_flags_off_nobody_is_in_the_cohort(self):
        self.assertFalse(p2_foundation.p2_applies(make_p2_org().organization))
        self.assertFalse(p2_foundation.p2_applies(None))

    def test_malformed_opt_in_entries_are_ignored(self):
        legacy = make_named_legacy_org("vyavos").organization
        raw = f"abc, -3, 0, , 5x, {legacy.pk}.0, ٣, {legacy.pk + 1000}"
        with override_settings(ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS=raw):
            self.assertEqual(p2_foundation.legacy_opt_in_ids(), frozenset({legacy.pk + 1000}))
            self.assertFalse(p2_foundation.p2_applies(legacy))
        with override_settings(ARTICLES_SELF_HEAL_LEGACY_ORGANIZATIONS=f"junk,{legacy.pk}"):
            self.assertTrue(p2_foundation.p2_applies(legacy))

    @override_settings(**ALL_FLAGS_ON)
    def test_policy_v2_and_the_stamp(self):
        p2 = make_p2_org().organization
        legacy = make_named_legacy_org("mlai").organization
        self.assertEqual(p2_foundation.policy_stamp(p2), {"version": 1, "cohort": "p2", "failure_policy": "v2"})
        self.assertEqual(p2_foundation.policy_stamp(legacy), {"version": 1, "cohort": "legacy", "failure_policy": "pre_p2"})
        with override_settings(ARTICLES_FAILURE_POLICY_V2_ENABLED=False):
            self.assertEqual(p2_foundation.policy_stamp(p2), {"version": 1, "cohort": "p2", "failure_policy": "pre_p2"})


class P2StampTests(TestCase):
    """IF-P2-26: ``stamp_run_request`` is idempotent and never changes an existing stamp."""

    @override_settings(**ALL_FLAGS_ON)
    def test_stamp_is_idempotent(self):
        p2 = make_p2_org().organization
        payload = {"workflow": "article_generation"}
        stamped = p2_foundation.stamp_run_request(payload, organization=p2)
        self.assertIs(stamped, payload)
        self.assertEqual(payload["self_heal_policy_v1"], {"version": 1, "cohort": "p2", "failure_policy": "v2"})
        self.assertEqual(payload["merge_policy"], "backend_worker")
        before = dict(payload)
        legacy = make_named_legacy_org("pawshare").organization
        self.assertEqual(p2_foundation.stamp_run_request(payload, organization=legacy), before)

    @override_settings(**ALL_FLAGS_ON)
    def test_an_existing_stamp_or_merge_policy_is_never_changed(self):
        p2 = make_p2_org().organization
        payload = {"self_heal_policy_v1": {"version": 1, "cohort": "legacy", "failure_policy": "pre_p2"}}
        before = dict(payload["self_heal_policy_v1"])
        p2_foundation.stamp_run_request(payload, organization=p2)
        self.assertEqual(payload["self_heal_policy_v1"], before)
        self.assertEqual(payload["merge_policy"], "backend_worker")
        payload = {"merge_policy": "something_else"}
        p2_foundation.stamp_run_request(payload, organization=p2)
        self.assertEqual(payload["merge_policy"], "something_else")

    @override_settings(**ALL_FLAGS_ON)
    def test_legacy_runs_get_a_legacy_stamp_and_no_merge_policy(self):
        built = make_named_legacy_org("talathrive")
        payload = p2_foundation.stamp_run_request({}, organization=built.organization, requester_user_id=built.owner.pk)
        self.assertEqual(payload, {"self_heal_policy_v1": {"version": 1, "cohort": "legacy", "failure_policy": "pre_p2"}})
        self.assertEqual(p2_foundation.stamp_run_request("not a dict", organization=built.organization), "not a dict")


class MergePolicyTests(TestCase):
    """IF-P2-26 and IF-P2-15: ``merge_policy_for``, including M12 and L3."""

    @override_settings(**ALL_FLAGS_ON)
    def test_worker_flag_on_for_a_p2_org(self):
        built = make_p2_org()
        self.assertEqual(p2_foundation.merge_policy_for(built.organization, requester_user_id=built.owner.pk),
                         "backend_worker")
        with override_settings(ARTICLES_MERGE_WORKER_ENABLED=False):
            self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=built.owner.pk))

    @override_settings(**ALL_FLAGS_ON)
    def test_none_for_a_legacy_org(self):
        for name in ("talathrive", "vyavos", "pawshare", "la_vie", "mlai"):
            with self.subTest(name=name):
                built = make_named_legacy_org(name)
                self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=built.owner.pk))

    def test_m12_member_without_publish_rights_with_every_flag_off(self):
        built = make_named_legacy_org("talathrive")
        member, publisher = _user(), _user()
        with fake_member_context(members={member.pk, publisher.pk}, publishers={publisher.pk}, owner_ids={built.owner.pk}):
            self.assertEqual(p2_foundation.merge_policy_for(built.organization, requester_user_id=member.pk),
                             "backend_worker")
            self.assertEqual(p2_foundation.merge_policy_for(built.organization, requester_user_id=str(member.pk)),
                             "backend_worker")
            self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=publisher.pk))
            # L3: the owner (a founder with their own company row) is never stamped by this clause.
            self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=built.owner.pk))
            self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=None))
            payload = p2_foundation.stamp_run_request({}, organization=built.organization, requester_user_id=member.pk)
            self.assertEqual(payload["merge_policy"], "backend_worker")

    def test_seam_provider_missing_means_no_members(self):
        built = make_named_legacy_org("vyavos")
        user = _user()
        with missing_provider(p2_foundation.TEAM_MODULE):
            self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=user.pk))

    def test_a_broken_provider_never_blocks_dispatch(self):
        built = make_named_legacy_org("vyavos")
        user = _user()

        def boom(*args, **kwargs):
            raise RuntimeError("provider failed")

        with fake_provider(p2_foundation.TEAM_MODULE, viewer_role=boom, member_may_publish=boom):
            with self.assertLogs("content_factory.p2_foundation", "WARNING") as logs:
                self.assertEqual(p2_foundation.merge_policy_for(built.organization, requester_user_id=user.pk),
                                 "backend_worker")
        self.assertIn("p2_merge_policy_member_check_failed", logs.output[0])

    def test_broken_provider_preserves_owner_and_guards_member(self):
        built = make_named_legacy_org("vyavos")
        user = _user()

        def boom(*args, **kwargs):
            raise RuntimeError("rights unavailable")

        with fake_provider(p2_foundation.TEAM_MODULE, viewer_role=boom, member_may_publish=boom):
            self.assertIsNone(p2_foundation.merge_policy_for(built.organization, requester_user_id=built.owner.pk))
            self.assertEqual(p2_foundation.merge_policy_for(built.organization, requester_user_id=user.pk),
                             "backend_worker")
        with fake_provider(p2_foundation.TEAM_MODULE, viewer_role=lambda *a: "member", member_may_publish=boom):
            self.assertEqual(p2_foundation.merge_policy_for(built.organization, requester_user_id=user.pk),
                             "backend_worker")

    def test_later_requester_adds_guard_without_replacing_stamp(self):
        built = make_named_legacy_org("vyavos")
        member = _user()
        payload = p2_foundation.stamp_run_request({}, organization=built.organization)
        original_stamp = dict(payload["self_heal_policy_v1"])
        self.assertNotIn("merge_policy", payload)
        with fake_member_context(members={member.pk}, owner_ids={built.owner.pk}):
            p2_foundation.stamp_run_request(payload, organization=built.organization, requester_user_id=member.pk)
        self.assertEqual(payload["self_heal_policy_v1"], original_stamp)
        self.assertEqual(payload["merge_policy"], "backend_worker")

    def test_fake_member_provider_has_real_signatures_and_recipient_rules(self):
        built = make_named_legacy_org("vyavos")
        member, publisher = _user(), _user()
        article = make_unstamped_run(built.organization, workflow="article_generation")
        setup = make_unstamped_run(built.organization, workflow="article_system_setup")
        with fake_member_context(members={member.pk, publisher.pk}, publishers={publisher.pk},
                                 owner_ids={built.owner.pk}) as fake:
            self.assertTrue(fake.member_may_use_run(member, article))
            self.assertFalse(fake.member_may_use_run(member, setup))
            context = fake.member_context(member, built.company.pk, need="write_articles")
            self.assertIsNotNone(context)
            self.assertEqual(context.profile.user_id, member.pk)
            self.assertIsNone(fake.member_context(member, built.company.pk, need="publish"))
            self.assertIsNotNone(fake.member_context(publisher, built.company.pk, need="publish"))
            for ask, expected in ((None, {built.owner.pk, member.pk, publisher.pk}),
                                  ("approve_on_github", {built.owner.pk, publisher.pk}),
                                  ("approve_setup", {built.owner.pk})):
                self.assertEqual({u.pk for u in fake.notification_recipients(built.organization, item="article:1", ask=ask)},
                                 expected)
            self.assertEqual(fake.notification_recipients(built.organization, item="website"), [built.owner])
            self.assertRegex(fake.issue_join_reference(user=member, organization=built.organization), r"^[0-9a-f]{32}$")


class RunMergePolicyTests(TestCase):
    """IF-P2-26: ``run_merge_policy`` falls back to the parent's request, then the source run's."""

    def test_own_parent_then_source(self):
        org = make_p2_org().organization
        parent = make_stamped_run(org, merge_policy="backend_worker")
        source = make_stamped_run(org, merge_policy="backend_worker")
        plain = make_unstamped_run(org)
        own = make_stamped_run(org, merge_policy="backend_worker")
        self.assertEqual(p2_foundation.run_merge_policy(own), "backend_worker")
        child = make_unstamped_run(org, run_request={"parent_run_id": parent.run_id})
        self.assertEqual(p2_foundation.run_merge_policy(child), "backend_worker")
        publish = make_unstamped_run(org, run_request={"parent_run_id": plain.run_id, "source_run_id": source.run_id})
        self.assertEqual(p2_foundation.run_merge_policy(publish), "backend_worker")
        self.assertIsNone(p2_foundation.run_merge_policy(make_unstamped_run(org, run_request={"parent_run_id": plain.run_id})))
        self.assertIsNone(p2_foundation.run_merge_policy(plain))
        forged = make_unstamped_run(org, run_request={"merge_policy": "cf_may_merge"})
        self.assertIsNone(p2_foundation.run_merge_policy(forged))


class PrivateStrategyKeyTests(TestCase):
    """IF-P2-28: private bookkeeping never leaves the backend or requests a refresh."""

    KEYS = ('self_heal', 'startup_team', 'founder_notices', 'founder_funnel', 'first_article_commands', 'paid_article_commands', 'founder_run_commands_v1', 'editorial_catalog_commands_v1', 'founder_library_commands_v1', 'founder_review_commands_v1')

    def _strategy(self):
        return {"pillars": [{"slug": "p"}], "ops_alerts": {"a": {"state": "sent"}},
                **{key: {"kept": key} for key in self.KEYS}}

    def test_constants(self):
        from .founder_journey_cohort import PRIVATE_STRATEGY_KEYS
        self.assertEqual(p2_foundation.P2_PRIVATE_STRATEGY_KEYS, self.KEYS)
        self.assertTrue(set(self.KEYS) <= set(PRIVATE_STRATEGY_KEYS))
        self.assertNotIn("ops_alerts", PRIVATE_STRATEGY_KEYS)

    def test_bootstrap_settings_and_scan_payloads_omit_them_and_keep_ops_alerts(self):
        from integrations.api_views_content_factory_app import _serialize_config, scan_existing_artifacts
        built = make_named_legacy_org("talathrive")
        config = built.config
        config.pillar_strategy = self._strategy()
        config.save(update_fields=["pillar_strategy"])
        serialized = _serialize_config(config)["pillar_strategy"]
        scan = scan_existing_artifacts(config)["pillar_strategy"]
        for payload in (serialized, scan):
            for key in self.KEYS:
                self.assertNotIn(key, payload)
            self.assertEqual(payload["ops_alerts"], {"a": {"state": "sent"}})
            self.assertEqual(payload["pillars"], [{"slug": "p"}])

    def test_scan_merge_keeps_them_and_workers_never_see_them(self):
        from . import editorial_catalog
        merged = editorial_catalog.merge_strategy(self._strategy(), {"pillars": [{"slug": "new"}], "self_heal": {"forged": 1}})
        for key in self.KEYS:
            self.assertEqual(merged[key], {"kept": key})
        self.assertEqual(merged["pillars"], [{"slug": "new"}])
        self.assertNotIn("self_heal", editorial_catalog.merge_strategy({}, {"self_heal": {"forged": 1}}))
        public = editorial_catalog.public_strategy(merged)
        self.assertEqual(set(public), {"pillars"})

    def test_only_p2_keys_is_not_refresh_eligible(self):
        from .models import OrganizationContentConfig
        from .services import island_refresh_scheduler
        config = OrganizationContentConfig(pillar_strategy={key: {"x": 1} for key in self.KEYS})
        self.assertFalse(island_refresh_scheduler._has_pillar_strategy(config))
        config.pillar_strategy = {**config.pillar_strategy, "ops_alerts": {}, "founder_journey": {}}
        self.assertFalse(island_refresh_scheduler._has_pillar_strategy(config))
        config.pillar_strategy = {**config.pillar_strategy, "audience": "x"}
        self.assertTrue(island_refresh_scheduler._has_pillar_strategy(config))

    def test_result_and_request_keys(self):
        self.assertEqual(p2_foundation.P2_BACKEND_RESULT_KEYS, frozenset(RECEIPT_BUILDERS))
        self.assertEqual(p2_foundation.P2_BACKEND_REQUEST_KEYS, frozenset({"self_heal_policy_v1", "merge_policy"}))


class SeamHelperTests(SimpleTestCase):
    """IF-P2-24: ``_optional`` and the fake-provider helper."""

    def test_missing_then_fake_then_missing(self):
        module = "content_factory.p2_test_only_provider"
        self.assertIsNone(p2_foundation._optional(module, "run"))
        with fake_provider(module, run=lambda: "fake") as fake:
            self.assertIs(sys.modules[module], fake)
            self.assertEqual(p2_foundation._optional(module, "run")(), "fake")
            self.assertIsNone(p2_foundation._optional(module, "absent"))
        self.assertNotIn(module, sys.modules)
        self.assertIsNone(p2_foundation._optional(module, "run"))

    def test_only_import_and_attribute_errors_are_caught(self):
        module = "content_factory.p2_test_only_broken"

        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("not swallowed")

        sys.modules[module] = Exploding()
        self.addCleanup(sys.modules.pop, module, None)
        p2_foundation._optional.cache_clear()
        with self.assertRaises(RuntimeError):
            p2_foundation._optional(module, "run")


class P2ReceiptBuilderTests(SimpleTestCase):
    """The section 10 receipt builders every package uses."""

    def test_every_section_10_receipt_has_a_builder(self):
        self.assertEqual(set(RECEIPT_BUILDERS), set(p2_foundation.P2_BACKEND_RESULT_KEYS))
        for key, builder in RECEIPT_BUILDERS.items():
            with self.subTest(key=key):
                receipt = builder()
                self.assertIsInstance(receipt, (dict, list))
        self.assertEqual(RECEIPT_BUILDERS["self_heal_v1"]()["version"], 1)
        self.assertEqual(set(RECEIPT_BUILDERS["merge_v1"]()),
                         {"merged_at", "merge_sha", "head_sha", "method", "consent", "approved_by_user_id"})
        self.assertEqual(set(RECEIPT_BUILDERS["deploy_v1"]()), {"live_at", "url", "provider"})
        self.assertEqual(len(RECEIPT_BUILDERS["staff_actions_v1"](actions=[("retry_now", "done")] * 25)), 20)
        alert = ops_alert_receipt(acknowledged_at="2026-10-31T00:00:00+00:00", acknowledged_by_user_id=3)
        self.assertEqual((alert["acknowledged_at"], alert["acknowledged_by_user_id"]), ("2026-10-31T00:00:00+00:00", 3))

    def test_helpers_are_never_imported_by_production_code(self):
        importers = set()
        for app in ("content_factory", "founder_tools", "community_chat", "integrations", "core", "mlai"):
            for path in (ROOT / app).rglob("*.py"):
                relative = path.relative_to(ROOT).as_posix()
                if (path.name.startswith("tests") or "/tests/" in relative or "/migrations/" in relative
                        or relative == "content_factory/self_heal_testing.py"):
                    continue
                if "self_heal_testing" in path.read_text(encoding="utf-8", errors="ignore"):
                    importers.add(relative)
        self.assertEqual(importers, set())
