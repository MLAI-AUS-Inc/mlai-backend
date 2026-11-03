"""Audience composition, provenance and concurrency without database access."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.db.models import Q
from django.test import SimpleTestCase

from community_chat.crm.errors import CrmError
from community_chat.crm.segment_services import _precondition, _replay, verify_snapshot_sources
from community_chat.crm.serializers import SegmentPreviewInput
from crm.blasts import recipient_fingerprint
from crm.segment_trees import as_v2, definition_etag, definition_hash, snapshot_nodes
from crm.segments import SegmentError, definition_q, validate_definition

NOW = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
SOURCE_ID = "6f1c7d0e-9b1a-4d4c-8a43-6c1f0a0b1c2d"


def rule(field, value):
    return {"kind": "rule", "rule": {"type": "field", "field": field, "op": "equals", "value": value}}


def group(match, *children):
    return {"kind": "group", "match": match, "children": list(children)}


def definition(include=None, exclude=None):
    return {"version": 2, "include": include or {"kind": "all_contacts"}, "exclude": exclude}


def snapshot(value):
    return {
        "kind": "saved_segment_snapshot", "source_segment_id": SOURCE_ID,
        "source_definition_hash": definition_hash(value), "source_name": "City members",
        "captured_at": NOW.isoformat().replace("+00:00", "Z"), "definition": as_v2(value),
    }


def matches(q, person):
    """Evaluate the actual scalar ORM expression against a tiny independent population."""

    outcomes = []
    for child in q.children:
        if isinstance(child, Q):
            outcomes.append(matches(child, person))
            continue
        key, expected = child
        field, _, lookup = key.partition("__")
        actual = person.get(field)
        if lookup == "iexact":
            outcomes.append(str(actual or "").lower() == expected.lower())
        elif lookup == "isnull":
            outcomes.append((actual is None) == expected)
        else:
            outcomes.append(actual == expected)
    value = (all(outcomes) if q.connector == "AND" else any(outcomes))
    return not value if q.negated else value


class CompositionTests(SimpleTestCase):
    people = [
        {"pk": 1, "city": "Adelaide", "job_title": "Founder", "company": "A"},
        {"pk": 2, "city": "Melbourne", "job_title": "Developer", "company": "Excluded"},
        {"pk": 3, "city": "Sydney", "job_title": "Developer", "company": "Excluded"},
        {"pk": 4, "city": "Sydney", "job_title": "Founder", "company": "B"},
    ]

    def selected(self, value):
        query = definition_q(validate_definition(value), now=NOW)
        return [person["pk"] for person in self.people if matches(query, person)]

    def test_nested_city_or_and_career_and_global_exclusion(self):
        audience = definition(group("all", group("any", rule("city", "Adelaide"), rule("city", "Melbourne")),
                                    group("any", rule("job_title", "Founder"), rule("job_title", "Developer"))),
                              rule("company", "Excluded"))
        self.assertEqual(self.selected(audience), [1])

    def test_source_exclusion_does_not_leak_into_sibling_union(self):
        source = definition(group("any", rule("city", "Adelaide"), rule("city", "Melbourne")),
                            rule("company", "Excluded"))
        audience = definition(group("any", snapshot(source), rule("job_title", "Developer")))
        self.assertEqual(self.selected(audience), [1, 2, 3])
        audience["exclude"] = rule("city", "Sydney")
        self.assertEqual(self.selected(audience), [1, 2])

    def test_all_contacts_is_true_inside_or_and_false_when_excluded(self):
        audience = definition(group("any", {"kind": "all_contacts"}, rule("city", "Nowhere")))
        self.assertEqual(self.selected(audience), [1, 2, 3, 4])
        audience["exclude"] = {"kind": "all_contacts"}
        self.assertEqual(self.selected(audience), [])

    def test_legacy_empty_and_exclusion_semantics_survive_conversion(self):
        for mode in ("all", "any"):
            for rules in ([], [rule("city", "Adelaide")["rule"], rule("city", "Melbourne")["rule"]]):
                legacy = {"match": mode, "rules": rules, "exclude": [rule("company", "Excluded")["rule"]]}
                self.assertEqual(self.selected(legacy), self.selected(as_v2(legacy)))
                self.assertEqual(definition_hash(legacy), definition_hash(as_v2(legacy)))

    def test_snapshot_membership_is_dynamic_and_has_no_database_dependency(self):
        source = definition(rule("city", "Adelaide"))
        audience = definition(snapshot(source))
        self.assertEqual(self.selected(audience), [1])
        query = definition_q(validate_definition(audience), now=NOW)
        self.assertTrue(matches(query, {"pk": 5, "city": "Adelaide"}))
        self.assertEqual(len(list(snapshot_nodes(audience))), 1)


class TreeValidationTests(SimpleTestCase):
    def invalid(self, value, reason):
        with self.assertRaises(SegmentError) as caught:
            validate_definition(value)
        self.assertEqual(caught.exception.reason, reason)
        self.assertTrue(caught.exception.pointer.startswith("/"))

    def test_empty_unknown_and_invalid_version_nodes_fail_closed(self):
        self.invalid(definition(group("all")), "empty_group")
        self.invalid(definition({"kind": "live_segment", "id": SOURCE_ID}), "unknown_node_kind")
        self.invalid({"version": True, "include": {"kind": "all_contacts"}}, "unsupported_version")
        self.invalid(definition({"kind": "all_contacts", "sql": "1=1"}), "unknown_key")

    def test_depth_and_expanded_leaf_limits(self):
        node = rule("city", "Adelaide")
        for _ in range(7):
            node = group("all", node)
        self.invalid(definition(node), "too_deep")
        source = definition(group("any", *[rule("city", "Adelaide") for _ in range(26)]))
        self.invalid(definition(group("all", snapshot(source), snapshot(source))), "too_many_rules")

    def test_snapshot_provenance_is_strict_and_timezoned(self):
        node = snapshot(definition(rule("city", "Adelaide")))
        node["captured_at"] = "2026-11-03T06:00:00"
        self.invalid(definition(node), "invalid_timestamp")
        node["captured_at"] = NOW.isoformat()
        node["source_definition_hash"] = "not-a-hash"
        self.invalid(definition(node), "invalid_hash")

    def test_snapshot_timestamp_overflow_is_a_validation_error(self):
        node = snapshot(definition(rule("city", "Adelaide")))
        node["captured_at"] = "0001-01-01T00:00:00+23:00"
        self.invalid(definition(node), "invalid_timestamp")

    def test_internal_profile_cannot_be_searched_as_raw_text(self):
        self.invalid({"rules": [{"type": "attribute", "key": "audience_profile_v1", "op": "contains", "value": "woman"}]},
                     "use_typed_audience_rule")

    def test_unrated_rule_has_no_numeric_default(self):
        value = validate_definition({"rules": [{"type": "engagement_stars", "op": "is_not_set"}]})
        self.assertEqual(value["rules"][0], {"type": "engagement_stars", "op": "is_not_set"})
        self.assertIsInstance(definition_q(value, now=NOW), Q)
        self.invalid({"rules": [{"type": "engagement_stars", "op": "is_not_set", "value": 1}]}, "not_allowed")


class SourceAndRevisionTests(SimpleTestCase):
    def segment(self, value=None):
        return SimpleNamespace(pk=uuid.UUID(SOURCE_ID), name="City members", description="",
                               definition=value or definition(rule("city", "Adelaide")), updated_at=NOW,
                               last_count=0, last_counted_at=NOW)

    def test_new_copy_verifies_its_complete_source(self):
        source = self.segment()
        proposed = definition(snapshot(source.definition))
        with patch("community_chat.crm.segment_services.Segment.objects.filter", return_value=[source]):
            verify_snapshot_sources(proposed)
            proposed["include"]["definition"]["include"] = rule("city", "Sydney")
            with self.assertRaises(CrmError) as caught:
                verify_snapshot_sources(proposed)
            self.assertEqual(caught.exception.code, "segment_source_changed")

    def test_retained_copy_survives_deleted_source(self):
        previous = definition(snapshot(self.segment().definition))
        with patch("community_chat.crm.segment_services.Segment.objects.filter") as lookup:
            verify_snapshot_sources(deepcopy(previous), previous)
        lookup.assert_not_called()

    def test_full_precision_revision_and_stale_write_rejection(self):
        segment = self.segment()
        token = definition_etag(segment)
        _precondition(segment, {}, token)
        segment.updated_at = NOW.replace(microsecond=1)
        self.assertNotEqual(token, definition_etag(segment))
        with self.assertRaises(CrmError) as caught:
            _precondition(segment, {}, token)
        self.assertEqual(caught.exception.code, "segment_changed")

    def test_old_client_cannot_downgrade_or_blindly_edit_v2(self):
        with self.assertRaises(CrmError) as caught:
            _precondition(self.segment(), {"definition": {"rules": []}}, None)
        self.assertEqual(caught.exception.code, "definition_version_required")
        with self.assertRaises(CrmError) as caught:
            _precondition(self.segment(), {"name": "Changed"}, None)
        self.assertEqual(caught.exception.code, "segment_precondition_required")

    def test_idempotency_receipt_rejects_different_creation_payload(self):
        queryset = Mock()
        queryset.order_by.return_value.first.return_value = SimpleNamespace(detail={"request_hash": "original"})
        segment = self.segment()
        with patch("community_chat.crm.segment_services.CrmAuditEvent.objects.filter", return_value=queryset):
            self.assertIs(_replay(segment, object(), "original"), segment)
            with self.assertRaises(CrmError) as caught:
                _replay(segment, object(), "different")
            self.assertEqual(caught.exception.code, "idempotency_key_reused")

    def test_preview_page_scope_is_bounded_and_explicit(self):
        serializer = SegmentPreviewInput(data={"definition": definition(), "sample_scope": "marketing_eligible", "limit": 50})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["sample_scope"], "marketing_eligible")
        serializer = SegmentPreviewInput(data={"definition": definition(), "limit": 51})
        self.assertFalse(serializer.is_valid())

    def test_campaign_fingerprint_detects_equal_count_membership_or_channel_change(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        expected = recipient_fingerprint({"email": [first]})
        self.assertEqual(expected, recipient_fingerprint({"email": iter([first])}))
        self.assertNotEqual(expected, recipient_fingerprint({"email": [second]}))
        self.assertNotEqual(expected, recipient_fingerprint({"sms": [first]}))
