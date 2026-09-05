"""Phase 3: serving content islands and the island graph through the bootstrap.

The load-bearing invariant here is D6 - every islandGraph node's slug must also be
a topicPillars slug, because the discovery route action resolves a clicked node by
slug against topicPillars and errors when it is absent. A zero-candidate island is
therefore emitted, unlike the cluster path which skips empty clusters.
"""
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import (
    ClusterMembership,
    ContentIsland,
    ContentIslandEdge,
    ContentIslandKeyword,
    ContentIslandStatus,
    OrganizationContentConfig,
    ResearchedKeyword,
    SemanticCluster,
)
from content_factory.vibe_marketing_views import (
    _apply_content_island_metadata,
    _balanced_topic_candidate_order,
    _bootstrap_state_fingerprint,
    _content_island_metadata_from_mapping,
    _serialize_run,
    _topic_pillars_for_bootstrap,
)
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from roo.models import PointsAccount
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()

BOOTSTRAP_URL = "/api/v1/vibe-marketing/bootstrap/"


class ContentIslandBootstrapTestCase(TestCase):
    """Founder dashboard fixture: authenticated profile + company bound to an org."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
        )
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER
        )
        self.organization = Organization.objects.create(name="Acme", domain="acme.com")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            name="Acme",
            domain="acme.com",
            registered=True,
            organization=self.organization,
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company", "updated_at"])
        PointsAccount.objects.update_or_create(
            user=self.user, defaults={"balance": 20, "earned_balance": 20}
        )
        self.client.force_authenticate(user=self.user)
        self.config = OrganizationContentConfig.objects.create(organization=self.organization)

    def bootstrap(self, view=None):
        # The 20s bootstrap cache would otherwise hide every mutation these tests make.
        url = BOOTSTRAP_URL if view is None else f"{BOOTSTRAP_URL}?view={view}"
        with patch.dict(os.environ, {"VIBE_BOOTSTRAP_CACHE_SECONDS": "0"}):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.data

    def make_keyword(self, keyword, **kwargs):
        defaults = {"volume": 400, "difficulty": 30, "opportunity_index": 120.0}
        defaults.update(kwargs)
        return ResearchedKeyword.objects.create(
            organization=self.organization,
            keyword=keyword,
            keyword_normalized=keyword.lower().strip(),
            **defaults,
        )

    def make_island(self, slug, *, keywords=(), **kwargs):
        defaults = {
            "organization": self.organization,
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "description": "",
            "pillar_keyword": slug.replace("-", " "),
            "icon_key": "rocket",
            "color_key": "teal",
            "status": ContentIslandStatus.VISIBLE,
            "keyword_count": len(keywords),
            "total_volume": 4000,
            "avg_difficulty": 30.0,
            "opportunity_score": 900.0,
            "ai_search_volume": 220,
            "articles_written": 1,
            "centroid_embedding": [1.0, 0.0],
            "promoted_at": timezone.now(),
        }
        defaults.update(kwargs)
        island = ContentIsland.objects.create(**defaults)
        for keyword in keywords:
            ContentIslandKeyword.objects.create(
                island=island, keyword=self.make_keyword(keyword), similarity_score=0.82
            )
        return island

    def make_cluster(self, pillar_keyword, members, *, cluster_id=1, total_volume=900):
        cluster = SemanticCluster.objects.create(
            organization=self.organization,
            cluster_id=cluster_id,
            pillar_keyword=pillar_keyword,
            total_volume=total_volume,
        )
        for member in members:
            ClusterMembership.objects.create(
                keyword=self.make_keyword(member), cluster=cluster, is_pillar=False, similarity_score=0.8
            )
        return cluster

    def set_strategy(self, pillars):
        self.config.pillar_strategy = {"pillars": pillars}
        self.config.save(update_fields=["pillar_strategy", "updated_at"])


# =============================================================================
# Pillar source precedence
# =============================================================================

@override_settings(CONTENT_ISLANDS_ENABLED=True)
class TopicPillarSourcePrecedenceTests(ContentIslandBootstrapTestCase):
    def test_islands_win_over_clusters_and_strategy(self):
        self.set_strategy([{"name": "Strategy Pillar", "keyword": "strategy pillar", "topics": ["a"]}])
        self.make_cluster("cluster pillar", ["cluster member"])
        self.make_island("ai-startup-fundraising", keywords=["seed round checklist"])

        pillars = _topic_pillars_for_bootstrap(self.organization, self.config)

        self.assertEqual([pillar["source"] for pillar in pillars], ["content_island"])
        self.assertEqual(pillars[0]["slug"], "ai-startup-fundraising")
        self.assertEqual(pillars[0]["id"], "island:ai-startup-fundraising")

    def test_clusters_still_win_when_the_org_has_no_visible_islands(self):
        self.set_strategy([{"name": "Strategy Pillar", "keyword": "strategy pillar", "topics": ["a"]}])
        self.make_cluster("cluster pillar", ["cluster member"])
        self.make_island("emerging-only", status=ContentIslandStatus.EMERGING, promoted_at=None)
        self.make_island("archived-only", status=ContentIslandStatus.ARCHIVED)

        pillars = _topic_pillars_for_bootstrap(self.organization, self.config)

        self.assertEqual([pillar["source"] for pillar in pillars], ["semantic_cluster"])

    def test_islands_carry_the_persisted_visuals_and_pillar_keyword(self):
        self.make_island(
            "healthcare-ai",
            keywords=["hospital ai rollout"],
            icon_key="shield",
            color_key="lime",
            pillar_keyword="healthcare ai adoption",
            description="Clinical AI adoption.",
        )

        pillar = _topic_pillars_for_bootstrap(self.organization, self.config)[0]

        self.assertEqual(pillar["iconKey"], "shield")
        self.assertEqual(pillar["colorKey"], "lime")
        self.assertEqual(pillar["pillarKeyword"], "healthcare ai adoption")
        self.assertEqual(pillar["description"], "Clinical AI adoption.")
        self.assertEqual(pillar["topicCandidates"][0]["pillarSlug"], "healthcare-ai")
        self.assertEqual(pillar["topicCandidates"][0]["pillarName"], "Healthcare Ai")
        self.assertEqual(pillar["topicCandidates"][0]["pillarKeyword"], "healthcare ai adoption")

    def test_islands_are_ordered_by_opportunity_score(self):
        self.make_island("low-opportunity", opportunity_score=10.0)
        self.make_island("high-opportunity", opportunity_score=9000.0)

        pillars = _topic_pillars_for_bootstrap(self.organization, self.config)

        self.assertEqual([pillar["slug"] for pillar in pillars], ["high-opportunity", "low-opportunity"])

    def test_declined_and_written_keywords_are_filtered_out_of_island_candidates(self):
        island = self.make_island("ai-startup-fundraising")
        keyword = self.make_keyword("seed round checklist")
        written = self.make_keyword("already written topic", status="written")
        ContentIslandKeyword.objects.create(island=island, keyword=keyword)
        ContentIslandKeyword.objects.create(island=island, keyword=written)

        pillars = _topic_pillars_for_bootstrap(
            self.organization, self.config, declined_keyword_keys={"seed round checklist"}
        )

        self.assertEqual(pillars[0]["topicCandidates"], [])
        self.assertEqual(pillars[0]["ideaCount"], 0)

    def test_compact_view_caps_candidates_at_eight(self):
        self.make_island(
            "ai-startup-fundraising",
            keywords=[f"fundraising topic {index}" for index in range(12)],
        )

        compact = _topic_pillars_for_bootstrap(self.organization, self.config, compact=True)
        full = _topic_pillars_for_bootstrap(self.organization, self.config, compact=False)

        self.assertEqual(len(compact[0]["topicCandidates"]), 8)
        self.assertEqual(len(full[0]["topicCandidates"]), 12)
        self.assertEqual(compact[0]["ideaCount"], 12)


# =============================================================================
# D6: zero-candidate islands and the node/pillar invariant
# =============================================================================

@override_settings(CONTENT_ISLANDS_ENABLED=True)
class IslandGraphInvariantTests(ContentIslandBootstrapTestCase):
    def test_a_visible_island_with_zero_candidates_is_still_a_topic_pillar(self):
        self.make_island("brand-new-island")

        pillars = _topic_pillars_for_bootstrap(self.organization, self.config)

        self.assertEqual([pillar["slug"] for pillar in pillars], ["brand-new-island"])
        self.assertEqual(pillars[0]["topicCandidates"], [])
        self.assertEqual(pillars[0]["ideaCount"], 0)
        self.assertTrue(pillars[0]["description"])

    def test_every_island_graph_node_slug_is_also_a_topic_pillar_slug(self):
        self.make_island("ai-startup-fundraising", keywords=["seed round checklist"])
        self.make_island("brand-new-island", opportunity_score=5.0)
        self.make_island("emerging-island", status=ContentIslandStatus.EMERGING, promoted_at=None)

        for view in (None, "summary"):
            payload = self.bootstrap(view)
            node_slugs = {node["slug"] for node in payload["islandGraph"]["nodes"]}
            pillar_slugs = {pillar["slug"] for pillar in payload["topicPillars"]}
            self.assertTrue(node_slugs)
            self.assertTrue(node_slugs <= pillar_slugs, f"{view}: {node_slugs - pillar_slugs}")
            self.assertNotIn("emerging-island", node_slugs)

    def test_node_idea_count_agrees_with_the_pillar_it_came_from(self):
        self.make_island("ai-startup-fundraising", keywords=["seed round checklist", "ai investor update"])
        self.make_island("brand-new-island", opportunity_score=5.0)

        payload = self.bootstrap("summary")

        idea_counts = {pillar["slug"]: pillar["ideaCount"] for pillar in payload["topicPillars"]}
        for node in payload["islandGraph"]["nodes"]:
            self.assertEqual(node["ideaCount"], idea_counts[node["slug"]])
        self.assertEqual(idea_counts["ai-startup-fundraising"], 2)
        self.assertEqual(idea_counts["brand-new-island"], 0)


# =============================================================================
# islandGraph payload shape
# =============================================================================

@override_settings(CONTENT_ISLANDS_ENABLED=True, CONTENT_ISLANDS_NEW_BADGE_DAYS=7)
class IslandGraphPayloadTests(ContentIslandBootstrapTestCase):
    def test_graph_is_present_in_both_the_summary_and_the_full_view(self):
        self.make_island("ai-startup-fundraising", keywords=["seed round checklist"])

        for view in (None, "summary"):
            payload = self.bootstrap(view)
            graph = payload["islandGraph"]
            self.assertEqual([node["slug"] for node in graph["nodes"]], ["ai-startup-fundraising"])
            node = graph["nodes"][0]
            self.assertEqual(node["id"], "island:ai-startup-fundraising")
            self.assertEqual(node["pillarKeyword"], "ai startup fundraising")
            self.assertEqual(node["iconKey"], "rocket")
            self.assertEqual(node["colorKey"], "teal")
            self.assertEqual(node["status"], ContentIslandStatus.VISIBLE)
            self.assertEqual(node["keywordCount"], 1)
            self.assertEqual(node["totalVolume"], 4000)
            self.assertEqual(node["aiSearchVolume"], 220)
            self.assertEqual(node["articlesWritten"], 1)
            self.assertIsNotNone(graph["updatedAt"])
            # Lean payload: no embeddings, no snapshots, no candidates.
            self.assertNotIn("centroidEmbedding", node)
            self.assertNotIn("centroid_embedding", node)
            self.assertNotIn("topicCandidates", node)

    def test_edges_use_bare_slugs_never_the_prefixed_node_id(self):
        first = self.make_island("ai-startup-fundraising")
        second = self.make_island("healthcare-ai", opportunity_score=100.0)
        ContentIslandEdge.objects.create(
            organization=self.organization, island_a=first, island_b=second, similarity=0.41
        )

        graph = self.bootstrap("summary")["islandGraph"]

        self.assertEqual(
            graph["edges"],
            [{"source": "ai-startup-fundraising", "target": "healthcare-ai", "similarity": 0.41}],
        )

    def test_edges_touching_a_hidden_island_are_dropped(self):
        visible = self.make_island("ai-startup-fundraising")
        emerging = self.make_island(
            "emerging-island", status=ContentIslandStatus.EMERGING, promoted_at=None
        )
        ContentIslandEdge.objects.create(
            organization=self.organization, island_a=emerging, island_b=visible, similarity=0.55
        )

        graph = self.bootstrap("summary")["islandGraph"]

        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["emergingCount"], 1)

    def test_is_new_tracks_the_badge_window(self):
        self.make_island("fresh-island", promoted_at=timezone.now() - timedelta(days=1))
        self.make_island(
            "old-island", opportunity_score=5.0, promoted_at=timezone.now() - timedelta(days=8)
        )

        nodes = {node["slug"]: node for node in self.bootstrap("summary")["islandGraph"]["nodes"]}

        self.assertTrue(nodes["fresh-island"]["isNew"])
        self.assertFalse(nodes["old-island"]["isNew"])

    def test_an_org_with_no_visible_islands_gets_no_graph_key(self):
        self.make_island("emerging-island", status=ContentIslandStatus.EMERGING, promoted_at=None)
        self.make_cluster("cluster pillar", ["cluster member"])

        payload = self.bootstrap("summary")

        self.assertNotIn("islandGraph", payload)
        self.assertEqual([pillar["source"] for pillar in payload["topicPillars"]], ["semantic_cluster"])


# =============================================================================
# Flag off: the legacy payload must be byte-identical
# =============================================================================

class IslandFlagOffLegacyPayloadTests(ContentIslandBootstrapTestCase):
    def _payload_with_and_without_islands(self):
        with_islands = self.bootstrap("summary")
        ContentIslandEdge.objects.filter(organization=self.organization).delete()
        ContentIslandKeyword.objects.filter(island__organization=self.organization).delete()
        ContentIsland.objects.filter(organization=self.organization).delete()
        without_islands = self.bootstrap("summary")
        return with_islands, without_islands

    @override_settings(CONTENT_ISLANDS_ENABLED=False)
    def test_cluster_backed_org_is_unchanged_by_the_presence_of_islands(self):
        self.make_cluster("cluster pillar", ["cluster member"])
        first = self.make_island("ai-startup-fundraising", keywords=["seed round checklist"])
        second = self.make_island("healthcare-ai", opportunity_score=100.0)
        ContentIslandEdge.objects.create(
            organization=self.organization, island_a=first, island_b=second, similarity=0.5
        )

        with_islands, without_islands = self._payload_with_and_without_islands()

        self.assertNotIn("islandGraph", with_islands)
        self.assertEqual(with_islands, without_islands)
        self.assertEqual([pillar["source"] for pillar in with_islands["topicPillars"]], ["semantic_cluster"])

    @override_settings(CONTENT_ISLANDS_ENABLED=False)
    def test_strategy_only_org_is_unchanged_by_the_presence_of_islands(self):
        self.set_strategy(
            [
                {
                    "name": "AI Startup Fundraising",
                    "keyword": "ai startup fundraising",
                    "description": "Raising money as an AI founder.",
                    "topics": ["seed round checklist"],
                }
            ]
        )
        self.make_island("ai-startup-fundraising", keywords=["seed round checklist"])

        with_islands, without_islands = self._payload_with_and_without_islands()

        self.assertNotIn("islandGraph", with_islands)
        self.assertEqual(with_islands, without_islands)
        self.assertEqual([pillar["source"] for pillar in with_islands["topicPillars"]], ["pillar_strategy"])


# =============================================================================
# Bootstrap freshness: the daily refresh writes no keywords at all
# =============================================================================

class IslandBootstrapFingerprintTests(ContentIslandBootstrapTestCase):
    def _fingerprint(self):
        return _bootstrap_state_fingerprint(self.organization, self.company, self.config)

    def _keyword_state(self):
        return list(
            ResearchedKeyword.objects.filter(organization=self.organization)
            .order_by("id")
            .values_list("id", "metrics_updated_at")
        )

    @override_settings(CONTENT_ISLANDS_ENABLED=True)
    def test_a_keyword_free_island_mutation_shifts_the_fingerprint(self):
        island = self.make_island("ai-startup-fundraising")
        keyword_state = self._keyword_state()
        before = self._fingerprint()

        island.total_volume = 99999
        island.opportunity_score = 12345.0
        island.save()

        self.assertNotEqual(self._fingerprint(), before)
        # A daily refresh is exactly this shape: island rows move, keyword rows do not.
        self.assertEqual(self._keyword_state(), keyword_state)

    @override_settings(CONTENT_ISLANDS_ENABLED=True)
    def test_a_new_island_shifts_the_fingerprint(self):
        before = self._fingerprint()
        self.make_island("brand-new-island")
        self.assertNotEqual(self._fingerprint(), before)

    @override_settings(CONTENT_ISLANDS_ENABLED=True)
    def test_membership_only_changes_shift_the_fingerprint(self):
        island = self.make_island("ai-startup-fundraising")
        keyword = self.make_keyword("seed round checklist")
        before = self._fingerprint()

        ContentIslandKeyword.objects.create(island=island, keyword=keyword, similarity_score=0.9)

        self.assertNotEqual(self._fingerprint(), before)

    @override_settings(CONTENT_ISLANDS_ENABLED=False)
    def test_the_legacy_fingerprint_terms_see_nothing_when_the_flag_is_off(self):
        island = self.make_island("ai-startup-fundraising")
        before = self._fingerprint()

        island.total_volume = 99999
        island.save()

        self.assertEqual(self._fingerprint(), before)


# =============================================================================
# Discovery dispatch round trip + run recovery
# =============================================================================

class IslandDispatchMetadataRoundTripTests(ContentIslandBootstrapTestCase):
    def _island_run(self, run_id="island-discovery-1"):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="auto_discovery",
            domain=self.organization.domain,
            status=ContentFactoryRunStatus.RUNNING,
            current_step="research",
            run_request={
                "domain": self.organization.domain,
                "content_island_slug": "ai-startup-fundraising",
                "content_island_name": "AI Startup Fundraising",
                "content_island_keyword": "ai startup fundraising",
                "content_island_icon_key": "rocket",
                "content_island_color_key": "teal",
            },
        )

    @override_settings(CONTENT_ISLANDS_ENABLED=True)
    def test_island_derived_slugs_round_trip_through_the_candidate_metadata_helpers(self):
        self.make_island("ai-startup-fundraising", keywords=["seed round checklist"])
        pillars = _topic_pillars_for_bootstrap(self.organization, self.config)
        run = self._island_run()

        metadata = _content_island_metadata_from_mapping(run.run_request)
        self.assertEqual(metadata["pillarSlug"], pillars[0]["slug"])
        self.assertEqual(metadata["pillarKeyword"], pillars[0]["pillarKeyword"])

        candidate = _apply_content_island_metadata(
            {"id": "topic:1", "keyword": "seed round checklist", "opportunityScore": 120, "volume": 400},
            metadata,
        )
        self.assertEqual(candidate["pillarSlug"], "ai-startup-fundraising")
        self.assertEqual(candidate["pillarName"], "AI Startup Fundraising")
        self.assertEqual(candidate["pillarIconKey"], "rocket")
        self.assertEqual(candidate["pillarColorKey"], "teal")

    @override_settings(CONTENT_ISLANDS_ENABLED=True)
    def test_balanced_ordering_interleaves_island_derived_slugs(self):
        self.make_island("ai-startup-fundraising", opportunity_score=9000.0)
        self.make_island("healthcare-ai", opportunity_score=10.0)
        pillars = _topic_pillars_for_bootstrap(self.organization, self.config)
        island_order = {pillar["slug"]: index for index, pillar in enumerate(pillars)}
        candidates = [
            {"keyword": "fundraising a", "pillarSlug": "ai-startup-fundraising", "opportunityScore": 90},
            {"keyword": "fundraising b", "pillarSlug": "ai-startup-fundraising", "opportunityScore": 80},
            {"keyword": "healthcare a", "pillarSlug": "healthcare-ai", "opportunityScore": 70},
        ]

        ordered = _balanced_topic_candidate_order(candidates, island_order)

        self.assertEqual(
            [candidate["keyword"] for candidate in ordered],
            ["fundraising a", "healthcare a", "fundraising b"],
        )

    def test_serialize_run_status_view_carries_the_island_quintet(self):
        run = self._island_run()

        payload = _serialize_run(run, mode="status")

        self.assertEqual(
            payload["contentIsland"],
            {
                "slug": "ai-startup-fundraising",
                "name": "AI Startup Fundraising",
                "keyword": "ai startup fundraising",
                "iconKey": "rocket",
                "icon_key": "rocket",
                "colorKey": "teal",
                "color_key": "teal",
            },
        )
        self.assertEqual(payload["content_island"], payload["contentIsland"])
        self.assertEqual(_serialize_run(run, mode="full")["contentIsland"], payload["contentIsland"])

    def test_a_non_island_run_gains_no_island_key(self):
        run = ContentFactoryRun.objects.create(
            run_id="plain-discovery-1",
            workflow="auto_discovery",
            domain=self.organization.domain,
            status=ContentFactoryRunStatus.RUNNING,
            current_step="research",
            run_request={"domain": self.organization.domain},
        )

        self.assertNotIn("contentIsland", _serialize_run(run, mode="status"))
        self.assertNotIn("content_island", _serialize_run(run, mode="full"))
