"""Audience source contracts without Django setup, a database, or migrations."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from crm.audience_profiles import (
    AUDIENCE_PROFILE_KEY, AudienceProfileError, activity_match, audience_profile,
    correction_profile, empty_profile, parse_instant, validate_activity, validate_profile, with_profile,
)
from crm.audience_sources import (
    activity_projection, mapped_observation, merge_activity, merge_observation, project_records,
    retract_mapping_sources, validate_mapping,
)
from crm.audience_taxonomy import normalize_value

NOW = datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)


def correction(facet="career_stage", values=None, **changes):
    return {"facet": facet, "status": "known", "values": ["early_career"] if values is None else values,
            "provenance": "verified", "source_record_id": "source-record-1",
            "observed_at": "2026-10-20T00:00:00Z", **changes}


def mapping(facet="career_stage", **changes):
    return {"id": "reviewed-form-field", "source_kind": "luma_answer", "source_key": "question-stage",
            "facet": facet, "provenance": "self_reported", "self_report_confirmed": True,
            "reviewed": True, "aliases": {}, **changes}


def observation(value="Early career", *, record="answer-1", at="2026-10-20T00:00:00Z", source=None):
    return mapped_observation(source or mapping(), value, source_record_id=record, observed_at=at, now=NOW)


class ProfileSchemaTests(TestCase):
    def test_timestamp_underflow_is_a_typed_validation_error(self):
        for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            with self.subTest(value=value), self.assertRaises(AudienceProfileError):
                parse_instant(value)

    def test_correction_has_separate_typed_dto_and_never_changes_other_attributes(self):
        attributes = {"marketing_review_hold": True, "engagement_rating": {"stars": 4}, "ordinary": "value"}
        saved = with_profile(attributes, correction_profile(attributes, correction(), now=NOW))
        self.assertEqual({key: saved[key] for key in attributes}, attributes)
        self.assertNotIn(AUDIENCE_PROFILE_KEY, attributes)
        dto = audience_profile(saved)
        self.assertEqual(dto["facets"]["career_stage"]["values"], ["early_career"])
        dto["facets"].clear()
        self.assertIn("career_stage", saved[AUDIENCE_PROFILE_KEY]["facets"])

    def test_absent_invalid_and_arbitrary_demographic_attributes_are_not_trusted(self):
        for attributes in ({}, None, {"gender": "woman"}, {AUDIENCE_PROFILE_KEY: "woman"},
                           {AUDIENCE_PROFILE_KEY: {"schema_version": 999}}):
            with self.subTest(attributes=attributes):
                self.assertIsNone(audience_profile(attributes))

    def test_sensitive_values_require_attested_self_report(self):
        for facet, values in (("gender", ["woman"]), ("age_band", ["25_34"])):
            with self.subTest(facet=facet), self.assertRaises(AudienceProfileError):
                correction_profile({}, correction(facet, values), now=NOW)
            profile = correction_profile({}, correction(facet, values, self_report_confirmed=True), now=NOW)
            self.assertEqual(profile["facets"][facet]["status"], "known")

    def test_derived_stage_is_refused(self):
        with self.assertRaises(AudienceProfileError):
            correction_profile({}, correction(provenance="derived"), now=NOW)

    def test_unknown_withheld_conflicted_and_unmapped_are_not_known_values(self):
        for status in ("not_provided", "withheld", "conflicted", "unmapped"):
            profile = correction_profile({}, correction("gender", [], status=status, self_report_confirmed=True), now=NOW)
            self.assertEqual(profile["facets"]["gender"]["values"], [])
            self.assertEqual(profile["facets"]["gender"]["status"], status)
        with self.assertRaises(AudienceProfileError):
            correction_profile({}, correction(status="not_provided"), now=NOW)

    def test_multi_role_but_single_career_stage(self):
        profile = correction_profile({}, correction("career", ["founder", "developer", "founder"]), now=NOW)
        self.assertEqual(profile["facets"]["career"]["values"], ["developer", "founder"])
        self.assertEqual(profile["facets"]["career"]["value_count"], 2)
        with self.assertRaises(AudienceProfileError):
            correction_profile({}, correction(values=["early_career", "senior"]), now=NOW)

    def test_malformed_fields_fail_before_any_persistence(self):
        for updates in ({"facet": []}, {"values": ["invented"]}, {"values": "early_career"},
                        {"source_record_id": ""}, {"observed_at": "2026-10-20"},
                        {"observed_at": "2030-01-01T00:00:00Z"}, {"extra": "field"}):
            with self.subTest(updates=updates), self.assertRaises(AudienceProfileError):
                correction_profile({}, correction(**updates), now=NOW)

    def test_correction_does_not_silently_discard_invalid_existing_projection(self):
        with self.assertRaises(AudienceProfileError):
            correction_profile({AUDIENCE_PROFILE_KEY: {"schema_version": 2}}, correction(), now=NOW)

    def test_times_are_normalized_for_lexical_json_queries(self):
        profile = correction_profile({}, correction(observed_at="2026-10-20T11:00:00+11:00"), now=NOW)
        self.assertEqual(profile["facets"]["career_stage"]["observed_at"], "2026-10-20T00:00:00.000000Z")


class SourceMappingTests(TestCase):
    def test_only_reviewed_exact_mappings_and_aliases_promote_values(self):
        with self.assertRaises(AudienceProfileError):
            validate_mapping(mapping(reviewed=False))
        self.assertIsNone(normalize_value("career", "Chief of everything"))
        self.assertIsNone(normalize_value("gender", "Jamie"))
        self.assertIsNone(normalize_value("age_band", "adult confirmed"))
        self.assertEqual(observation("junior", source=mapping(aliases={"junior": "early_career"}))["values"], ["early_career"])

    def test_unknown_value_and_prefer_not_to_say_are_distinct(self):
        self.assertEqual(observation("unmapped career answer")["status"], "unmapped")
        self.assertEqual(observation("")["status"], "not_provided")
        withheld = observation("Prefer not to say", source=mapping("gender"))
        self.assertEqual((withheld["status"], withheld["values"]), ("withheld", []))

    def test_same_source_new_answer_supersedes_and_old_replay_is_ignored(self):
        first = merge_observation(None, observation())
        current = merge_observation(first, observation("Senior", at="2026-11-01T00:00:00Z"))
        self.assertEqual(current["facets"]["career_stage"]["values"], ["senior"])
        replayed = merge_observation(current, observation())
        self.assertEqual(replayed, current)

    def test_concurrent_sources_conflict_and_explicit_correction_resolves(self):
        first = merge_observation(None, observation())
        conflict = merge_observation(first, observation("Senior", record="different-current-source"))
        self.assertEqual(conflict["facets"]["career_stage"]["status"], "conflicted")
        resolved = correction_profile(with_profile({}, conflict), correction(), now=NOW)
        self.assertEqual(resolved["facets"]["career_stage"]["values"], ["early_career"])

    def test_ordinary_import_does_not_replace_reported_choice(self):
        first = merge_observation(None, observation("Founder", source=mapping("career")))
        lower = observation("Developer", record="csv-row", source=mapping("career", source_kind="csv", provenance="imported"))
        result = merge_observation(first, lower)
        self.assertEqual(result["facets"]["career"]["values"], ["founder"])

    def test_source_retraction_removes_only_that_sources_contribution(self):
        source = mapping("career")
        first = merge_observation(None, observation("Founder", source=source))
        both = merge_observation(first, observation("Developer", record="other-answer", source=source))
        retracted = merge_observation(both, observation("", at="2026-11-01T00:00:00Z", source=source))
        self.assertEqual(retracted["facets"]["career"]["values"], ["developer"])

    def test_missing_source_refresh_is_idempotent_and_exact_restoration_is_accepted(self):
        source = mapping("career", id="form")
        original = observation("Founder", record="mapping:form:answer-1", source=source)
        first = merge_observation(None, original)
        first = merge_observation(first, observation("Developer", record="mapping:form-extra:answer-2", source=source))
        retracted = retract_mapping_sources(first, source, set(), now=NOW)
        self.assertEqual(retracted["facets"]["career"]["values"], ["developer"])
        repeated = retract_mapping_sources(retracted, source, set(), now=NOW + timedelta(days=1))
        self.assertEqual(repeated, retracted)
        restored = merge_observation(repeated, original)
        self.assertEqual(restored["facets"]["career"]["values"], ["developer", "founder"])
        restored_source = next(item for item in restored["facets"]["career"]["evidence"]
                               if item["source_record_id"] == "mapping:form:answer-1")
        self.assertEqual(restored_source, original["evidence"])

    def test_mapping_ids_cannot_collide_with_source_reference_prefixes(self):
        for mapping_id in ("form:nested", "form:", ":form"):
            with self.subTest(mapping_id=mapping_id), self.assertRaises(AudienceProfileError) as caught:
                validate_mapping(mapping(id=mapping_id))
            self.assertEqual(caught.exception.reason, "mapping_id_contains_reserved_separator")

    def test_city_connection_never_changes_reported_city(self):
        profile = merge_observation(None, observation("Adelaide", source=mapping("location_affinity", source_kind="membership")))
        self.assertEqual(profile["facets"]["location_affinity"]["values"], ["au-sa-adelaide"])
        self.assertNotIn("reported_city", profile["facets"])

    def test_batch_preview_is_idempotent_and_returns_no_raw_answers(self):
        records = [{"mapping_id": "reviewed-form-field", "source_record_id": "answer-1", "observed_at": "2026-10-20T00:00:00Z", "value": "Early career"}]
        first, counts = project_records({}, records, [mapping()], now=NOW)
        second, _ = project_records(with_profile({}, first), records, [mapping()], now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(counts, {"known": 1})
        self.assertNotIn("Early career", str(first))


class ActivityEvidenceTests(TestCase):
    def coverage(self, **changes):
        return {"last_observed_at": None, "covered_from": (NOW - timedelta(days=180)).isoformat(),
                "covered_through": NOW.isoformat(), "status": "available", "identity_status": "verified", **changes}

    def test_absence_is_negative_only_with_full_interval_and_verified_identity(self):
        self.assertTrue(activity_match(self.coverage(), within_days=90, now=NOW, negative=True))
        for changes in ({"status": "stale"}, {"identity_status": "unmapped"},
                        {"covered_from": (NOW - timedelta(days=10)).isoformat()},
                        {"covered_through": (NOW - timedelta(days=3)).isoformat()}):
            with self.subTest(changes=changes):
                self.assertIsNone(activity_match(self.coverage(**changes), within_days=90, now=NOW, negative=True))

    def test_fresh_past_watermark_supports_negative_activity_without_future_coverage(self):
        evidence = self.coverage(covered_through=(NOW - timedelta(minutes=5)).isoformat())
        self.assertTrue(activity_match(evidence, within_days=90, now=NOW, negative=True))
        newer = {**evidence, "last_observed_at": (NOW - timedelta(minutes=1)).isoformat()}
        self.assertFalse(activity_match(newer, within_days=90, now=NOW, negative=True))

    def test_real_positive_observation_survives_a_source_outage(self):
        known = self.coverage(last_observed_at=(NOW - timedelta(days=2)).isoformat())
        profile = merge_activity(None, "slack_message", known)
        updated = merge_activity(profile, "slack_message", self.coverage(status="unavailable"))
        self.assertTrue(activity_match(updated["activity"]["slack_message"], within_days=90, now=NOW))
        self.assertFalse(activity_match(updated["activity"]["slack_message"], within_days=90, now=NOW, negative=True))

    def test_empty_failed_collector_keeps_identity_of_retained_historical_evidence(self):
        profile = merge_activity(None, "slack_message", self.coverage(last_observed_at=(NOW - timedelta(days=2)).isoformat()))
        outage = activity_projection([], {"slack": {"status": "unavailable"}}, now=NOW)["slack_message"]
        self.assertEqual(outage["identity_status"], "unmapped")
        updated = merge_activity(profile, "slack_message", outage)
        self.assertTrue(activity_match(updated["activity"]["slack_message"], within_days=90, now=NOW))
        conflicted = merge_activity(profile, "slack_message", {**outage, "identity_status": "conflicted"})
        self.assertIsNone(activity_match(conflicted["activity"]["slack_message"], within_days=90, now=NOW))

    def test_future_event_is_never_recent_activity(self):
        future = self.coverage(last_observed_at=(NOW + timedelta(seconds=1)).isoformat(), status="partial")
        self.assertIsNone(activity_match(future, within_days=90, now=NOW))

    def test_invalid_coverage_interval_and_false_available_are_rejected(self):
        for changes in ({"covered_from": None, "covered_through": None},
                        {"covered_from": (NOW + timedelta(days=1)).isoformat()},
                        {"identity_status": "assumed"}):
            with self.subTest(changes=changes), self.assertRaises(AudienceProfileError):
                validate_activity(self.coverage(**changes))

    def test_profile_roundtrip_does_not_share_mutable_state(self):
        profile = empty_profile()
        result = validate_profile(profile)
        result["activity"]["slack_message"] = self.coverage()
        self.assertEqual(profile, empty_profile())
