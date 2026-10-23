"""Segment definitions: the schema validator and expression builder, without a database."""

import uuid
from datetime import datetime, timezone

from django.db.models import Exists, Q
from django.test import SimpleTestCase

from crm.models import BlastDelivery, CrmEvent
from crm.segments import (
    ANSWER_SCAN_LIMIT,
    MAX_RULES,
    RULE_TYPES,
    SegmentError,
    catalog,
    definition_q,
    evaluate,
    quick_segment_definition,
    validate_definition,
)

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)
EVENT_ID = "6f1c7d0e-9b1a-4d4c-8a43-6c1f0a0b1c2d"

EVERY_RULE = [
    {"type": "event_registration", "event_ids": [EVENT_ID], "statuses": ["going"], "checked_in": True},
    {"type": "events_attended_count", "op": "gte", "value": 2},
    {"type": "last_attended_within_days", "value": 180},
    {"type": "tag", "op": "has_any", "values": ["founder"]},
    {"type": "field", "field": "city", "op": "equals", "value": "Sydney"},
    {"type": "attribute", "key": "favourite_model", "op": "contains", "value": "claude"},
    {"type": "registration_answer", "label": "What do you do?", "op": "contains", "value": "research"},
    {"type": "chat_member", "value": True},
    {"type": "source", "values": ["import"]},
    {"type": "reachable", "channel": "sms"},
]


def flatten(q):
    found = []
    for child in q.children:
        if isinstance(child, Q):
            found.extend(flatten(child))
        else:
            found.append(child)
    return found


def leaves(node):
    found = []
    for child in getattr(node, "children", ()):
        found.extend(leaves(child) if hasattr(child, "children") else [child])
    return found


