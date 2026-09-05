"""Cross-repo contract: the exact payload content-factory builds must satisfy this API.

The fixture body is captured verbatim from content-factory's own
``island_synthesis.synthesize_islands(...).to_wire()`` (deterministic fake embedder and
namer). It is deliberately a literal capture rather than something rebuilt from these
models: if content-factory changes its wire shape, this test is what fails instead of
production silently syncing nothing.
"""
import json
from pathlib import Path

from content_factory.models import (
    ContentIsland,
    ContentIslandEdge,
    ContentIslandSnapshot,
)
from organizations.models import Organization

from .test_content_islands import ContentIslandAPITestCase

CF_PAYLOAD = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "content_factory_island_sync.json").read_text()
)


class ContentFactoryIslandSyncContractTests(ContentIslandAPITestCase):
    """Replays a real content-factory payload through the real bulk endpoint."""

    def setUp(self):
        super().setUp()
        self.cf_org = Organization.objects.create(
            name="Contract Check",
            domain=CF_PAYLOAD["domain"],
        )

    def post_cf_payload(self):
        return self.client.post(
            "/api/seo/islands/bulk/",
            data=json.dumps(CF_PAYLOAD),
            content_type="application/json",
        )

    def seed_island(self, **overrides):
        defaults = {
            "organization": self.cf_org,
            "slug": "seeded-island",
            "name": "Seeded Island",
            "pillar_keyword": "fundraising seed round",
            "icon_key": "rocket",
            "color_key": "blue",
            "status": "visible",
            "origin": "pillar_strategy_seed",
            "centroid_embedding": [],
        }
        defaults.update(overrides)
        return ContentIsland.objects.create(**defaults)

    def test_content_factory_payload_is_accepted(self):
        # content-factory only names an existing slug it just read back from
        # GET /api/seo/islands/, so the realistic starting state has that island stored.
        self.seed_island()

        response = self.post_cf_payload()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["created"], 1)

        born = ContentIsland.objects.get(organization=self.cf_org, origin="cluster_birth")
        self.assertTrue(born.slug)
        self.assertEqual(born.status, "emerging")
        self.assertTrue(born.centroid_embedding)
        self.assertEqual(born.icon_key, "chart")
        self.assertEqual(born.keyword_count, 3)

    def test_unknown_slug_from_content_factory_is_created_rather_than_dropped(self):
        response = self.post_cf_payload()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["created"], 2)
        self.assertTrue(
            ContentIsland.objects.filter(organization=self.cf_org, slug="seeded-island").exists()
        )

    def test_matched_seed_island_keeps_identity_and_gains_a_centroid(self):
        seeded = self.seed_island()

        response = self.post_cf_payload()
        self.assertEqual(response.status_code, 200, response.content)

        seeded.refresh_from_db()
        self.assertEqual(seeded.slug, "seeded-island")
        self.assertEqual(seeded.icon_key, "rocket")
        self.assertEqual(seeded.color_key, "blue")
        self.assertTrue(seeded.centroid_embedding)
        self.assertEqual(seeded.consecutive_misses, 0)
        self.assertEqual(seeded.keyword_count, 3)

    def test_expanded_slugs_key_from_content_factory_is_honoured(self):
        self.assertIn("expanded_slugs", CF_PAYLOAD)
        seeded = self.seed_island()

        response = self.post_cf_payload()
        self.assertEqual(response.status_code, 200, response.content)

        seeded.refresh_from_db()
        self.assertIsNotNone(seeded.last_expanded_on)
        self.assertEqual(seeded.last_expanded_on.isoformat(), CF_PAYLOAD["captured_on"])

    def test_edges_and_snapshots_are_derived_server_side(self):
        self.assertNotIn("edges", CF_PAYLOAD)
        self.seed_island()

        response = self.post_cf_payload()
        self.assertEqual(response.status_code, 200, response.content)

        islands = ContentIsland.objects.filter(organization=self.cf_org)
        self.assertEqual(
            ContentIslandSnapshot.objects.filter(
                island__organization=self.cf_org,
                captured_on=CF_PAYLOAD["captured_on"],
            ).count(),
            islands.count(),
        )
        self.assertEqual(
            ContentIslandEdge.objects.filter(organization=self.cf_org).count(),
            response.json()["edges"],
        )

    def test_islands_absent_from_the_payload_take_a_miss(self):
        stale = self.seed_island(
            slug="stale-island",
            name="Stale Island",
            pillar_keyword="obsolete theme",
            centroid_embedding=[0.9, -0.1, 0.2, 0.1, 0.0, 0.0, 0.0, 0.0],
        )

        response = self.post_cf_payload()
        self.assertEqual(response.status_code, 200, response.content)

        stale.refresh_from_db()
        self.assertEqual(stale.consecutive_misses, 1)
        self.assertEqual(stale.status, "visible")
