"""Real persistence/payment integration checks, included in the canonical CI suite."""
from unittest.mock import patch

from django.test import override_settings
from content_factory.island_research import refund_empty_or_failed_research
from content_factory.models import ContentIsland, ContentIslandKeyword, ContentIslandSnapshot
from content_factory.vibe_marketing_views import _serialize_run, _topic_pillars_for_bootstrap
from roo.models import Ledger, PointsAccount
from workflow_runs.models import ContentFactoryRun
from tests.test_content_island_bootstrap import ContentIslandBootstrapTestCase

URL = "/api/v1/vibe-marketing/islands/research"
BRIEF = {"subject": "AI integration for business workflows", "searchIntent": "informational", "clientRequestId": "research-contract-request-1"}
PROPOSAL = {"id": "measured-proposal", "name": "AI Workflow Integration", "description": "Integrating AI into business workflows.",
    "pillar_keyword": "ai workflow integration", "icon_key": "tools", "color_key": "purple", "centroid_embedding": [1., 0.],
    "metrics": {"keyword_count": 3, "total_volume": 600, "avg_difficulty": 20., "opportunity_score": 60., "ai_search_volume": 0},
    "keywords": [{"keyword": keyword, "volume": volume, "difficulty": 20, "intent": "informational", "opportunity_index": volume / 10}
        for keyword, volume in [("ai workflow integration", 300), ("ai integration examples", 200), ("business ai workflows", 100)]],
    "members": [{"keyword_normalized": keyword, "similarity_score": .9, "is_centroid": i == 0}
        for i, keyword in enumerate(["ai workflow integration", "ai integration examples", "business ai workflows"])]}


@override_settings(CONTENT_ISLANDS_ENABLED=True)
class IslandResearchContractTests(ContentIslandBootstrapTestCase):
    def start(self):
        def dispatch(**kwargs):
            payload = kwargs["payload"]
            return ContentFactoryRun.objects.create(run_id=payload["client_request_id"], domain=self.organization.domain,
                workflow="island_refresh", status="queued", run_request=payload)
        with patch("content_factory.vibe_marketing_views._queue_content_factory_run", side_effect=dispatch) as queue:
            first = self.client.post(URL, BRIEF, format="json")
            second = self.client.post(URL, BRIEF, format="json")
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(second.data["runId"], first.data["runId"])
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 19)
        return ContentFactoryRun.objects.get(run_id=first.data["runId"])

    def complete(self, run, proposals):
        run.status = "completed"
        run.result = {"island_research": True, "suggested_islands": proposals, "source": "DataForSEO"}
        run.save(update_fields=["status", "result"])

    def test_payment_idempotency_and_empty_result_refund(self):
        run = self.start()
        self.assertEqual(ContentIsland.objects.filter(organization=self.organization).count(), 0)
        self.complete(run, [])
        refund_empty_or_failed_research(run)
        refund_empty_or_failed_research(run)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 20)
        self.assertEqual(Ledger.objects.filter(user=self.user, idempotency_key__startswith="content_factory:topic_generation:refund:").count(), 1)

    def test_adoption_uses_researched_metrics_is_idempotent_and_preserves_existing_islands(self):
        untouched = ContentIsland.objects.create(organization=self.organization, slug="existing", name="Existing island",
            pillar_keyword="unrelated history", status="visible", centroid_embedding=[0., 1.], keyword_count=7, total_volume=900)
        run = self.start()
        self.complete(run, [PROPOSAL])
        payload = {"proposalId": PROPOSAL["id"], "name": "Forged name", "metrics": {"total_volume": 999999}}
        first = self.client.post(f"{URL}/{run.run_id}/adopt", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post(f"{URL}/{run.run_id}/adopt", payload, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["island"]["slug"], second.data["island"]["slug"])
        island = ContentIsland.objects.get(organization=self.organization, slug=first.data["island"]["slug"])
        self.assertEqual(island.name, PROPOSAL["name"])
        self.assertEqual(island.total_volume, 600)
        self.assertEqual(island.status, "visible")
        self.assertEqual(ContentIslandKeyword.objects.filter(island=island).count(), 3)
        self.assertEqual(ContentIslandSnapshot.objects.filter(island=island).count(), 1)
        self.assertIn(BRIEF["subject"], island.description)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 19)
        untouched.refresh_from_db()
        self.assertEqual(untouched.total_volume, 900)
        self.assertEqual(untouched.status, "visible")
        self.assertEqual(untouched.consecutive_misses, 0)
        pillars = _topic_pillars_for_bootstrap(self.organization, self.config, compact=True)
        self.assertIn(island.slug, [item["slug"] for item in pillars])
        compact = _serialize_run(run, mode="status")
        self.assertEqual(compact["result"]["suggested_islands"][0]["metrics"]["total_volume"], 600)
        self.assertNotIn("centroid_embedding", compact["result"]["suggested_islands"][0])

    def test_unfinished_and_foreign_runs_cannot_be_adopted(self):
        run = self.start()
        response = self.client.post(f"{URL}/{run.run_id}/adopt", {"proposalId": PROPOSAL["id"]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.complete(run, [PROPOSAL])
        run.domain = "another-company.test"
        run.save(update_fields=["domain"])
        response = self.client.post(f"{URL}/{run.run_id}/adopt", {"proposalId": PROPOSAL["id"]}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ContentIsland.objects.count(), 0)