class ValidatorTests(SimpleTestCase):
    def invalid(self, definition):
        with self.assertRaises(SegmentError) as caught:
            validate_definition(definition)
        self.assertEqual(caught.exception.code, "invalid_definition")
        return caught.exception.pointer, caught.exception.reason

    def test_every_v1_rule_type_is_accepted_and_defaults_are_filled(self):
        definition = validate_definition({"rules": EVERY_RULE})
        self.assertEqual(definition["match"], "all")
        self.assertEqual(definition["exclude"], [])
        self.assertEqual([rule["type"] for rule in definition["rules"]], [rule["type"] for rule in EVERY_RULE])
        self.assertEqual(set(RULE_TYPES) - {rule["type"] for rule in EVERY_RULE}, {"blast_engagement"})

    def test_values_are_normalised(self):
        definition = validate_definition(
            {
                "match": "any",
                "rules": [
                    {"type": "event_registration", "event_ids": [EVENT_ID.upper(), EVENT_ID]},
                    {"type": "tag", "op": "has_all", "values": [" founder ", "founder", "big   speaker"]},
                    {"type": "field", "field": "company", "op": "is_set", "value": ""},
                ],
                "exclude": [{"type": "blast_engagement", "blast_id": EVENT_ID, "action": "clicked"}],
            }
        )
        self.assertEqual(
            definition["rules"][0],
            {"type": "event_registration", "event_ids": [EVENT_ID], "statuses": [], "checked_in": None},
        )
        self.assertEqual(definition["rules"][1]["values"], ["founder", "big speaker"])
        self.assertEqual(definition["rules"][2], {"type": "field", "field": "company", "op": "is_set"})
        self.assertEqual(definition["exclude"][0]["action"], "clicked")

    def test_errors_carry_a_json_pointer(self):
        cases = [
            ([], ("/", "expected_object")),
            ({"match": "most"}, ("/match", "expected_one_of")),
            ({"sql": "select"}, ("/sql", "unknown_key")),
            ({"rules": {}}, ("/rules", "expected_array")),
            ({"rules": ["tag"]}, ("/rules/0", "expected_object")),
            ({"rules": [{"type": "raw_sql"}]}, ("/rules/0/type", "unknown_rule_type")),
            ({"rules": [{"type": "tag", "op": "has_any", "values": ["a"], "where": "1=1"}]}, ("/rules/0/where", "unknown_key")),
            ({"rules": [{"type": "events_attended_count", "op": "gt", "value": 2}]}, ("/rules/0/op", "expected_one_of")),
            ({"rules": [{"type": "events_attended_count", "op": "gte", "value": True}]}, ("/rules/0/value", "expected_integer")),
            ({"rules": [{"type": "events_attended_count", "op": "gte", "value": "2"}]}, ("/rules/0/value", "expected_integer")),
            ({"rules": [{"type": "last_attended_within_days", "value": 0}]}, ("/rules/0/value", "out_of_range")),
            ({"rules": [{"type": "tag", "op": "has_any", "values": []}]}, ("/rules/0/values", "required")),
            ({"rules": [{"type": "tag", "op": "has_any", "values": ["x" * 65]}]}, ("/rules/0/values/0", "too_long")),
            ({"rules": [{"type": "field", "field": "password", "op": "is_set"}]}, ("/rules/0/field", "expected_one_of")),
            ({"rules": [{"type": "field", "field": "city", "op": "equals"}]}, ("/rules/0/value", "expected_string")),
            ({"rules": [{"type": "field", "field": "city", "op": "contains", "value": "   "}]}, ("/rules/0/value", "required")),
            ({"rules": [{"type": "attribute", "key": "Bad Key", "op": "is_set"}]}, ("/rules/0/key", "invalid_attribute_key")),
            ({"rules": [{"type": "registration_answer", "label": "", "op": "is_set"}]}, ("/rules/0/label", "required")),
            ({"rules": [{"type": "chat_member", "value": "yes"}]}, ("/rules/0/value", "expected_boolean")),
            ({"rules": [{"type": "source", "values": ["luma", "spreadsheet"]}]}, ("/rules/0/values/1", "expected_one_of")),
            ({"rules": [{"type": "reachable", "channel": "fax"}]}, ("/rules/0/channel", "expected_one_of")),
            ({"rules": [{"type": "event_registration", "event_ids": ["evt-1"]}]}, ("/rules/0/event_ids/0", "expected_uuid")),
            ({"rules": [{"type": "event_registration", "statuses": ["declined"]}]}, ("/rules/0/statuses/0", "expected_one_of")),
            ({"rules": [{"type": "event_registration", "checked_in": "true"}]}, ("/rules/0/checked_in", "expected_boolean")),
            ({"exclude": [{"type": "tag", "op": "has_any", "values": [1]}]}, ("/exclude/0/values/0", "expected_string")),
            ({"rules": [{"type": "blast_engagement", "blast_id": "nope", "action": "clicked"}]}, ("/rules/0/blast_id", "expected_uuid")),
        ]
        for definition, expected in cases:
            with self.subTest(definition=definition):
                self.assertEqual(self.invalid(definition), expected)

    def test_pointer_escapes_slashes_and_tildes_in_keys(self):
        self.assertEqual(self.invalid({"a/b~c": 1}), ("/a~1b~0c", "unknown_key"))

    def test_rule_count_is_bounded_across_rules_and_exclusions(self):
        rule = {"type": "chat_member", "value": True}
        validate_definition({"rules": [rule] * MAX_RULES})
        self.assertEqual(
            self.invalid({"rules": [rule] * MAX_RULES, "exclude": [rule]}), ("/exclude", "too_many_rules")
        )

    def test_quick_segments_are_valid_definitions(self):
        event = CrmEvent(pk=uuid.UUID(EVENT_ID))
        for key in ("going", "checked_in", "no_show", "waitlist"):
            with self.subTest(key=key):
                definition = quick_segment_definition(key, event)
                self.assertEqual(validate_definition(definition), definition)
        self.assertEqual(
            quick_segment_definition("no_show", event)["rules"][0],
            {"type": "event_registration", "event_ids": [EVENT_ID], "statuses": ["going"], "checked_in": False},
        )


