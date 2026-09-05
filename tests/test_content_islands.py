"""Content islands: persisted AI-search research (Phase 0) + the island entity (Phase 1).

Phase 0 covers the AI-search columns content-factory already computes and used to
drop on the floor. Phase 1 covers ContentIsland and the two /api/seo/islands/
endpoints, whose bulk view owns the whole birth/update/miss/archive/promote state
machine. NOTE: sqlite does NOT enforce varchar lengths, so the column limits are
asserted explicitly rather than relied on to raise.
"""
import os
from datetime import date, timedelta
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.content_islands import (
    ISLAND_COLOR_KEYS,
    ISLAND_ICON_KEYS,
    compute_island_edges,
    cosine_similarity,
    least_used_color_key,
    normalize_color_key,
    normalize_icon_key,
    unique_island_slug,
)
from content_factory.models import (
    ClusterMembership,
    ContentIsland,
    ContentIslandEdge,
    ContentIslandKeyword,
    ContentIslandOrigin,
    ContentIslandRefreshDispatch,
    ContentIslandRefreshDispatchStatus,
    ContentIslandSnapshot,
    ContentIslandStatus,
    OrganizationContentConfig,
    ResearchedKeyword,
    SemanticCluster,
    WrittenArticle,
)
from content_factory.vibe_marketing_views import _topic_pillars_for_bootstrap
from organizations.models import Organization

ISLANDS_URL = "/api/seo/islands/"
ISLANDS_BULK_URL = "/api/seo/islands/bulk/"
KEYWORDS_URL = "/api/seo/keywords/"
KEYWORDS_BULK_URL = "/api/seo/keywords/bulk/"


class ContentIslandAPITestCase(TestCase):
    """Shared HasRooApiKey-authenticated client, mirroring the other SEO service tests."""

    def setUp(self):
        self.client = APIClient()
        self.api_key = "test-content-islands-key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key
        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.org = Organization.objects.create(name="Acme", domain="acme.example.com")

    def make_keyword(self, keyword, **kwargs):
        defaults = {
            "volume": 200,
            "difficulty": 30,
            "opportunity_index": 100.0,
        }
        defaults.update(kwargs)
        return ResearchedKeyword.objects.create(
            organization=self.org,
            keyword=keyword,
            keyword_normalized=keyword.lower().strip(),
            **defaults
        )

    def sync(self, islands, **payload):
        body = {"domain": self.org.domain, "islands": islands}
        body.update(payload)
        return self.client.post(ISLANDS_BULK_URL, body, format="json")


# =============================================================================
# Phase 0 - AI-search research must survive the round trip
# =============================================================================

