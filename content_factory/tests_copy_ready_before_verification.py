"""P1.8 (IF-P1-10): any article started before the website verifies is copy-ready.

For the new-journey cohort, with ``ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED``
on, an explicit content_only start for a connected website stays content_only
while the website cannot generate (setup running, failed, stopped,
unsupported, unbound or unverified), and a verified website upgrades it to a
review draft only once the owner chose "Publish automatically". The legacy
cohort keeps the 17 Oct rule: 409 ``website_preview_required`` or the
review_draft/hosted upgrade, with every P1 flag on.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.response import Response

from founder_tools.services import FounderCompanyContext

from . import vibe_marketing_views as views
from .founder_journey_cohort import opt_in, read_marker, update_marker
from .founder_journey_testing import ALL_P1_FLAGS_ON, NAMED_LEGACY_ORGS, _connect, make_named_legacy_org, make_new_journey_org
from .portable_drafts import copy_ready_reported_mode, keeps_copy_ready_start
from .website_contract import CONNECTION_FIELDS

START = views.start_article_generation
WITHOUT_V2 = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
WITHOUT_COPY_READY = {**ALL_P1_FLAGS_ON, "ARTICLES_COPY_READY_BEFORE_VERIFICATION_ENABLED": False}
FLAG_SETS = (("all P1 flags on", ALL_P1_FLAGS_ON), ("all P1 flags on minus V2", WITHOUT_V2))
READY = {"canGenerateArticle": True, "reasonCode": "", "reason": "", "nextStep": "articles"}
# The website states that cannot generate an article, as the capabilities report them.
UNREADY_STATES = {
    "setup running": "setup_pr_unmerged",
    "setup failed": "integration_required",
    "setup stopped": "integration_required",
    "unsupported site": "publishing_adapter_required",
    "unbound repository": "repository_required",
    "unverified": "verification_required",
}
OWNER_APPROVAL = {"enabledAt": "2026-10-30T00:00:00+00:00", "enabledBy": 1, "origin": "owner_setup_approval"}


def unready(reason_code):
    return {"canGenerateArticle": False, "reasonCode": reason_code, "reason": "Finish website setup.",
            "nextStep": "articles"}


@contextmanager
def no_guard(scope, *, action):
    yield


def context_for(built):
    company = built.company
    return FounderCompanyContext(profile=company.profile, company=company, organization=built.organization)


def portable_request(built, topic="Copy-ready article"):
    data = {"topic": topic, "deliveryMode": "content_only", "deliveryModeConfirmed": True,
            "deliveryModeExplicit": True}
    connection = built.connection
    if connection is not None:
        # What a client sends for a connected website: its reviewed identity.
        data.update(website_connection_id=str(connection.pk), connection_generation=connection.generation)
    return data


def confirmed_only_request(built, topic="Confirmed-only article"):
    """The same choice with only ``deliveryModeConfirmed``, as some MCP callers send it."""
    data = portable_request(built, topic)
    data.pop("deliveryModeExplicit")
    return data


class _StartHarness:
    """Runs today's start with billing and Content Factory replaced by recorders."""

    def outcome(self, built, data, capabilities):
        """``("refused", code)``, ``("review_draft", mode)`` for the preview upgrade, or ``("started", mode)``."""
        inner = Mock(return_value=Response({"runId": "preview-run"}, status=202))
        queued = Mock(return_value=SimpleNamespace(run_id="copy-run", status="queued"))
        caps = Mock(return_value=capabilities)
        with patch.object(views, "start_article_generation", inner), \
             patch.object(views, "_article_capabilities_for_context", caps), \
             patch.object(views, "_charge_roo_points_for_article", return_value=(built.owner, None, {}, None)), \
             patch.object(views, "_queue_content_factory_run", queued), \
             patch.object(views, "_mark_keyword_in_progress"), \
             patch.object(views, "authority_guard", side_effect=no_guard):
            response = START(user=built.owner, context=context_for(built), data=data)
        self.capability_reads = caps.call_count
        if response.status_code == 409:
            return "refused", response.data.get("code")
        self.assertEqual(response.status_code, 202, response.data)
        if inner.called:
            return "review_draft", inner.call_args.kwargs["data"]["delivery_mode"]
        self.queued_payload = queued.call_args.kwargs["payload"]
        return "started", self.queued_payload.get("delivery_mode")


