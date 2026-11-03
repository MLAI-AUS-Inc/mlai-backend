"""Exact PostgreSQL aggregate equivalence over synthetic protected profile states."""

from copy import deepcopy
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from crm.audience_catalog import empty_counts, postgres_profile_counts
from crm.audience_profile_rules import _orm_profile_counts, profile_catalog
from crm.audience_profiles import AUDIENCE_PROFILE_KEY, empty_profile
from crm.audience_taxonomy import ACTIVITY_SOURCES, FACETS, FACET_STATUSES
from crm.models import Contact


@skipUnless(connection.vendor == "postgresql", "JSONB parity requires PostgreSQL")
class AudienceCatalogDatabaseTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.index = 0

    def contact(self, profile, *, scoped=True):
        self.index += 1
        return Contact.objects.create(
            email=f"synthetic-catalog-{self.index}@example.test",
            tags=["in_scope"] if scoped else [],
            attributes={AUDIENCE_PROFILE_KEY: profile} if profile is not None else {},
        )

    def test_single_pass_matches_reference_for_statuses_options_activity_and_scope(self):
        for status in FACET_STATUSES:
            profile = empty_profile()
            profile["facets"] = {
                facet: {"status": status, "values": list(spec["options"]) * 2 if status == "known" else [],
                        "value_count": len(spec["options"]) if status == "known" else 0}
                for facet, spec in FACETS.items()
            }
            self.contact(profile)
        self.contact(None)
        self.contact({"schema_version": 2, "taxonomy_version": "2026-11-03", "facets": {"gender": {"status": "known", "values": ["woman"], "value_count": 1}}})
        self.contact({"schema_version": 1, "taxonomy_version": "old", "facets": {"career": {"status": "known", "values": ["developer"], "value_count": 1}}})
        for index, status in enumerate(("available", "partial", "stale", "unavailable")):
            profile = empty_profile()
            profile["activity"] = {source: {
                "status": status, "identity_status": "verified" if index < 3 else "unmapped",
                "last_observed_at": (self.now + timedelta(days=1 if index == 2 else -1)).isoformat(timespec="microseconds").replace("+00:00", "Z") if index else None,
            } for source in ACTIVITY_SOURCES}
            self.contact(profile)
        profile = empty_profile()
        profile["facets"] = {"career": {"status": "known", "values": ["developer"], "value_count": 1}}
        self.contact(profile, scoped=False)
        scoped = Contact.objects.filter(tags__contains=["in_scope"])
        expected = _orm_profile_counts(scoped, now=self.now)
        with self.assertNumQueries(1):
            actual = postgres_profile_counts(scoped, now=self.now)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["total"], 12)
        self.assertEqual(actual["career_option_1"], 1)
        self.assertEqual(actual["gender_withheld"], 1)
        self.assertEqual(actual["activity_slack_message_known"], 1)
        # No source-value arrays or provenance escape the aggregate.
        self.assertTrue(all(isinstance(value, int) for value in actual.values()))
        with self.assertNumQueries(1):
            catalog = profile_catalog(scoped, now=self.now)
        self.assertEqual(next(facet for facet in catalog["facets"] if facet["id"] == "career")["known_count"], 1)
        self.assertEqual(postgres_profile_counts(scoped), _orm_profile_counts(scoped))

    def test_empty_and_parameterized_authorized_querysets(self):
        self.assertEqual(postgres_profile_counts(Contact.objects.none(), now=self.now), empty_counts())
        profile = empty_profile()
        profile["facets"] = {"career": {"status": "known", "values": ["founder"], "value_count": 1}}
        included = self.contact(deepcopy(profile))
        self.contact(deepcopy(profile))
        scoped = Contact.objects.filter(pk=included.pk, display_name__contains="' OR true --")
        self.assertEqual(postgres_profile_counts(scoped, now=self.now), empty_counts())
        scoped = Contact.objects.filter(pk=included.pk)
        self.assertEqual(postgres_profile_counts(scoped, now=self.now), _orm_profile_counts(scoped, now=self.now))
        for scoped in (Contact.objects.order_by("id")[:1], Contact.objects.order_by("id").distinct("id")):
            self.assertEqual(postgres_profile_counts(scoped, now=self.now)["total"], scoped.count())
