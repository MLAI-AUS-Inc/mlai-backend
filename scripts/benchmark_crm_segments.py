"""Optional synthetic audience benchmark for an approved disposable PostgreSQL.

Select one class with the guarded database runner; this module starts no server,
loads no credentials, and uses TestCase rollback. It is not a timing assertion or
a production-data load test. Example labels:
scripts.benchmark_crm_segments.SegmentBenchmark10000
scripts.benchmark_crm_segments.SegmentBenchmark100000
"""

import json
import math
from statistics import median
from time import perf_counter

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from crm.audience_profiles import correction_profile, with_profile
from crm.audience_profile_rules import profile_catalog
from crm.models import Contact
from crm.segment_trees import definition_hash
from crm.segments import evaluate, segment_contacts


class SegmentBenchmarkBase(TestCase):
    """Generate independent synthetic facts and report cold/warm query timings."""

    size = 0

    def run_benchmark(self):
        now = timezone.now()
        profiles = {}
        cities = ("au-sa-adelaide", "au-vic-melbourne", "au-nsw-sydney", "au-wa-perth")
        for city in cities:
            for stage in ("early_career", "senior"):
                attributes = {}
                for facet, value in (("location_affinity", city), ("career_stage", stage)):
                    profile = correction_profile(attributes, {
                        "facet": facet, "values": [value], "provenance": "self_reported",
                        "source_record_id": "synthetic-benchmark", "observed_at": now.isoformat(),
                    }, now=now)
                    attributes = with_profile(attributes, profile)
                profiles[(city, stage)] = attributes
        expected = 0
        load_started = perf_counter()
        for offset in range(0, self.size, 1000):
            batch = []
            for index in range(offset, min(offset + 1000, self.size)):
                stage = "early_career" if index % 3 == 0 else "senior"
                attributes = {**profiles[(cities[index % 4], stage)], "engagement_rating": {"stars": index % 5 + 1}}
                excluded = index % 7 == 0
                expected += int(index % 4 < 2 and index % 3 == 0 and index % 5 >= 3 and not excluded)
                batch.append(Contact(
                    email=f"segment-benchmark-{index:07d}@example.test", source="import",
                    display_name=f"Synthetic {index:07d}", attributes=attributes,
                    tags=["excluded"] if excluded else [], marketing_consent="granted",
                ))
            Contact.objects.bulk_create(batch, batch_size=1000)
        load_seconds = perf_counter() - load_started
        source = {"version": 2, "include": {"kind": "rule", "rule": {
            "type": "location_affinity", "op": "has_any", "city_ids": list(cities[:2]),
        }}, "exclude": None}
        definition = {"version": 2, "include": {"kind": "group", "match": "all", "children": [
            {"kind": "saved_segment_snapshot", "source_segment_id": "6f1c7d0e-9b1a-4d4c-8a43-6c1f0a0b1c2d",
             "source_name": "Synthetic cities", "source_definition_hash": definition_hash(source),
             "captured_at": now.isoformat(), "definition": source},
            {"kind": "rule", "rule": {"type": "audience_facet", "facet": "career_stage", "op": "has_any", "values": ["early_career"]}},
            {"kind": "rule", "rule": {"type": "engagement_stars", "op": "gte", "value": 4}},
        ]}, "exclude": {"kind": "rule", "rule": {"type": "tag", "op": "has_any", "values": ["excluded"]}}}
        durations, query_counts = [], []
        for _ in range(6):
            queries = []

            def measured(execute, sql, params, many, context):
                started = perf_counter()
                try:
                    return execute(sql, params, many, context)
                finally:
                    queries.append(perf_counter() - started)

            started = perf_counter()
            with connection.execute_wrapper(measured):
                matched = segment_contacts(definition, now=now)
                self.assertEqual(matched.count(), expected)
                eligible, _ = evaluate(definition, "marketing", now=now)
                self.assertEqual(eligible.count(), expected)
                list(eligible.order_by("display_name", "pk").values("pk", "display_name")[:20])
            durations.append((perf_counter() - started) * 1000)
            query_counts.append(len(queries))
        warm = durations[1:]
        catalogue_started = perf_counter()
        catalogue = profile_catalog(Contact.objects.all(), now=now)
        self.assertEqual(next(facet for facet in catalogue["facets"] if facet["id"] == "career_stage")["known_count"], self.size)
        catalogue_ms = (perf_counter() - catalogue_started) * 1000
        report = {
            "benchmark": "nested-snapshot-location-stage-warm-exclusion",
            "contacts": self.size, "matches": expected, "load_seconds": round(load_seconds, 3),
            "cold_ms": round(durations[0], 3), "warm_runs": len(warm),
            "warm_p50_ms": round(median(warm), 3),
            "warm_p95_ms": round(sorted(warm)[math.ceil(len(warm) * .95) - 1], 3),
            "queries_per_run": query_counts,
            "profile_catalog_ms": round(catalogue_ms, 3),
        }
        print("CRM_SEGMENT_BENCHMARK " + json.dumps(report, sort_keys=True), flush=True)


class SegmentBenchmark10000(SegmentBenchmarkBase):
    size = 10_000

    def test_synthetic_audience(self):
        self.run_benchmark()


class SegmentBenchmark100000(SegmentBenchmarkBase):
    size = 100_000

    def test_synthetic_audience(self):
        self.run_benchmark()
