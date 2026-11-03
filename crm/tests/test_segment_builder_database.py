"""PostgreSQL regression coverage; run only under approved disposable migrations."""

from datetime import timedelta

from django.test import TestCase, override_settings

from crm.audience_profiles import correction_profile, empty_profile, with_profile
from crm.models import Blast, BlastDelivery, Contact, CrmAuditEvent, Segment
from crm.segment_trees import as_v2, definition_hash
from crm.segments import SegmentError, segment_contacts

from .api_fixtures import PREFIX, CrmApiMixin, crm_enabled
from .test_blasts_database import BlastFixtures, SENDING
from .test_segments_database import Population


def leaf(rule_type, **fields):
    return {"kind": "rule", "rule": {"type": rule_type, **fields}}


def group(match, *children):
    return {"kind": "group", "match": match, "children": list(children)}


def definition(include=None, exclude=None):
    return {"version": 2, "include": include or {"kind": "all_contacts"}, "exclude": exclude}


def field(name, value):
    return leaf("field", field=name, op="equals", value=value)


@crm_enabled
class SegmentBuilderDatabaseTests(CrmApiMixin, Population, TestCase):
    def setUp(self):
        super().setUp()
        self.build()

    def create(self, value, name="Audience", **headers):
        response = self.client.post(PREFIX + "segments/", {"name": name, "definition": value}, format="json", **headers)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def preview(self, value, **fields):
        response = self.client.post(PREFIX + "segments/preview/", {"definition": value, **fields}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def selected(self, value):
        return set(segment_contacts(value, now=self.now).values_list("email", flat=True))

    def snapshot(self, saved):
        return {"kind": "saved_segment_snapshot", "source_segment_id": saved["id"],
                "source_definition_hash": saved["definition_hash"], "source_name": saved["name"],
                "captured_at": self.now.isoformat(), "definition": as_v2(saved["definition"])}

    def profile(self, contact, facet, values, status="known"):
        profile = correction_profile(contact.attributes, {
            "facet": facet, "values": values, "status": status, "provenance": "self_reported",
            "self_report_confirmed": True, "source_record_id": "synthetic-survey",
            "observed_at": self.now.isoformat(),
        }, now=self.now)
        contact.attributes = with_profile(contact.attributes, profile)
        contact.save(update_fields=["attributes", "updated_at"])

    def test_nested_membership_cache_and_contact_list_agree(self):
        value = definition(group("all", group("any", field("city", "Sydney"), field("city", "Melbourne")),
                                 leaf("tag", op="has_any", values=["founder"])))
        self.assertEqual(self.selected(value), self.people(self.ada, self.bob))
        saved = self.create(value)
        self.assertEqual(saved["last_count"], 1)
        preview = self.preview(value)
        self.assertEqual((preview["matched_count"], preview["email_reachable_count"]), (2, 1))
        listed = self.client.get(PREFIX + f"contacts/?segment_id={saved['id']}").json()
        self.assertEqual({person["email"] for person in listed["results"]}, self.people(self.ada, self.bob))

    def test_preview_separates_counts_and_pages_the_selected_population(self):
        value = definition()
        first = self.preview(value, limit=2)
        self.assertEqual((first["count"], first["matched_count"], first["email_reachable_count"], first["marketing_eligible_count"]),
                         (4, 7, 4, 2))
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual({person["email"] for person in first["matched_sample"]}, self.people(self.ada, self.bob))
        next_page = self.preview(value, offset=first["next_offset"], limit=2, definition_hash=first["definition_hash"])
        self.assertFalse({person["id"] for person in first["matched_sample"]} & {person["id"] for person in next_page["matched_sample"]})
        eligible = self.preview(value, sample_scope="marketing_eligible")
        self.assertEqual({person["email"] for person in eligible["matched_sample"]}, self.people(self.ada, self.dan))
        self.assertIsNone(eligible["next_offset"])

    def test_snapshot_union_preserves_local_exclusions_and_survives_source_changes(self):
        source_value = definition(group("any", field("city", "Sydney"), field("city", "Melbourne")),
                                  leaf("tag", op="has_any", values=["founder"]))
        source = self.create(source_value, "City nonfounders")
        composed = definition(group("any", self.snapshot(source), leaf("tag", op="has_any", values=["founder"])))
        saved = self.create(composed, "Combined")
        self.assertEqual(self.selected(saved["definition"]), self.people(self.ada, self.bob, self.cat, self.gus))
        Segment.objects.filter(pk=source["id"]).update(definition=definition(field("city", "Nowhere")))
        Segment.objects.filter(pk=source["id"]).delete()
        response = self.client.patch(PREFIX + f"segments/{saved['id']}/",
                                     {"name": "Still combined", "definition": saved["definition"]},
                                     format="json", HTTP_IF_MATCH=saved["etag"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.selected(response.json()["definition"]), self.people(self.ada, self.bob, self.cat, self.gus))

    def test_stale_edits_and_legacy_downgrades_cannot_overwrite(self):
        saved = self.create(definition())
        url = PREFIX + f"segments/{saved['id']}/"
        edited = self.client.patch(url, {"name": "New name"}, format="json", HTTP_IF_MATCH=saved["etag"])
        self.assertEqual(edited.status_code, 200, edited.content)
        stale = self.client.patch(url, {"name": "Stale name"}, format="json", HTTP_IF_MATCH=saved["etag"])
        self.assertEqual((stale.status_code, stale.json()["error"]), (409, "segment_changed"))
        blind = self.client.patch(url, {"name": "Blind name"}, format="json")
        self.assertEqual(blind.status_code, 428)
        downgrade = self.client.patch(url, {"definition": {"rules": []}}, format="json", HTTP_IF_MATCH=edited.json()["etag"])
        self.assertEqual(downgrade.json()["error"], "definition_version_required")
        self.assertEqual(Segment.objects.get(pk=saved["id"]).name, "New name")

    def test_a_segment_cannot_embed_itself_or_its_ancestry(self):
        original = self.create(definition(field("city", "Sydney")), "Original")
        copied = self.create(definition(self.snapshot(original)), "Copy")
        for node in (self.snapshot(original), self.snapshot(copied)):
            response = self.client.patch(PREFIX + f"segments/{original['id']}/",
                                         {"definition": definition(node)}, format="json", HTTP_IF_MATCH=original["etag"])
            self.assertEqual((response.status_code, response.json()["error"]), (400, "segment_self_reference"))

    def test_create_retry_is_durable_and_bound_to_content(self):
        first = self.create(definition(), HTTP_IDEMPOTENCY_KEY="create-attempt-1")
        second = self.create(definition(), HTTP_IDEMPOTENCY_KEY="create-attempt-1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Segment.objects.count(), 1)
        self.assertEqual(CrmAuditEvent.objects.filter(target_type="segment").count(), 1)
        conflict = self.client.post(PREFIX + "segments/", {"name": "Different", "definition": definition()},
                                    format="json", HTTP_IDEMPOTENCY_KEY="create-attempt-1")
        self.assertEqual((conflict.status_code, conflict.json()["error"]), (409, "idempotency_key_reused"))

    def test_create_retry_does_not_resurrect_a_deleted_segment(self):
        saved = self.create(definition(), HTTP_IDEMPOTENCY_KEY="deleted-attempt")
        deleted = self.client.delete(PREFIX + f"segments/{saved['id']}/", HTTP_IF_MATCH=saved["etag"])
        self.assertEqual(deleted.status_code, 204)
        retried = self.client.post(PREFIX + "segments/", {"name": "Audience", "definition": definition()},
                                  format="json", HTTP_IDEMPOTENCY_KEY="deleted-attempt")
        self.assertEqual((retried.status_code, retried.json()["error"]), (409, "segment_deleted"))
        self.assertFalse(Segment.objects.exists())

    def test_authoring_disabled_still_replays_an_existing_create(self):
        saved = self.create(definition(), HTTP_IDEMPOTENCY_KEY="existing-attempt")
        with override_settings(CRM_SEGMENT_BUILDER_V2_ENABLED=False):
            replayed = self.create(definition(), HTTP_IDEMPOTENCY_KEY="existing-attempt")
        self.assertEqual(replayed["id"], saved["id"])
        self.assertEqual(Segment.objects.count(), 1)

    def test_gender_values_do_not_conflate_missing_withheld_or_negation(self):
        self.profile(self.ada, "gender", ["woman"])
        self.profile(self.bob, "gender", ["man"])
        self.profile(self.cat, "gender", [], status="withheld")
        women = definition(leaf("audience_facet", facet="gender", op="has_any", values=["woman"]))
        not_women = definition(leaf("audience_facet", facet="gender", op="has_none", values=["woman"]))
        self.assertEqual(self.selected(women), self.people(self.ada))
        self.assertEqual(self.selected(not_women), self.people(self.bob))
        missing = definition(leaf("audience_facet", facet="gender", op="is_not_provided"))
        self.assertEqual(self.selected(missing), self.people(self.dan, self.eve, self.fay, self.gus))

    def test_negative_activity_with_missing_coverage_fails_closed(self):
        value = definition(exclude=leaf("activity", source="slack_message", op="within", within_days=90))
        with self.assertRaises(SegmentError) as caught:
            self.selected(value)
        self.assertEqual(caught.exception.code, "rule_not_available")

    def test_complete_coverage_and_null_observations_match_inactive_people(self):
        for contact in Contact.objects.all():
            profile = empty_profile()
            profile["activity"] = {"slack_message": {
                "status": "available", "identity_status": "verified",
                "covered_from": (self.now - timedelta(days=100)).isoformat(),
                "covered_through": (self.now - timedelta(minutes=5)).isoformat(),
                "last_observed_at": self.now.isoformat() if contact.pk == self.ada.pk else None,
            }}
            contact.attributes = with_profile(contact.attributes, profile)
            contact.save(update_fields=["attributes"])
        inactive = definition(leaf("activity", source="slack_message", op="not_within", within_days=90))
        self.assertEqual(self.selected(inactive), self.everyone_but(self.ada))

    def test_activity_exclusion_refuses_one_missing_identity_among_covered_contacts(self):
        for contact in Contact.objects.all():
            if contact.pk == self.bob.pk:
                continue
            profile = empty_profile()
            profile["activity"] = {"slack_message": {
                "status": "available", "identity_status": "verified",
                "covered_from": (self.now - timedelta(days=100)).isoformat(),
                "covered_through": (self.now - timedelta(minutes=5)).isoformat(), "last_observed_at": None,
            }}
            contact.attributes = with_profile(contact.attributes, profile)
            contact.save(update_fields=["attributes"])
        value = definition(exclude=leaf("activity", source="slack_message", op="within", within_days=90))
        with self.assertRaises(SegmentError) as caught:
            self.selected(value)
        self.assertEqual(caught.exception.reason, "activity_coverage_incomplete")

    def test_stale_activity_watermark_cannot_establish_inactivity(self):
        for contact in Contact.objects.all():
            profile = empty_profile()
            profile["activity"] = {"slack_message": {
                "status": "available", "identity_status": "verified",
                "covered_from": (self.now - timedelta(days=100)).isoformat(),
                "covered_through": (self.now - timedelta(days=3)).isoformat(), "last_observed_at": None,
            }}
            contact.attributes = with_profile(contact.attributes, profile)
            contact.save(update_fields=["attributes"])
        with self.assertRaises(SegmentError):
            self.selected(definition(leaf("activity", source="slack_message", op="not_within", within_days=90)))

    def test_typed_profile_correction_preserves_consent_and_is_visible_in_detail(self):
        original = (self.ada.marketing_consent, self.ada.email_status)
        self.ada.attributes = {"marketing_review_hold": True, "ordinary": "value"}
        self.ada.save(update_fields=["attributes"])
        response = self.client.post(PREFIX + f"contacts/{self.ada.pk}/audience-profile/", {
            "facet": "gender", "status": "known", "values": ["woman"], "provenance": "verified",
            "self_report_confirmed": True, "source_record_id": "synthetic-survey",
            "observed_at": self.now.isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.ada.refresh_from_db()
        self.assertEqual((self.ada.marketing_consent, self.ada.email_status), original)
        self.assertTrue(self.ada.attributes["marketing_review_hold"])
        detail = self.client.get(PREFIX + f"contacts/{self.ada.pk}/").json()
        self.assertEqual(detail["attributes"], {"ordinary": "value"})
        self.assertEqual(detail["audience_profile"]["facets"]["gender"]["values"], ["woman"])

    def test_authoring_flag_preserves_read_and_evaluation(self):
        saved = self.create(definition())
        with override_settings(CRM_SEGMENT_BUILDER_V2_ENABLED=False):
            self.assertEqual(self.client.get(PREFIX + f"segments/{saved['id']}/").status_code, 200)
            self.assertEqual(self.preview(saved["definition"])["matched_count"], 7)
            blocked = self.client.patch(PREFIX + f"segments/{saved['id']}/", {"name": "Blocked"},
                                       format="json", HTTP_IF_MATCH=saved["etag"])
            self.assertEqual(blocked.json()["error"], "segment_builder_disabled")


@override_settings(**SENDING)
class SegmentBuilderCampaignDatabaseTests(BlastFixtures, TestCase):
    def audience(self, blast):
        response = self.client.post(PREFIX + f"blasts/{blast.pk}/audience/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def confirmed_send(self, blast, audience):
        return self.client.post(PREFIX + f"blasts/{blast.pk}/send/", {
            "confirm": {"email": audience["channels"]["email"]["reachable"]},
            "idempotency_key": "composed-send", "definition_hash": audience["definition_hash"],
            "recipient_fingerprint": audience["recipient_fingerprint"],
        }, format="json")

    def test_same_count_membership_change_requires_fresh_review_and_snapshots_exact_rules(self):
        self.ada.tags = ["selected"]
        self.ada.save(update_fields=["tags"])
        value = definition(leaf("tag", op="has_any", values=["selected"]))
        segment = Segment.objects.create(name="Selected", definition=value, created_by=self.admin)
        blast = self.create_draft(quick_segment="", segment_id=str(segment.pk), channels=["email"])
        before = self.audience(blast)
        self.ada.tags = []
        self.ada.save(update_fields=["tags"])
        self.bob.tags = ["selected"]
        self.bob.save(update_fields=["tags"])
        refused = self.confirmed_send(blast, before)
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, "audience_changed"))
        self.assertFalse(BlastDelivery.objects.filter(blast=blast).exists())
        after = self.audience(blast)
        self.assertEqual(before["channels"]["email"]["reachable"], after["channels"]["email"]["reachable"])
        self.assertNotEqual(before["recipient_fingerprint"], after["recipient_fingerprint"])
        sent = self.confirmed_send(blast, after)
        self.assertEqual(sent.status_code, 202, sent.content)
        blast.refresh_from_db()
        self.assertEqual(blast.status, Blast.Status.QUEUED)
        self.assertEqual(definition_hash(blast.segment_definition), after["definition_hash"])
        self.assertEqual(set(BlastDelivery.objects.filter(blast=blast).values_list("contact_id", flat=True)), {self.bob.pk})

    def test_same_count_rule_change_requires_fresh_review(self):
        segment = Segment.objects.create(name="Person", definition=definition(field("first_name", "Ada")), created_by=self.admin)
        blast = self.create_draft(quick_segment="", segment_id=str(segment.pk), channels=["email"])
        before = self.audience(blast)
        segment.definition = definition(field("first_name", "Bob"))
        segment.save(update_fields=["definition", "updated_at"])
        refused = self.confirmed_send(blast, before)
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, "audience_changed"))
        self.assertFalse(BlastDelivery.objects.filter(blast=blast).exists())
