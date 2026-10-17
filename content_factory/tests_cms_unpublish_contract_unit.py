"""Explicit withdrawal retains owned CMS drafts and never repeats an uncertain write."""

from copy import deepcopy
from uuid import uuid4

from django.test import SimpleTestCase

from . import cms_post_contract as posts
from . import cms_unpublish_contract as withdraw
from .cms_api_transport import CmsApiError
from .tests_cms_post_contract_unit import intent, response, issued


class CmsUnpublishContractTests(SimpleTestCase):
    def fixture(self, platform="wordpress"):
        publication = intent(platform)
        acknowledged = response(publication)
        if platform == "ghost": acknowledged = {"posts": [acknowledged]}
        confirmed = posts.observe_posts(publication, issued(publication)[0], acknowledged, lookup=False)
        chosen = withdraw.make_intent(operation_id=str(uuid4()), request_id=str(uuid4()), publication=publication, state=confirmed)
        context = {"publication": publication, "published_state": confirmed}
        return chosen, withdraw.new_state(chosen, **context), context

    def provider(self, publication, *, draft=False, **changes):
        row = response(publication, **changes)
        if draft: row["status"] = "draft"
        if publication["binding"]["platform"] == "ghost":
            row.setdefault("updated_at", "2026-10-18T00:00:00.000Z")
            return {"posts": [row]}
        return row

    def ready(self, chosen, state, context):
        return withdraw.observe(chosen, state, self.provider(context["publication"]), **context)

    def test_fixed_provider_id_is_the_only_lookup_and_reserved_state_is_not_permission(self):
        for platform in ("wordpress", "ghost"):
            chosen, state, context = self.fixture(platform)
            effect = withdraw.read_effect(chosen, state, **context)
            self.assertEqual((effect.method, effect.resource, effect.identifier, effect.payload), ("GET", "posts", chosen["provider_id"], None))
            self.assertNotIn("slug", effect.query)
            with self.assertRaises(CmsApiError): withdraw.issue(chosen, state, **context)

    def test_an_uncertain_original_create_cannot_become_a_withdrawal_target(self):
        publication = intent()
        unknown, _effect = issued(publication)
        with self.assertRaises(CmsApiError):
            withdraw.make_intent(operation_id=str(uuid4()), request_id=str(uuid4()), publication=publication, state=unknown)

    def test_wordpress_issues_one_status_only_update_and_get_recovers_its_lost_reply(self):
        chosen, state, context = self.fixture()
        state = self.ready(chosen, state, context)
        issued_state, effect = withdraw.issue(chosen, state, **context)
        self.assertEqual((effect.method, effect.identifier, effect.payload), ("POST", 42, {"status": "draft"}))
        self.assertEqual(effect.query, {"context": "edit"})
        self.assertEqual(state["issue_count"], 0)
        self.assertEqual((issued_state["phase"], issued_state["issue_count"], issued_state["outcome_unknown"]), ("issued", 1, True))
        with self.assertRaises(CmsApiError): withdraw.issue(chosen, issued_state, **context)
        recovered = withdraw.observe(chosen, issued_state, self.provider(context["publication"], draft=True), **context)
        self.assertEqual((recovered["phase"], recovered["issue_count"], recovered["outcome_unknown"]), ("provider_draft", 1, False))
        self.assertNotIn("public_absence_verified", recovered)
        self.assertNotIn("The exact approved body", repr(effect))

    def test_ghost_put_contains_only_status_and_fresh_collision_token(self):
        chosen, state, context = self.fixture("ghost")
        state = self.ready(chosen, state, context)
        after, effect = withdraw.issue(chosen, state, **context)
        self.assertEqual(effect.method, "PUT")
        self.assertEqual(effect.query, {"formats": "html"})
        self.assertEqual(effect.payload, {"posts": [{"status": "draft", "updated_at": "2026-10-18T00:00:00.000Z"}]})
        self.assertEqual(effect.identifier, chosen["provider_id"])
        self.assertNotIn("authors", repr(effect.payload)); self.assertNotIn("tags", repr(effect.payload))
        self.assertEqual(withdraw.read_effect(chosen, after, **context).method, "GET")

    def test_already_owned_draft_requires_no_write_on_either_platform(self):
        for platform in ("wordpress", "ghost"):
            chosen, state, context = self.fixture(platform)
            observed = withdraw.observe(chosen, state, self.provider(context["publication"], draft=True), **context)
            self.assertEqual((observed["phase"], observed["issue_count"]), ("provider_draft", 0))
            with self.assertRaises(CmsApiError): withdraw.issue(chosen, observed, **context)

    def test_still_published_after_issue_is_unknown_and_stops_after_eight_reads(self):
        chosen, state, context = self.fixture()
        state, _effect = withdraw.issue(chosen, self.ready(chosen, state, context), **context)
        for index in range(8):
            state = withdraw.observe(chosen, state, self.provider(context["publication"]), **context)
            self.assertEqual(state["probe_count"], index + 1)
            self.assertEqual(state["issue_count"], 1)
            self.assertTrue(state["outcome_unknown"])
            with self.assertRaises(CmsApiError): withdraw.issue(chosen, state, **context)
        self.assertEqual(state["phase"], "reconcile_exhausted")
        with self.assertRaises(CmsApiError): withdraw.observe(chosen, state, self.provider(context["publication"]), **context)

    def test_owner_republication_after_observed_draft_is_not_unpublished_again(self):
        chosen, state, context = self.fixture()
        state = withdraw.observe(chosen, state, self.provider(context["publication"], draft=True), **context)
        with self.assertRaises(CmsApiError) as failure:
            withdraw.observe(chosen, state, self.provider(context["publication"]), **context)
        self.assertEqual(failure.exception.category, "post_ownership_conflict")
        with self.assertRaises(CmsApiError): withdraw.issue(chosen, state, **context)

    def test_foreign_id_changed_title_body_or_native_fields_cannot_prepare_a_write(self):
        chosen, state, context = self.fixture()
        for changes in ({"id": 43}, {"title": {"raw": "Owner edited"}}, {"content": {"raw": "Owner edited"}},
                {"author": 8}, {"categories": [99]}, {"featured_media": 8}, {"status": "private"}):
            with self.subTest(changes=changes), self.assertRaises(CmsApiError):
                withdraw.observe(chosen, state, self.provider(context["publication"], **changes), **context)
        self.assertEqual(state["phase"], "reserved")

    def test_rechecksummed_metadata_cannot_change_original_scope_or_enable_boolean_selectors(self):
        chosen, _state, context = self.fixture()
        for changes in ({"schema_version": True}, {"provider_id": 99}, {"public_url": "https://other.test/"},
                {"body_sha256": "f" * 64}, {"mode": "delete"}, {"binding": {**chosen["binding"], "schema_version": True}},
                {"publication_operation_id": str(uuid4())}, {"secret": "do-not-export"}):
            value = {**chosen, **changes}
            value["digest"] = posts._digest({key: item for key, item in value.items() if key != "digest"})
            with self.subTest(changes=changes), self.assertRaises(CmsApiError): withdraw.validate_intent(value, **context)

    def test_missing_or_malformed_ghost_collision_revision_does_not_admit_a_put(self):
        chosen, state, context = self.fixture("ghost")
        for revision in (None, True, "2026-10-18", "2026-10-18T00:00:00+01:00", "not-a-date"):
            with self.subTest(revision=revision), self.assertRaises(CmsApiError):
                withdraw.observe(chosen, state, self.provider(context["publication"], updated_at=revision), **context)

    def test_invented_or_reset_journals_are_rejected_and_inputs_are_not_shared(self):
        chosen, state, context = self.fixture()
        original = deepcopy((chosen, state, context))
        for changes in ({"issue_count": True}, {"probe_count": True}, {"phase": "issued"},
                {"observation_digest": "a" * 64}, {"provider_revision": "2026-10-18T00:00:00Z"},
                {"public_absence_verified": True}, {"outcome_unknown": True}):
            with self.subTest(changes=changes), self.assertRaises(CmsApiError): withdraw.validate_state({**state, **changes}, chosen, **context)
        self.ready(chosen, state, context)
        self.assertEqual((chosen, state, context), original)