class ExpressionTests(SimpleTestCase):
    def build(self, *rules, match="all", exclude=()):
        return definition_q(
            validate_definition({"match": match, "rules": list(rules), "exclude": list(exclude)}), now=NOW
        )

    def test_every_available_rule_builds_an_orm_expression(self):
        for rule in EVERY_RULE:
            with self.subTest(rule=rule["type"]):
                self.assertIsInstance(self.build(rule), Q)

    def test_an_empty_definition_matches_everyone(self):
        self.assertEqual(self.build(), Q())

    def test_match_and_exclude_combine(self):
        any_q = self.build(
            {"type": "source", "values": ["luma"]},
            {"type": "source", "values": ["import"]},
            match="any",
            exclude=[{"type": "chat_member", "value": True}],
        )
        self.assertEqual(any_q.connector, "AND")
        rules, exclusion = any_q.children
        self.assertEqual(rules.connector, "OR")
        self.assertTrue(exclusion.negated)

    def test_tag_ops(self):
        has_all = self.build({"type": "tag", "op": "has_all", "values": ["a", "b"]})
        self.assertEqual(flatten(has_all), [("tags__contains", ["a", "b"])])
        has_none = self.build({"type": "tag", "op": "has_none", "values": ["a", "b"]})
        self.assertTrue(has_none.negated or has_none.children[0].negated)
        self.assertEqual(
            sorted(flatten(has_none)), [("tags__contains", ["a"]), ("tags__contains", ["b"])]
        )

    def test_field_ops_are_case_insensitive_text_lookups(self):
        self.assertEqual(
            flatten(self.build({"type": "field", "field": "city", "op": "equals", "value": "sydney"})),
            [("city__iexact", "sydney")],
        )
        self.assertEqual(
            flatten(self.build({"type": "field", "field": "company", "op": "contains", "value": "bank"})),
            [("company__icontains", "bank")],
        )
        is_set = self.build({"type": "field", "field": "job_title", "op": "is_set"})
        self.assertEqual(flatten(is_set), [("job_title", "")])

    def test_counts_and_recency(self):
        self.assertEqual(
            flatten(self.build({"type": "events_attended_count", "op": "eq", "value": 3})),
            [("events_attended_count__exact", 3)],
        )
        self.assertEqual(
            flatten(self.build({"type": "last_attended_within_days", "value": 30})),
            [("last_attended_at__gte", datetime(2026, 9, 23, tzinfo=timezone.utc))],
        )

    def test_registration_rules_use_exists_subqueries(self):
        for rule in (EVERY_RULE[0], EVERY_RULE[6], EVERY_RULE[7]):
            with self.subTest(rule=rule["type"]):
                self.assertTrue(any(isinstance(child, Exists) for child in flatten(self.build(rule))))

    def test_answer_rules_match_the_label_at_each_scanned_position(self):
        q = self.build(EVERY_RULE[6])
        [exists] = [child for child in flatten(q) if isinstance(child, Exists)]
        label_checks = [
            leaf for leaf in leaves(exists.query.where) if getattr(leaf, "rhs", None) == "What do you do?"
        ]
        self.assertEqual(len(label_checks), ANSWER_SCAN_LIMIT)

    def engagement_fields(self, action):
        rule = {"type": "blast_engagement", "blast_id": EVENT_ID, "action": action}
        [exists] = [child for child in flatten(self.build(rule)) if isinstance(child, Exists)]
        self.assertIs(exists.query.model, BlastDelivery)
        return {
            leaf.lhs.target.name
            for leaf in leaves(exists.query.where)
            if getattr(getattr(leaf, "lhs", None), "target", None) is not None
        }

    def test_blast_engagement_reads_the_blast_deliveries(self):
        clicked = self.engagement_fields("clicked")
        self.assertIn("first_clicked_at", clicked)
        self.assertNotIn("first_opened_at", clicked)
        # A click means the email was opened, even with its tracking pixel blocked.
        self.assertTrue({"first_opened_at", "first_clicked_at"} <= self.engagement_fields("opened"))

    def test_purpose_rules_are_checked_before_any_query(self):
        with self.assertRaises(SegmentError) as caught:
            evaluate({"rules": []}, "newsletter")
        self.assertEqual(caught.exception.code, "invalid_purpose")
        with self.assertRaises(SegmentError) as caught:
            evaluate({"rules": []}, "event_update", None)
        self.assertEqual(caught.exception.code, "event_required")
        with self.assertRaises(SegmentError) as caught:
            evaluate({"rules": "all"}, "marketing")
        self.assertEqual(caught.exception.code, "invalid_definition")


class CatalogTests(SimpleTestCase):
    def test_catalog_lists_rule_types_and_supplied_counts(self):
        body = catalog(
            tags=[{"tag": "founder", "count": 3}],
            attributes=[{"key": "favourite_model", "count": 2}],
            questions=[{"label": "What do you do?", "count": 5}],
        )
        availability = {item["type"]: item["available"] for item in body["rule_types"]}
        self.assertTrue(availability.pop("blast_engagement"))
        self.assertTrue(all(availability.values()))
        self.assertIn({"field": "city", "label": "City"}, body["fields"])
        self.assertEqual(body["tags"], [{"tag": "founder", "count": 3}])
        self.assertEqual(body["registration_questions"][0]["label"], "What do you do?")
        self.assertEqual({item["value"] for item in body["channels"]}, {"email", "sms", "whatsapp"})
        self.assertEqual(body["registration_statuses"], ["going", "waitlist", "pending"])
