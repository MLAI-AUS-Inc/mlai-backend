"""Audience ORM shapes, protection and correction authorization without database access."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db.models import Q
from django.test import SimpleTestCase

from community_chat.crm.errors import CrmError
from community_chat.crm.profile_services import correct_audience_profile
from community_chat.crm.serializers import ContactFieldsInput, contact_detail
from crm.audience_profile_rules import (
    PREFIX, ensure_activity_coverage, profile_catalog, profile_rule_q, validate_profile_rule,
)
from crm.audience_profiles import AUDIENCE_PROFILE_KEY, correction_profile, with_profile
from crm.contact_import import ImportFileError, parse_rows, plan_columns, validate_column_plan
from crm.engagement import editable_attributes
from crm.models import Contact
from crm.segments import SegmentError, definition_q, validate_definition

NOW = datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)


def correction():
    return {"facet": "career_stage", "status": "known", "values": ["early_career"],
            "provenance": "verified", "source_record_id": "source-1", "observed_at": "2026-10-20T00:00:00Z"}


def flatten(q):
    found = []
    for child in q.children:
        found.extend(flatten(child) if isinstance(child, Q) else [child])
    return found


class AudienceRuleTests(SimpleTestCase):
    def test_new_leaves_roundtrip_in_legacy_and_v2_envelopes(self):
        rules = [
            {"type": "location_affinity", "op": "has_any", "city_ids": ["au-vic-melbourne", "au-sa-adelaide"]},
            {"type": "audience_facet", "facet": "career_stage", "op": "has_any", "values": ["early_career"]},
            {"type": "audience_facet", "facet": "gender", "op": "has_status", "status": "withheld"},
            {"type": "activity", "source": "slack_message", "op": "within", "within_days": 90},
        ]
        for rule in rules:
            with self.subTest(rule=rule):
                legacy = validate_definition({"rules": [rule]})
                tree = validate_definition({"version": 2, "include": {"kind": "rule", "rule": rule}, "exclude": None})
                self.assertEqual(legacy["rules"][0], tree["include"]["rule"])
                self.assertIsInstance(definition_q(tree, now=NOW), Q)

    def test_negative_facet_requires_known_usable_data(self):
        rule = {"type": "audience_facet", "facet": "gender", "op": "has_none", "values": ["woman"]}
        query = profile_rule_q(rule, NOW)
        self.assertIn((PREFIX + "__facets__gender__status", "known"), flatten(query))
        self.assertIn((PREFIX + "__facets__gender__value_count__gte", 1), flatten(query))
        self.assertTrue(any(isinstance(child, Q) and child.negated for child in query.children))

    def test_provided_not_provided_withheld_are_distinct_queries(self):
        base = {"type": "audience_facet", "facet": "gender"}
        provided = profile_rule_q({**base, "op": "is_provided"}, NOW)
        absent = profile_rule_q({**base, "op": "is_not_provided"}, NOW)
        withheld = profile_rule_q({**base, "op": "has_status", "status": "withheld"}, NOW)
        self.assertNotEqual(provided, absent)
        self.assertNotEqual(absent, withheld)
        self.assertNotIn((PREFIX + "__facets__gender__status", "withheld"), flatten(absent))

    def test_invalid_leaf_values_always_have_typed_errors(self):
        cases = [
            {"type": "audience_facet", "facet": [], "op": "has_any", "values": ["woman"]},
            {"type": "activity", "source": [], "op": "within", "within_days": 90},
            {"type": "activity", "source": "slack_message", "op": "within", "within_days": True},
            {"type": "audience_facet", "facet": "gender", "op": "has_any", "values": ["guessed"]},
            {"type": "audience_facet", "facet": "career_stage", "op": "has_all", "values": ["early_career", "senior"]},
            {"type": "location_affinity", "op": "is_provided", "city_ids": []},
            {"type": "activity", "source": "slack_message", "op": "within", "within_days": 90, "sql": "bad"},
        ]
        for rule in cases:
            with self.subTest(rule=rule), self.assertRaises(SegmentError):
                validate_profile_rule(rule, ("rules", 0))

    def test_activity_bounds_have_fixed_timestamp_precision(self):
        query = profile_rule_q({"type": "activity", "source": "app_login", "op": "within", "within_days": 30}, NOW)
        upper = dict(flatten(query))[PREFIX + "__activity__app_login__last_observed_at__lte"]
        self.assertEqual(upper, "2026-11-03T06:30:00.000000Z")

    def test_excluded_activity_fails_closed_on_incomplete_coverage(self):
        rule = {"type": "activity", "source": "slack_message", "op": "within", "within_days": 90}
        query = Contact.objects.all()
        tree = {"version": 2, "include": {"kind": "all_contacts"}, "exclude": {"kind": "rule", "rule": rule}}
        with (patch("django.db.models.query.QuerySet.aggregate", return_value={"observation_as_of": NOW}),
              patch("django.db.models.query.QuerySet.exists", return_value=True), self.assertRaises(SegmentError) as caught):
            ensure_activity_coverage(tree, NOW, query)
        self.assertEqual(caught.exception.reason, "activity_coverage_incomplete")
        with (patch("django.db.models.query.QuerySet.aggregate", return_value={"observation_as_of": NOW}),
              patch("django.db.models.query.QuerySet.exists", return_value=False)):
            self.assertEqual(ensure_activity_coverage(tree, NOW, query), {"slack_message": NOW})

    def test_covered_inactivity_explicitly_includes_json_null_or_absent_last_observation(self):
        query = profile_rule_q({"type": "activity", "source": "slack_message", "op": "not_within", "within_days": 90}, NOW)
        values = flatten(query)
        key = PREFIX + "__activity__slack_message__last_observed_at"
        self.assertIn((key + "__isnull", True), values)
        self.assertIn((key, None), values)
        self.assertIn((PREFIX + "__activity__slack_message__status", "available"), values)

    def test_positive_activity_and_ordinary_profiles_do_not_query_for_negative_coverage(self):
        query = MagicMock()
        ensure_activity_coverage({"rules": [{"type": "activity", "source": "slack_message", "op": "within", "within_days": 90}]}, NOW, query)
        ensure_activity_coverage({"rules": [{"type": "audience_facet", "facet": "gender", "op": "is_provided"}]}, NOW, query)
        query.exclude.assert_not_called()

    def test_catalog_does_not_claim_uninspected_data_exists(self):
        catalog = profile_catalog()
        self.assertTrue(all(facet["status"] == "unmapped" and facet["known_count"] is None for facet in catalog["facets"]))
        query = MagicMock()
        query.aggregate.return_value = {"total": 12, "career_stage_known": 3, "career_stage_option_1": 3}
        facets = {facet["id"]: facet for facet in profile_catalog(query, now=NOW)["facets"]}
        self.assertEqual(facets["career_stage"]["known_count"], 3)
        self.assertEqual(facets["career_stage"]["status"], "available")
        query.aggregate.assert_called_once()


class AudienceProtectionTests(SimpleTestCase):
    def test_generic_csv_cannot_write_protected_namespace(self):
        self.assertEqual(plan_columns(["email", AUDIENCE_PROFILE_KEY])[AUDIENCE_PROFILE_KEY], "ignore")
        forged = {"email": "email", "profile": "attributes." + AUDIENCE_PROFILE_KEY}
        for operation in (lambda: validate_column_plan(forged), lambda: parse_rows([], forged)):
            with self.assertRaises(ImportFileError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "reserved_attribute")

    def test_generic_contact_edit_and_display_exclude_namespace(self):
        value = with_profile({"ordinary": "value"}, correction_profile({}, correction(), now=NOW))
        self.assertEqual(editable_attributes(value), {"ordinary": "value"})
        serializer = ContactFieldsInput(data={"attributes": {AUDIENCE_PROFILE_KEY: "attempted overwrite"}})
        self.assertFalse(serializer.is_valid())

    def test_detail_separates_profile_from_scalar_attributes(self):
        contact = Contact(email="synthetic@example.test", attributes=with_profile({}, correction_profile({}, correction(), now=NOW)))
        body = contact_detail(contact, now=NOW, registrations=[], deliveries=[], replies=[], history=[])
        self.assertEqual(body["attributes"], {})
        self.assertEqual(body["audience_profile"]["facets"]["career_stage"]["values"], ["early_career"])

    def test_correction_requires_admin_before_database_access(self):
        with patch("community_chat.crm.profile_services.require_mlai_admin", side_effect=PermissionError), patch("community_chat.crm.profile_services.Contact.objects") as contacts:
            with self.assertRaises(PermissionError):
                correct_audience_profile("id", correction(), actor=object(), now=NOW)
        contacts.select_for_update.assert_not_called()

    def test_correction_updates_only_attributes_and_audits_no_values(self):
        contact = SimpleNamespace(pk="contact-1", attributes={"marketing_review_hold": True}, save=MagicMock())
        with (patch("community_chat.crm.profile_services.require_mlai_admin"),
              patch("community_chat.crm.profile_services.transaction.atomic"),
              patch("community_chat.crm.profile_services.Contact.objects") as contacts,
              patch("community_chat.crm.profile_services.get_object_or_404", return_value=contact),
              patch("community_chat.crm.profile_services.CrmAuditEvent.objects") as audit):
            contacts.select_for_update.return_value.get.return_value = contact
            correct_audience_profile(contact.pk, correction(), actor=object(), now=NOW)
        self.assertTrue(contact.attributes["marketing_review_hold"])
        contact.save.assert_called_once_with(update_fields=["attributes", "updated_at"])
        self.assertEqual(audit.create.call_args.kwargs["detail"], {"operation": "audience_profile_correction", "fields": ["career_stage"]})