class AISearchKeywordPersistenceTests(ContentIslandAPITestCase):
    """The four AI-search columns must land AND be readable back through the list API."""

    def _read_back(self, keyword_normalized):
        response = self.client.get(KEYWORDS_URL, {"domain": self.org.domain})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {str(row["keyword"]).lower(): row for row in response.data["keywords"]}
        self.assertIn(keyword_normalized, rows)
        return rows[keyword_normalized]

    def test_bulk_upsert_round_trips_all_four_ai_fields_through_the_list_endpoint(self):
        response = self.client.post(
            KEYWORDS_BULK_URL,
            {
                "domain": self.org.domain,
                "keywords": [
                    {
                        "keyword": "AI startup fundraising",
                        "volume": 880,
                        "difficulty": 34,
                        "ai_search_volume": 610,
                        "ai_monthly_searches": [120, 180, 310, 610],
                        "aeo_score": 0.62,
                        "query_type": "informational",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        row = self._read_back("ai startup fundraising")
        self.assertEqual(row["ai_search_volume"], 610)
        self.assertEqual(row["ai_monthly_searches"], [120, 180, 310, 610])
        self.assertAlmostEqual(row["aeo_score"], 0.62)
        self.assertEqual(row["query_type"], "informational")

    def test_camel_case_ai_fields_are_accepted(self):
        response = self.client.post(
            KEYWORDS_BULK_URL,
            {
                "domain": self.org.domain,
                "keywords": [
                    {
                        "keyword": "seed round checklist",
                        "volume": 210,
                        "aiSearchVolume": 44,
                        "aiMonthlySearches": [10, 20, 44],
                        "aeoScore": 0.4,
                        "queryType": "commercial",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        row = self._read_back("seed round checklist")
        self.assertEqual(row["ai_search_volume"], 44)
        self.assertEqual(row["ai_monthly_searches"], [10, 20, 44])
        self.assertAlmostEqual(row["aeo_score"], 0.4)
        self.assertEqual(row["query_type"], "commercial")

    def test_absent_ai_fields_do_not_clobber_stored_values(self):
        self.client.post(
            KEYWORDS_BULK_URL,
            {
                "domain": self.org.domain,
                "keywords": [
                    {
                        "keyword": "AI startup fundraising",
                        "volume": 880,
                        "ai_search_volume": 610,
                        "ai_monthly_searches": [120, 180, 310, 610],
                        "aeo_score": 0.62,
                        "query_type": "informational",
                    }
                ],
            },
            format="json",
        )

        # A later run with the GEO flags off sends no AI keys at all.
        response = self.client.post(
            KEYWORDS_BULK_URL,
            {
                "domain": self.org.domain,
                "keywords": [{"keyword": "AI startup fundraising", "volume": 1200, "difficulty": 41}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        row = self._read_back("ai startup fundraising")
        self.assertEqual(row["volume"], 1200)  # the second write really happened
        self.assertEqual(row["ai_search_volume"], 610)
        self.assertEqual(row["ai_monthly_searches"], [120, 180, 310, 610])
        self.assertAlmostEqual(row["aeo_score"], 0.62)
        self.assertEqual(row["query_type"], "informational")

    def test_never_measured_keyword_reads_as_null_not_zero(self):
        self.client.post(
            KEYWORDS_BULK_URL,
            {"domain": self.org.domain, "keywords": [{"keyword": "cap table basics", "volume": 90}]},
            format="json",
        )
        row = self._read_back("cap table basics")
        self.assertIsNone(row["ai_search_volume"])
        self.assertIsNone(row["aeo_score"])
        self.assertEqual(row["ai_monthly_searches"], [])
        self.assertEqual(row["query_type"], "")


# =============================================================================
# Phase 1 - pure helpers
# =============================================================================

class ContentIslandConstantsTests(TestCase):
    def test_icon_and_color_enums_are_the_agreed_sets(self):
        self.assertEqual(
            ISLAND_COLOR_KEYS,
            ["green", "purple", "blue", "orange", "teal", "rose", "amber", "indigo", "cyan", "lime"],
        )
        self.assertEqual(
            ISLAND_ICON_KEYS,
            ["brain", "community", "rocket", "tools", "chart", "globe", "shield", "leaf", "bolt", "default"],
        )

    def test_unknown_icon_falls_back_to_default(self):
        self.assertEqual(normalize_icon_key("chart"), "chart")
        self.assertEqual(normalize_icon_key("sparkles"), "default")
        self.assertEqual(normalize_icon_key(None), "default")
        self.assertEqual(normalize_icon_key(""), "default")

    def test_unknown_color_falls_back_to_least_used(self):
        self.assertEqual(normalize_color_key("teal"), "teal")
        self.assertEqual(normalize_color_key("chartreuse", used_color_keys=["green"]), "purple")
        self.assertEqual(least_used_color_key(["green", "purple", "blue"]), "orange")

    def test_slug_dedupe_suffixes_and_respects_the_column_limit(self):
        self.assertEqual(unique_island_slug("AI Fundraising", set()), "ai-fundraising")
        self.assertEqual(unique_island_slug("AI Fundraising", {"ai-fundraising"}), "ai-fundraising-2")
        long_slug = unique_island_slug("x" * 200, set())
        self.assertEqual(len(long_slug), 80)
        self.assertLessEqual(len(unique_island_slug("x" * 200, {long_slug})), 80)


class ContentIslandEdgeMathTests(TestCase):
    class _FakeIsland:
        def __init__(self, slug, centroid):
            self.slug = slug
            self.centroid_embedding = centroid

    def test_cosine_is_degenerate_safe(self):
        self.assertEqual(cosine_similarity([], [1.0]), 0.0)
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 0.0]), 0.0)
        self.assertEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_edges_drop_below_threshold_and_order_canonically(self):
        islands = [
            self._FakeIsland("zeta", [1.0, 0.0, 0.0]),
            self._FakeIsland("alpha", [0.8, 0.6, 0.0]),
            self._FakeIsland("omega", [0.0, 0.0, 1.0]),
        ]
        edges = compute_island_edges(islands)
        self.assertEqual([(a, b) for a, b, _ in edges], [("alpha", "zeta")])
        self.assertAlmostEqual(edges[0][2], 0.8)

    def test_islands_without_centroids_are_skipped(self):
        islands = [
            self._FakeIsland("a", [1.0, 0.0]),
            self._FakeIsland("b", []),
        ]
        self.assertEqual(compute_island_edges(islands), [])

    def test_top_n_truncation_drops_a_pair_neither_endpoint_ranks(self):
        # Unit vectors with cos(alpha,beta)=0.9, cos(alpha,gamma)=0.5, cos(beta,gamma)=0.4.
        islands = [
            self._FakeIsland("alpha", [1.0, 0.0, 0.0]),
            self._FakeIsland("beta", [0.9, 0.43589, 0.0]),
            self._FakeIsland("gamma", [0.5, -0.11470, 0.85840]),
        ]
        self.assertEqual(
            [(a, b) for a, b, _ in compute_island_edges(islands)],
            [("alpha", "beta"), ("alpha", "gamma"), ("beta", "gamma")],
        )
        # With one neighbour each, beta~gamma is nobody's best link and falls away.
        self.assertEqual(
            [(a, b) for a, b, _ in compute_island_edges(islands, top_n=1)],
            [("alpha", "beta"), ("alpha", "gamma")],
        )


# =============================================================================
# Phase 1 - GET /api/seo/islands/ and seed-on-first-read
# =============================================================================

class ContentIslandListAndSeedTests(ContentIslandAPITestCase):
    def _seed_and_compare_with_bootstrap_pillars(self):
        config = OrganizationContentConfig.objects.filter(organization=self.org).first()
        pillars = _topic_pillars_for_bootstrap(self.org, config)
        self.assertTrue(pillars, "fixture must produce bootstrap pillars")

        response = self.client.get(ISLANDS_URL, {"domain": self.org.domain, "seed": "1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["seeded"])

        islands = {island["slug"]: island for island in response.data["islands"]}
        self.assertEqual(sorted(islands.keys()), sorted(pillar["slug"] for pillar in pillars))
        for pillar in pillars:
            island = islands[pillar["slug"]]
            self.assertEqual(island["name"], pillar["name"])
            self.assertEqual(island["description"], pillar["description"])
            self.assertEqual(island["icon_key"], pillar["iconKey"])
            self.assertEqual(island["color_key"], pillar["colorKey"])
            self.assertEqual(island["status"], ContentIslandStatus.VISIBLE)
            self.assertEqual(island["origin"], ContentIslandOrigin.PILLAR_STRATEGY_SEED)
            self.assertEqual(island["centroid_embedding"], [])
        return pillars

    def test_seed_adopts_a_strategy_only_orgs_pillars_verbatim(self):
        OrganizationContentConfig.objects.create(
            organization=self.org,
            pillar_strategy={
                "pillars": [
                    {
                        "name": "AI Startup Fundraising",
                        "keyword": "ai startup fundraising",
                        "description": "Raising money as an AI founder.",
                        "topics": ["seed round checklist", "ai investor update"],
                    },
                    {
                        "name": "Healthcare AI",
                        "keyword": "healthcare ai",
                        "description": "Clinical AI adoption.",
                        "topics": ["hospital ai rollout"],
                    },
                ]
            },
        )
        pillars = self._seed_and_compare_with_bootstrap_pillars()
        # Position-among-survivors visuals are copied, not recomputed.
        self.assertEqual([pillar["colorKey"] for pillar in pillars], ["green", "purple"])
        island = ContentIsland.objects.get(organization=self.org, slug="ai-startup-fundraising")
        self.assertEqual(island.pillar_keyword, "ai startup fundraising")
        self.assertIsNotNone(island.promoted_at)

    def test_seed_adopts_a_cluster_backed_orgs_pillars_verbatim(self):
        OrganizationContentConfig.objects.create(organization=self.org, pillar_strategy={})
        for index, (pillar_keyword, members) in enumerate(
            [
                ("ai startup fundraising", ["seed round checklist", "ai investor update"]),
                ("healthcare ai", ["hospital ai rollout"]),
            ],
            start=1,
        ):
            cluster = SemanticCluster.objects.create(
                organization=self.org,
                cluster_id=index,
                pillar_keyword=pillar_keyword,
                total_volume=1000 - index,
            )
            for member in members:
                ClusterMembership.objects.create(
                    keyword=self.make_keyword(member),
                    cluster=cluster,
                    is_pillar=False,
                    similarity_score=0.8,
                )

        pillars = self._seed_and_compare_with_bootstrap_pillars()
        self.assertEqual([pillar["source"] for pillar in pillars], ["semantic_cluster", "semantic_cluster"])
        island = ContentIsland.objects.get(organization=self.org, slug="ai-startup-fundraising")
        self.assertEqual(island.pillar_keyword, "ai startup fundraising")

    def test_seed_is_a_one_shot_and_never_re_runs(self):
        OrganizationContentConfig.objects.create(
            organization=self.org,
            pillar_strategy={"pillars": [{"name": "AI Startup Fundraising", "keyword": "ai startup fundraising"}]},
        )
        first = self.client.get(ISLANDS_URL, {"domain": self.org.domain, "seed": "1"})
        self.assertTrue(first.data["seeded"])

        second = self.client.get(ISLANDS_URL, {"domain": self.org.domain, "seed": "1"})
        self.assertFalse(second.data["seeded"])
        self.assertEqual(ContentIsland.objects.filter(organization=self.org).count(), 1)

    def test_read_without_seed_flag_creates_nothing(self):
        OrganizationContentConfig.objects.create(
            organization=self.org,
            pillar_strategy={"pillars": [{"name": "AI Startup Fundraising", "keyword": "ai startup fundraising"}]},
        )
        response = self.client.get(ISLANDS_URL, {"domain": self.org.domain})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["seeded"])
        self.assertEqual(response.data["islands"], [])
        self.assertFalse(ContentIsland.objects.filter(organization=self.org).exists())

    def test_seeding_an_org_with_no_pillars_at_all_is_a_no_op_not_a_500(self):
        response = self.client.get(ISLANDS_URL, {"domain": self.org.domain, "seed": "1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["seeded"])
        self.assertEqual(response.data["islands"], [])

    def test_archived_islands_are_not_served(self):
        ContentIsland.objects.create(
            organization=self.org,
            slug="gone",
            name="Gone",
            pillar_keyword="gone",
            status=ContentIslandStatus.ARCHIVED,
        )
        ContentIsland.objects.create(
            organization=self.org,
            slug="here",
            name="Here",
            pillar_keyword="here",
            status=ContentIslandStatus.VISIBLE,
        )
        response = self.client.get(ISLANDS_URL, {"domain": self.org.domain})
        self.assertEqual([island["slug"] for island in response.data["islands"]], ["here"])

    def test_domain_is_required_and_unknown_domains_404(self):
        self.assertEqual(self.client.get(ISLANDS_URL).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            self.client.get(ISLANDS_URL, {"domain": "nope.example.com"}).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_endpoints_require_the_roo_api_key(self):
        anonymous = APIClient()
        self.assertIn(
            anonymous.get(ISLANDS_URL, {"domain": self.org.domain}).status_code,
            {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN},
        )
        self.assertIn(
            anonymous.post(ISLANDS_BULK_URL, {"domain": self.org.domain, "islands": []}, format="json").status_code,
            {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN},
        )


class ContentIslandUrlRegistrationTests(TestCase):
    def test_routes_are_registered_with_and_without_a_trailing_slash(self):
        # APPEND_SLASH is False fleet-wide, so both spellings must resolve.
        self.assertEqual(reverse("seo_content_island_list"), ISLANDS_URL)
        self.assertEqual(reverse("seo_content_island_list_no_slash"), "/api/seo/islands")
        self.assertEqual(reverse("seo_content_island_bulk_sync"), ISLANDS_BULK_URL)
        self.assertEqual(reverse("seo_content_island_bulk_sync_no_slash"), "/api/seo/islands/bulk")
        self.assertEqual(
            resolve("/api/seo/islands").func.view_class,
            resolve(ISLANDS_URL).func.view_class,
        )
        self.assertEqual(
            resolve("/api/seo/islands/bulk").func.view_class,
            resolve(ISLANDS_BULK_URL).func.view_class,
        )


# =============================================================================
# Phase 1 - POST /api/seo/islands/bulk/ state machine
# =============================================================================

class ContentIslandBulkSyncTests(ContentIslandAPITestCase):
    def birth(self, name, centroid, members=None, **metrics):
        entry = {
            "slug": None,
            "name": name,
            "description": f"{name} description",
            "pillar_keyword": name.lower(),
            "icon_key": "chart",
            "color_key": "teal",
            "centroid_embedding": centroid,
            "origin": "cluster_birth",
            "matched": False,
            "metrics": {
                "keyword_count": metrics.get("keyword_count", 6),
                "total_volume": metrics.get("total_volume", 900),
                "avg_difficulty": metrics.get("avg_difficulty", 31.2),
                "opportunity_score": metrics.get("opportunity_score", 8120.5),
                "ai_search_volume": metrics.get("ai_search_volume", 610),
            },
        }
        if members is not None:
            entry["members"] = members
        return entry

    def test_birth_creates_an_island_with_a_server_assigned_slug(self):
        response = self.sync([self.birth("AI Startup Fundraising", [1.0, 0.0, 0.0])])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 1)
        self.assertEqual(response.data["created_slugs"], ["ai-startup-fundraising"])

        island = ContentIsland.objects.get(organization=self.org, slug="ai-startup-fundraising")
        self.assertEqual(island.name, "AI Startup Fundraising")
        self.assertEqual(island.icon_key, "chart")
        self.assertEqual(island.color_key, "teal")
        self.assertEqual(island.origin, ContentIslandOrigin.CLUSTER_BIRTH)
        self.assertEqual(island.centroid_embedding, [1.0, 0.0, 0.0])
        self.assertEqual(island.total_volume, 900)
        self.assertEqual(island.ai_search_volume, 610)
        self.assertIsNotNone(island.first_seen_at)

    def test_birth_without_a_centroid_is_rejected_and_writes_nothing(self):
        entry = self.birth("Ghost Island", [])
        response = self.sync([entry, self.birth("Real Island", [1.0, 0.0])])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("centroid_embedding", response.data["error"])
        self.assertFalse(ContentIsland.objects.filter(organization=self.org).exists())

    def test_birth_slugs_dedupe_with_a_numeric_suffix(self):
        self.sync([self.birth("AI Startup Fundraising", [1.0, 0.0, 0.0])])
        response = self.sync([self.birth("AI Startup Fundraising", [0.0, 1.0, 0.0])])
        self.assertEqual(response.data["created_slugs"], ["ai-startup-fundraising-2"])
        self.assertEqual(ContentIsland.objects.filter(organization=self.org).count(), 2)

    def test_invalid_icon_and_color_fall_back_instead_of_500ing(self):
        entry = self.birth("Odd Visuals", [1.0, 0.0])
        entry["icon_key"] = "sparkles"
        entry["color_key"] = "chartreuse"
        response = self.sync([entry])
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        island = ContentIsland.objects.get(organization=self.org, slug="odd-visuals")
        self.assertEqual(island.icon_key, "default")
        self.assertEqual(island.color_key, "green")  # least-used of an empty palette

    def test_update_preserves_identity_and_visuals_while_refreshing_metrics(self):
        self.sync([self.birth("AI Startup Fundraising", [1.0, 0.0, 0.0])])
        island = ContentIsland.objects.get(slug="ai-startup-fundraising")
        island.consecutive_misses = 3
        island.save(update_fields=["consecutive_misses"])

        response = self.sync([
            {
                "slug": "ai-startup-fundraising",
                "name": "AI Startup Fundraising",
                "pillar_keyword": "ai fundraising",
                "icon_key": "rocket",
                "color_key": "rose",
                "centroid_embedding": [0.9, 0.1, 0.0],
                "matched": True,
                "metrics": {"keyword_count": 14, "total_volume": 5200, "opportunity_score": 9310.2},
            }
        ])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(response.data["updated"], 1)

        island.refresh_from_db()
        self.assertEqual(island.slug, "ai-startup-fundraising")
        self.assertEqual(island.icon_key, "chart")  # stamped at creation, never reassigned
        self.assertEqual(island.color_key, "teal")
        self.assertEqual(island.pillar_keyword, "ai fundraising")
        self.assertEqual(island.centroid_embedding, [0.9, 0.1, 0.0])
        self.assertEqual(island.keyword_count, 14)
        self.assertEqual(island.total_volume, 5200)
        self.assertEqual(island.consecutive_misses, 0)
        self.assertIsNotNone(island.last_matched_at)
        self.assertIsNotNone(island.last_refreshed_at)

    def test_members_replace_the_membership_set_and_unknown_keywords_are_skipped_softly(self):
        known = self.make_keyword("seed round checklist")
        stale = self.make_keyword("old topic")
        entry = self.birth(
            "AI Startup Fundraising",
            [1.0, 0.0],
            members=[{"keyword_normalized": "old topic", "similarity_score": 0.5}],
        )
        self.sync([entry])
        island = ContentIsland.objects.get(slug="ai-startup-fundraising")
        self.assertEqual(
            list(island.memberships.values_list("keyword_id", flat=True)), [stale.id]
        )

        response = self.sync([
            {
                "slug": "ai-startup-fundraising",
                "matched": True,
                "centroid_embedding": [1.0, 0.0],
                "members": [
                    {"keyword_normalized": "seed round checklist", "similarity_score": 0.87, "is_centroid": True},
                    {"keyword_normalized": "never synced keyword", "similarity_score": 0.55},
                ],
            }
        ])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["skipped_keywords"], 1)

        memberships = list(ContentIslandKeyword.objects.filter(island=island))
        self.assertEqual([membership.keyword_id for membership in memberships], [known.id])
        self.assertAlmostEqual(memberships[0].similarity_score, 0.87)
        self.assertTrue(memberships[0].is_centroid)
        island.refresh_from_db()
        self.assertEqual(island.keyword_count, 1)  # keyword_count defaults to resolved members

    @override_settings(CONTENT_ISLANDS_PROMOTION_MIN_KEYWORDS=5, CONTENT_ISLANDS_PROMOTION_MIN_VOLUME=200)
    def test_promotion_requires_both_thresholds(self):
        response = self.sync([
            self.birth("Big Island", [1.0, 0.0], keyword_count=6, total_volume=900),
            self.birth("Thin Island", [0.0, 1.0], keyword_count=2, total_volume=900),
            self.birth("Quiet Island", [0.0, 0.0, 1.0], keyword_count=9, total_volume=40),
        ])
        self.assertEqual(response.data["promoted"], ["big-island"])

        self.assertEqual(ContentIsland.objects.get(slug="big-island").status, ContentIslandStatus.VISIBLE)
        self.assertIsNotNone(ContentIsland.objects.get(slug="big-island").promoted_at)
        self.assertEqual(ContentIsland.objects.get(slug="thin-island").status, ContentIslandStatus.EMERGING)
        self.assertEqual(ContentIsland.objects.get(slug="quiet-island").status, ContentIslandStatus.EMERGING)

    @override_settings(CONTENT_ISLANDS_MAX_VISIBLE=2)
    def test_visible_cap_holds_the_lowest_ranked_island_back_until_a_slot_frees(self):
        response = self.sync([
            self.birth("Alpha", [1.0, 0.0], opportunity_score=900.0),
            self.birth("Beta", [0.0, 1.0], opportunity_score=800.0),
            self.birth("Gamma", [0.0, 0.0, 1.0], opportunity_score=100.0),
        ])
        self.assertEqual(response.data["promoted"], ["alpha", "beta"])
        self.assertEqual(ContentIsland.objects.get(slug="gamma").status, ContentIslandStatus.EMERGING)

        # Free a slot; the highest-ranked emerging island takes it on the next sync.
        beta = ContentIsland.objects.get(slug="beta")
        beta.status = ContentIslandStatus.ARCHIVED
        beta.archived_at = timezone.now()
        beta.save(update_fields=["status", "archived_at"])

        response = self.sync([
            {"slug": "alpha", "matched": True, "centroid_embedding": [1.0, 0.0]},
            {"slug": "gamma", "matched": True, "centroid_embedding": [0.0, 0.0, 1.0]},
        ])
        self.assertEqual(response.data["promoted"], ["gamma"])
        self.assertEqual(ContentIsland.objects.get(slug="gamma").status, ContentIslandStatus.VISIBLE)

    @override_settings(CONTENT_ISLANDS_MAX_VISIBLE=1)
    def test_a_visible_island_is_never_demoted_by_the_cap(self):
        with override_settings(CONTENT_ISLANDS_MAX_VISIBLE=3):
            self.sync([
                self.birth("Alpha", [1.0, 0.0], opportunity_score=900.0),
                self.birth("Beta", [0.0, 1.0], opportunity_score=800.0),
            ])
        self.sync([
            {"slug": "alpha", "matched": True, "centroid_embedding": [1.0, 0.0]},
            {"slug": "beta", "matched": True, "centroid_embedding": [0.0, 1.0]},
        ])
        self.assertEqual(ContentIsland.objects.get(slug="alpha").status, ContentIslandStatus.VISIBLE)
        self.assertEqual(ContentIsland.objects.get(slug="beta").status, ContentIslandStatus.VISIBLE)

    def test_a_miss_counts_at_most_once_per_calendar_day(self):
        self.sync([self.birth("AI Startup Fundraising", [1.0, 0.0])], captured_on="2026-08-23")

        for _ in range(3):
            self.sync([], captured_on="2026-08-24")
        island = ContentIsland.objects.get(slug="ai-startup-fundraising")
        self.assertEqual(island.consecutive_misses, 1)
        self.assertEqual(island.last_missed_on, date(2026, 8, 24))

        self.sync([], captured_on="2026-08-25")
        island.refresh_from_db()
        self.assertEqual(island.consecutive_misses, 2)

    @override_settings(CONTENT_ISLANDS_ARCHIVE_AFTER_MISSES=5)
    def test_an_island_archives_after_five_distinct_days_of_misses(self):
        self.sync([self.birth("AI Startup Fundraising", [1.0, 0.0])], captured_on="2026-08-23")
        start = date(2026, 8, 24)
        for offset in range(4):
            self.sync([], captured_on=(start + timedelta(days=offset)).isoformat())
        island = ContentIsland.objects.get(slug="ai-startup-fundraising")
        self.assertEqual(island.consecutive_misses, 4)
        self.assertNotEqual(island.status, ContentIslandStatus.ARCHIVED)

        response = self.sync([], captured_on=(start + timedelta(days=4)).isoformat())
        self.assertEqual(response.data["archived"], ["ai-startup-fundraising"])
        island.refresh_from_db()
        self.assertEqual(island.status, ContentIslandStatus.ARCHIVED)
        self.assertIsNotNone(island.archived_at)

    @override_settings(CONTENT_ISLANDS_ARCHIVE_AFTER_MISSES=5)
    def test_an_island_with_written_articles_never_auto_archives(self):
        keyword = self.make_keyword("seed round checklist")
        article = WrittenArticle.objects.create(
            organization=self.org,
            title="Seed round checklist",
            slug="seed-round-checklist",
            category="fundraising",
            primary_keyword="seed round checklist",
        )
        keyword.written_article = article
        keyword.status = "written"
        keyword.save(update_fields=["written_article", "status"])

        self.sync(
            [
                self.birth(
                    "AI Startup Fundraising",
                    [1.0, 0.0],
                    members=[{"keyword_normalized": "seed round checklist", "similarity_score": 0.9}],
                )
            ],
            captured_on="2026-08-23",
        )
        island = ContentIsland.objects.get(slug="ai-startup-fundraising")
        self.assertEqual(island.articles_written, 1)

        start = date(2026, 8, 24)
        for offset in range(8):
            self.sync([], captured_on=(start + timedelta(days=offset)).isoformat())

        island.refresh_from_db()
        self.assertEqual(island.consecutive_misses, 8)
        self.assertNotEqual(island.status, ContentIslandStatus.ARCHIVED)

    def test_edges_are_recomputed_server_side_and_survive_a_missed_but_visible_island(self):
        self.sync(
            [
                self.birth("Alpha", [1.0, 0.0, 0.0]),
                self.birth("Beta", [0.8, 0.6, 0.0]),
                self.birth("Omega", [0.0, 0.0, 1.0]),
            ],
            captured_on="2026-08-23",
        )
        edges = list(ContentIslandEdge.objects.filter(organization=self.org))
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0].island_a.slug, edges[0].island_b.slug), ("alpha", "beta"))
        self.assertAlmostEqual(edges[0].similarity, 0.8)

        # Beta's cluster does not re-form; the edge must still be there.
        response = self.sync(
            [{"slug": "alpha", "matched": True, "centroid_embedding": [1.0, 0.0, 0.0]}],
            captured_on="2026-08-24",
        )
        self.assertEqual(response.data["edges"], 1)
        edges = list(ContentIslandEdge.objects.filter(organization=self.org))
        self.assertEqual(
            [(edge.island_a.slug, edge.island_b.slug) for edge in edges], [("alpha", "beta")]
        )

    def test_snapshots_are_one_per_island_per_day_and_replays_are_idempotent(self):
        payload = [self.birth("AI Startup Fundraising", [1.0, 0.0])]
        self.sync(payload, captured_on="2026-08-23")
        island = ContentIsland.objects.get(slug="ai-startup-fundraising")

        self.sync(
            [{
                "slug": "ai-startup-fundraising",
                "matched": True,
                "centroid_embedding": [1.0, 0.0],
                "metrics": {"keyword_count": 11, "total_volume": 4400},
            }],
            captured_on="2026-08-23",
        )
        snapshots = list(ContentIslandSnapshot.objects.filter(island=island))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].captured_on, date(2026, 8, 23))
        self.assertEqual(snapshots[0].keyword_count, 11)
        self.assertEqual(snapshots[0].total_volume, 4400)

        self.sync(
            [{"slug": "ai-startup-fundraising", "matched": True, "centroid_embedding": [1.0, 0.0]}],
            captured_on="2026-08-24",
        )
        self.assertEqual(ContentIslandSnapshot.objects.filter(island=island).count(), 2)

    def test_expanded_slugs_stamp_the_round_robin_cursor(self):
        self.sync([self.birth("Alpha", [1.0, 0.0])], captured_on="2026-08-23")
        response = self.sync(
            [{"slug": "alpha", "matched": True, "centroid_embedding": [1.0, 0.0]}],
            captured_on="2026-08-24",
            expanded=["alpha"],
        )
        self.assertEqual(response.data["expanded"], ["alpha"])
        self.assertEqual(ContentIsland.objects.get(slug="alpha").last_expanded_on, date(2026, 8, 24))

    def test_a_matching_run_id_completes_the_daily_dispatch(self):
        dispatch = ContentIslandRefreshDispatch.objects.create(
            organization=self.org,
            local_date=date(2026, 8, 23),
            content_factory_run_id="cf-run-123",
            idempotency_key=f"island-refresh:{self.org.domain}:2026-08-23",
        )
        self.assertEqual(dispatch.status, ContentIslandRefreshDispatchStatus.QUEUED)

        self.sync(
            [self.birth("Alpha", [1.0, 0.0])],
            captured_on="2026-08-23",
            run_id="cf-run-123",
        )
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, ContentIslandRefreshDispatchStatus.COMPLETED)

    def test_camel_case_payload_fields_are_accepted(self):
        response = self.sync(
            [
                {
                    "slug": None,
                    "name": "Camel Island",
                    "pillarKeyword": "camel island",
                    "iconKey": "globe",
                    "colorKey": "indigo",
                    "centroidEmbedding": [1.0, 0.0],
                    "metrics": {"keywordCount": 7, "totalVolume": 640, "aiSearchVolume": 12},
                }
            ],
            capturedOn="2026-08-23",
            runId="",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        island = ContentIsland.objects.get(slug="camel-island")
        self.assertEqual(island.pillar_keyword, "camel island")
        self.assertEqual(island.icon_key, "globe")
        self.assertEqual(island.color_key, "indigo")
        self.assertEqual(island.keyword_count, 7)
        self.assertEqual(island.total_volume, 640)
        self.assertEqual(island.ai_search_volume, 12)

    def test_bad_requests_are_rejected(self):
        self.assertEqual(
            self.client.post(ISLANDS_BULK_URL, {"islands": []}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(ISLANDS_BULK_URL, {"domain": self.org.domain}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                ISLANDS_BULK_URL, {"domain": "nope.example.com", "islands": []}, format="json"
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )


class ContentIslandColumnLimitTests(ContentIslandAPITestCase):
    """sqlite does not enforce varchar lengths - assert them here or nowhere."""

    def test_declared_column_limits_match_the_wire_contract(self):
        self.assertEqual(ContentIsland._meta.get_field("slug").max_length, 80)
        self.assertEqual(ContentIsland._meta.get_field("name").max_length, 160)
        self.assertEqual(ContentIsland._meta.get_field("pillar_keyword").max_length, 200)
        self.assertEqual(ContentIsland._meta.get_field("icon_key").max_length, 32)
        self.assertEqual(ContentIsland._meta.get_field("color_key").max_length, 32)
        self.assertEqual(
            ContentIslandRefreshDispatch._meta.get_field("idempotency_key").max_length, 100
        )
        self.assertEqual(
            ContentIslandRefreshDispatch._meta.get_field("content_factory_run_id").max_length, 64
        )

    def test_an_overlong_birth_is_clamped_to_the_column_limits(self):
        response = self.client.post(
            ISLANDS_BULK_URL,
            {
                "domain": self.org.domain,
                "islands": [
                    {
                        "slug": None,
                        "name": "Australian Standards Aligned Arboricultural Documentation " * 6,
                        "pillar_keyword": "australian standards aligned arboricultural documentation " * 8,
                        "centroid_embedding": [1.0, 0.0],
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        island = ContentIsland.objects.get(organization=self.org)
        self.assertLessEqual(len(island.slug), 80)
        self.assertLessEqual(len(island.name), 160)
        self.assertLessEqual(len(island.pillar_keyword), 200)

    def test_the_daily_idempotency_key_fits_the_column(self):
        # "island-refresh:{domain}:{local_date}" - the Phase 4 dispatch key format.
        domain = "australian-standards-aligned-arboricultural-documentation.com.au"
        key = f"island-refresh:{domain}:2026-08-23"
        self.assertLessEqual(len(key), 100)
        dispatch = ContentIslandRefreshDispatch.objects.create(
            organization=self.org,
            local_date=date(2026, 8, 23),
            idempotency_key=key,
        )
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.idempotency_key, key)


class ContentIslandGraphSerializerTests(ContentIslandAPITestCase):
    """The camelCase projection Phase 3 will hang off bootstrap."""

    def _island(self, slug, **kwargs):
        defaults = {
            "organization": self.org,
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "pillar_keyword": slug.replace("-", " "),
            "icon_key": "rocket",
            "color_key": "blue",
            "status": ContentIslandStatus.VISIBLE,
            "keyword_count": 14,
            "total_volume": 5200,
            "avg_difficulty": 28.4,
            "opportunity_score": 9310.2,
            "ai_search_volume": 840,
            "articles_written": 2,
            "centroid_embedding": [1.0, 0.0],
        }
        defaults.update(kwargs)
        return ContentIsland.objects.create(**defaults)

    def test_nodes_are_camel_case_and_edges_join_on_bare_slugs(self):
        from content_factory.serializers import ContentIslandGraphSerializer

        fresh = self._island("ai-startup-fundraising", promoted_at=timezone.now())
        stale = self._island(
            "healthcare-ai",
            promoted_at=timezone.now() - timedelta(days=30),
            centroid_embedding=[0.8, 0.6],
        )
        edge = ContentIslandEdge.objects.create(
            organization=self.org, island_a=fresh, island_b=stale, similarity=0.41
        )

        data = ContentIslandGraphSerializer(
            {
                "updated_at": timezone.now(),
                "emerging_count": 2,
                "islands": [fresh, stale],
                "edges": [edge],
            },
            context={"idea_counts": {"ai-startup-fundraising": 6}},
        ).data

        self.assertEqual(data["emergingCount"], 2)
        self.assertIsNotNone(data["updatedAt"])

        node = data["nodes"][0]
        self.assertEqual(node["id"], "island:ai-startup-fundraising")
        self.assertEqual(node["slug"], "ai-startup-fundraising")
        self.assertEqual(node["pillarKeyword"], "ai startup fundraising")
        self.assertEqual(node["iconKey"], "rocket")
        self.assertEqual(node["colorKey"], "blue")
        self.assertEqual(node["keywordCount"], 14)
        self.assertEqual(node["totalVolume"], 5200)
        self.assertAlmostEqual(node["avgDifficulty"], 28.4)
        self.assertAlmostEqual(node["opportunityScore"], 9310.2)
        self.assertEqual(node["aiSearchVolume"], 840)
        self.assertEqual(node["articlesWritten"], 2)
        self.assertEqual(node["ideaCount"], 6)
        self.assertTrue(node["isNew"])
        self.assertFalse(data["nodes"][1]["isNew"])
        self.assertEqual(data["nodes"][1]["ideaCount"], 0)

        self.assertEqual(
            data["edges"],
            [{"source": "ai-startup-fundraising", "target": "healthcare-ai", "similarity": 0.41}],
        )

    @override_settings(CONTENT_ISLANDS_NEW_BADGE_DAYS=7)
    def test_is_new_tracks_the_badge_window(self):
        from content_factory.serializers import ContentIslandGraphNodeSerializer

        never_promoted = self._island("emerging-one", status=ContentIslandStatus.EMERGING)
        just_outside = self._island("older-one", promoted_at=timezone.now() - timedelta(days=8))
        self.assertFalse(ContentIslandGraphNodeSerializer(never_promoted).data["isNew"])
        self.assertFalse(ContentIslandGraphNodeSerializer(just_outside).data["isNew"])


class ContentIslandSettingsTests(TestCase):
    """The §4 flags table - every knob the later phases read must exist now."""

    def test_all_island_settings_are_declared_with_the_documented_defaults(self):
        self.assertIs(settings.CONTENT_ISLANDS_ENABLED, False)
        self.assertIs(settings.CONTENT_ISLANDS_SCHEDULER_ENABLED, False)
        self.assertEqual(settings.CONTENT_ISLANDS_REFRESH_LOCAL_HOUR, 6)
        self.assertEqual(settings.CONTENT_ISLANDS_REFRESH_MAX_PER_TICK, 3)
        self.assertEqual(settings.CONTENT_ISLANDS_PROMOTION_MIN_KEYWORDS, 5)
        self.assertEqual(settings.CONTENT_ISLANDS_PROMOTION_MIN_VOLUME, 200)
        self.assertEqual(settings.CONTENT_ISLANDS_MAX_VISIBLE, 12)
        self.assertEqual(settings.CONTENT_ISLANDS_ARCHIVE_AFTER_MISSES, 5)
        self.assertEqual(settings.CONTENT_ISLANDS_NEW_BADGE_DAYS, 7)


# =============================================================================
# Phase 1 - pilot/ops command
# =============================================================================

class ManageContentIslandsCommandTests(ContentIslandAPITestCase):
    def setUp(self):
        super().setUp()
        self.island = ContentIsland.objects.create(
            organization=self.org,
            slug="ai-startup-fundraising",
            name="AI Startup Fundraising",
            pillar_keyword="ai startup fundraising",
            status=ContentIslandStatus.ARCHIVED,
            archived_at=timezone.now(),
            consecutive_misses=5,
            last_missed_on=date(2026, 8, 23),
            centroid_embedding=[1.0, 0.0],
        )
        self.neighbour = ContentIsland.objects.create(
            organization=self.org,
            slug="healthcare-ai",
            name="Healthcare AI",
            pillar_keyword="healthcare ai",
            status=ContentIslandStatus.VISIBLE,
            centroid_embedding=[0.8, 0.6],
        )

    def _call(self, *args):
        out = StringIO()
        call_command("manage_content_islands", *args, stdout=out)
        return out.getvalue()

    def test_list_reports_islands_and_edges(self):
        output = self._call("--domain", self.org.domain, "--list")
        self.assertIn("ai-startup-fundraising", output)
        self.assertIn("healthcare-ai", output)
        self.assertIn("2 island(s)", output)

    def test_restore_brings_an_archived_island_back_and_resets_misses(self):
        output = self._call("--domain", self.org.domain, "--restore-island", "ai-startup-fundraising")
        self.assertIn("Restored ai-startup-fundraising", output)

        self.island.refresh_from_db()
        self.assertEqual(self.island.status, ContentIslandStatus.EMERGING)
        self.assertEqual(self.island.consecutive_misses, 0)
        self.assertIsNone(self.island.last_missed_on)
        self.assertIsNone(self.island.archived_at)
        # Back on the map means back in the edge set.
        self.assertEqual(ContentIslandEdge.objects.filter(organization=self.org).count(), 1)

    def test_archive_removes_an_island_and_its_edges(self):
        self._call("--domain", self.org.domain, "--restore-island", "ai-startup-fundraising")
        self.assertEqual(ContentIslandEdge.objects.filter(organization=self.org).count(), 1)

        output = self._call("--domain", self.org.domain, "--archive-island", "ai-startup-fundraising")
        self.assertIn("Archived ai-startup-fundraising", output)
        self.island.refresh_from_db()
        self.assertEqual(self.island.status, ContentIslandStatus.ARCHIVED)
        self.assertIsNotNone(self.island.archived_at)
        self.assertEqual(ContentIslandEdge.objects.filter(organization=self.org).count(), 0)

    def test_unknown_domain_or_slug_fails_loudly(self):
        with self.assertRaises(CommandError):
            self._call("--domain", "nope.example.com", "--list")
        with self.assertRaises(CommandError):
            self._call("--domain", self.org.domain, "--archive-island", "not-an-island")