@override_settings(**ALL_P1_FLAGS_ON)
class NewJourneyCopyReadyTests(_StartHarness, TestCase):
    def setUp(self):
        self.built = make_new_journey_org()
        self.built.connection, self.built.target = _connect(self.built.organization, self.built.config,
                                                            repo="example-new/website")

    def test_every_unready_website_starts_copy_ready_for_article_one_and_two(self):
        for state, reason_code in UNREADY_STATES.items():
            for article in (1, 2):
                with self.subTest(state=state, article=article):
                    result = self.outcome(self.built, portable_request(self.built, f"Article {article}"),
                                          unready(reason_code))
                    self.assertEqual(result, ("started", "content_only"))

    def test_a_paused_website_and_a_saved_repo_alone_also_start_copy_ready(self):
        self.built.connection.state = "paused"
        self.built.connection.save(update_fields=["state"])
        self.assertEqual(self.outcome(self.built, portable_request(self.built), unready("website_publishing_paused")),
                         ("started", "content_only"))
        saved_repo_only = make_new_journey_org()
        saved_repo_only.config.github_repo = "example-saved/website"
        saved_repo_only.config.save(update_fields=["github_repo"])
        for article in (1, 2):
            with self.subTest(article=article):
                self.assertEqual(self.outcome(saved_repo_only, portable_request(saved_repo_only, f"A{article}"),
                                              unready("verification_required")), ("started", "content_only"))

    def test_the_content_factory_request_carries_no_repository_or_connection(self):
        for shape, build in (("explicit", portable_request), ("confirmed only", confirmed_only_request)):
            for operable in (False, True):
                with self.subTest(shape=shape, github_operable=operable):
                    request = self.content_factory_request(build(self.built), github_operable=operable)
                    self.assertEqual((request["delivery_mode"], request["github_repo"]), ("content_only", ""))
                    self.assertIs(request["delivery_mode_explicit"], True)
                    for field in (*CONNECTION_FIELDS, "branch", "app_root"):
                        self.assertNotIn(field, request)

    def test_a_confirmed_only_request_starts_copy_ready_even_with_github_operable(self):
        for operable in (False, True):
            with self.subTest(github_operable=operable), \
                 patch.object(views, "_github_repo_operable", return_value=operable):
                self.assertEqual(self.outcome(self.built, confirmed_only_request(self.built),
                                              unready("verification_required")), ("started", "content_only"))
                self.assertIs(self.queued_payload["delivery_mode_explicit"], True)
                self.assertIs(self.queued_payload["delivery_mode_confirmed"], True)
                # Without "Publish automatically" a verified website stays copy-ready too.
                self.assertEqual(self.outcome(self.built, confirmed_only_request(self.built), READY),
                                 ("started", "content_only"))

    def test_a_start_that_names_no_delivery_mode_keeps_todays_repository_path(self):
        # The IF-P1-10 gate covers the content_only upgrade only; a start that
        # names no mode is today's repository start (P2.13 enforces the opt-in further).
        with patch.object(views, "_github_repo_operable", return_value=True):
            self.assertEqual(self.outcome(self.built, {"topic": "No mode"}, READY), ("started", None))
        self.assertEqual(self.queued_payload["github_repo"], "example-new/website")
        self.assertIs(self.queued_payload["delivery_mode_explicit"], False)

    def content_factory_request(self, data, *, github_operable=False):
        sent = []
        from requests import RequestException

        def accept(url, json=None, **kwargs):
            sent.append(json)
            return SimpleNamespace(status_code=202, content=b"fixture",
                                   json=lambda: {"run_id": json["client_request_id"], "status": "queued"})

        with patch.object(views, "_article_capabilities_for_context", return_value=unready("setup_pr_unmerged")), \
             patch.object(views, "_charge_roo_points_for_article", return_value=(self.built.owner, None, {}, None)), \
             patch.object(views, "_refresh_article_editorial_payload", return_value=None), \
             patch.object(views, "founder_actor_id_for_user", return_value="mlai_user:new"), \
             patch.object(views, "_content_factory_remote_config", return_value={
                 "enabled": True, "base_url": "https://worker.invalid", "api_key_configured": True, "is_local_env": True}), \
             patch.object(views, "_content_factory_headers", return_value={}), \
             patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("pending", {})), \
             patch.object(views, "_github_repo_operable", return_value=github_operable), \
             patch.object(views, "http_client") as http:
            http.RequestException = RequestException
            http.post.side_effect = accept
            response = START(user=self.built.owner, context=context_for(self.built), data=data)
        self.assertEqual(response.status_code, 202, response.data)
        return sent[-1]

    def test_a_verified_website_stays_copy_ready_without_publish_automatically(self):
        result = self.outcome(self.built, portable_request(self.built), READY)
        self.assertEqual(result, ("started", "content_only"))
        # Without the owner's choice there is nothing to upgrade, so GitHub is not even probed.
        self.assertEqual(self.capability_reads, 0)

    def test_publish_automatically_upgrades_a_verified_website_to_a_review_draft(self):
        for origin in ("owner_setup_approval", "opt_in_existing_publisher"):
            with self.subTest(origin=origin):
                update_marker(self.built.organization, publish_automatically={**OWNER_APPROVAL, "origin": origin})
                self.assertEqual(self.outcome(self.built, portable_request(self.built), READY),
                                 ("review_draft", "review_draft"))
                # A website that cannot generate still stays copy-ready.
                self.assertEqual(self.outcome(self.built, portable_request(self.built), unready("verification_required")),
                                 ("started", "content_only"))

    def test_a_request_that_is_not_an_explicit_portable_choice_keeps_todays_rule(self):
        resolved = {**portable_request(self.built), "resolvedDeliveryMode": "review_draft"}
        self.assertFalse(keeps_copy_ready_start(self.built.organization, self.built.config, resolved,
                                                can_generate_article=lambda: False))
        self.assertEqual(self.outcome(self.built, resolved, unready("verification_required")),
                         ("refused", "website_preview_required"))
        implicit = {"topic": "Implicit", "deliveryMode": "content_only"}
        self.assertEqual(self.outcome(self.built, implicit, unready("verification_required")),
                         ("refused", "article_system_setup_blocked"))

    def test_reported_delivery_mode_is_content_only_until_verified_with_publish_automatically(self):
        config = self.built.config
        config.refresh_from_db()
        self.assertEqual(views._reported_article_delivery_mode(config), "content_only")
        self.built.target.verified_at = None
        self.built.target.save(update_fields=["verified_at"])
        update_marker(self.built.organization, publish_automatically=OWNER_APPROVAL)
        config.refresh_from_db()
        self.assertEqual(views._reported_article_delivery_mode(config), "content_only")
        # Stored settings are never rewritten.
        self.assertEqual(config.article_delivery_mode, "review_draft")
        from django.utils import timezone
        self.built.target.verified_at = timezone.now()
        self.built.target.save(update_fields=["verified_at"])
        config.refresh_from_db()
        self.assertIsNone(copy_ready_reported_mode(config))
        self.assertEqual(views._reported_article_delivery_mode(config), "review_draft")

    def test_switches_off_restore_the_17_october_rule_for_this_org(self):
        for name, flags in (("V2 off", WITHOUT_V2), ("copy-ready off", WITHOUT_COPY_READY)):
            with self.subTest(name), override_settings(**flags):
                self.assertEqual(self.outcome(self.built, portable_request(self.built), unready("verification_required")),
                                 ("refused", "website_preview_required"))
                self.assertEqual(self.outcome(self.built, portable_request(self.built), READY),
                                 ("review_draft", "review_draft"))
                self.assertEqual(views._reported_article_delivery_mode(self.built.config), "review_draft")


@override_settings(ARTICLES_HOSTED_SITES_ENABLED=True)  # PawShare's production shape
class LegacyDeliveryModeTests(_StartHarness, TestCase):
    """G1: the five named organisations keep today's delivery mode with every P1 flag on."""

    def results(self, built):
        built.config.refresh_from_db()
        return {
            "unready": self.outcome(built, portable_request(built), unready("verification_required")),
            "ready": self.outcome(built, portable_request(built), READY),
            "reported": views._reported_article_delivery_mode(built.config),
        }

    def test_named_legacy_orgs_keep_todays_results(self):
        expected = {
            # Connected GitHub publishers: refused before billing, or upgraded to a preview.
            "connected": {"unready": ("refused", "website_preview_required"), "ready": ("review_draft", "review_draft"),
                          "reported": "review_draft"},
            # PawShare's hosted site publishes repo-free drafts, unchanged.
            "hosted": {"unready": ("started", "content_only"), "ready": ("started", "content_only"),
                       "reported": "review_draft"},
        }
        for name in NAMED_LEGACY_ORGS:
            for marker in (True, False):
                built = make_named_legacy_org(name, marker=marker, domain=f"{name.replace('_', '-')}-{marker}.example.test")
                today = self.results(built)
                self.assertEqual(today, expected["hosted" if name == "pawshare" else "connected"], name)
                for label, flags in FLAG_SETS:
                    with self.subTest(name=name, marker=marker, flags=label), override_settings(**flags):
                        self.assertEqual(self.results(built), today)

    def test_a_confirmed_only_request_keeps_todays_preview_rule(self):
        for name in NAMED_LEGACY_ORGS:
            if name == "pawshare":
                continue
            built = make_named_legacy_org(name, domain=f"{name.replace('_', '-')}-confirmed.example.test")
            for label, flags in FLAG_SETS:
                with self.subTest(name=name, flags=label), override_settings(**flags):
                    self.assertEqual(self.outcome(built, confirmed_only_request(built), unready("verification_required")),
                                     ("refused", "website_preview_required"))
                    self.assertEqual(self.outcome(built, confirmed_only_request(built), READY),
                                     ("review_draft", "review_draft"))

    @override_settings(**ALL_P1_FLAGS_ON)
    def test_talathrive_after_opt_in_still_gets_review_drafts(self):
        built = make_named_legacy_org("talathrive")
        opt_in(built.organization, built.owner)
        marker = read_marker(built.organization)
        self.assertEqual(marker["cohort"], "new_journey")
        self.assertEqual(marker["publishAutomatically"]["origin"], "opt_in_existing_publisher")
        self.assertEqual(self.outcome(built, portable_request(built), READY), ("review_draft", "review_draft"))
        built.config.refresh_from_db()
        self.assertEqual(views._reported_article_delivery_mode(built.config), "review_draft")
