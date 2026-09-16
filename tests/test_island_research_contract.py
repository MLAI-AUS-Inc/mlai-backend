"""Real persistence/payment integration checks, included in the canonical CI suite."""
from unittest.mock import patch

from django.test import override_settings
from content_factory.island_research import refund_empty_or_failed_research
from content_factory.models import ContentIsland, ContentIslandKeyword, ContentIslandSnapshot
from content_factory.vibe_marketing_views import _serialize_run, _topic_pillars_for_bootstrap
from organizations.models import Organization
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
    def test_small_measured_island_can_be_adopted_without_another_charge(self):
        run = self.start()
        proposal = {**PROPOSAL, "keywords": PROPOSAL["keywords"][:1], "members": PROPOSAL["members"][:1],
                    "limited_data": True, "metrics": {**PROPOSAL["metrics"], "keyword_count": 1,
                    "total_volume": 300, "opportunity_score": 30.}}
        self.complete(run, [proposal])
        response = self.client.post(f"{URL}/{run.run_id}/adopt", {"proposalId": proposal["id"]}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        island = ContentIsland.objects.get(organization=self.organization, slug=response.data["island"]["slug"])
        self.assertEqual(island.keyword_count, 1)
        self.assertEqual(island.total_volume, 300)
        self.assertEqual(ContentIslandKeyword.objects.filter(island=island).count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 19)

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
        # Run ownership uses the organization FK, not a mutable domain label.
        run.organization = Organization.objects.create(name="Other company", domain="another-company.test")
        run.domain = run.organization.domain
        run.save(update_fields=["organization", "domain"])
        response = self.client.post(f"{URL}/{run.run_id}/adopt", {"proposalId": PROPOSAL["id"]}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ContentIsland.objects.count(), 0)

    def test_batch_preview_atomic_adoption_and_repeat_save_are_free(self):
        from copy import deepcopy
        from content_factory.island_selection import STATE_KEY, dynamic_scopes
        run = self.start()
        close = deepcopy(PROPOSAL)
        close.update(id="close", name="AI adoption services", pillar_keyword="ai adoption services", centroid_embedding=[.99, .01])
        close['keywords'][0]['keyword'] = close['members'][0]['keyword_normalized'] = close['pillar_keyword']
        distant = deepcopy(PROPOSAL)
        distant.update(id="distant", name="Home composting", pillar_keyword="home composting", centroid_embedding=[0., 1.])
        for index, row in enumerate(distant['keywords']):
            row['keyword'] = distant['members'][index]['keyword_normalized'] = f'composting {index}'
        self.complete(run, [PROPOSAL, close, distant])
        url = f"{URL}/{run.run_id}/adopt"
        ids = [PROPOSAL['id'], close['id'], distant['id']]
        preview = self.client.post(url, {'proposalIds': ids, 'preview': True}, format='json')
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertEqual(len(preview.data['groups']), 2)
        self.assertFalse(ContentIsland.objects.exists())
        invalid = self.client.post(url, {'proposalIds': [ids[0], 'forged']}, format='json')
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(ContentIsland.objects.exists())
        first = self.client.post(url, {'proposalIds': ids}, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        second = self.client.post(url, {'proposalIds': ids[::-1]}, format='json')
        self.assertEqual(first.data, second.data)
        self.assertEqual(len(first.data['islands']), 2)
        run.refresh_from_db()
        self.assertEqual(sorted(run.result[STATE_KEY]['selected_ids']), sorted(ids))
        self.assertEqual(len(dynamic_scopes(self.organization)), 1)
        self.assertEqual(run.result[STATE_KEY]['revision'], 1)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 19)
        self.assertEqual(ContentIslandKeyword.objects.filter(island__slug__in=run.result[STATE_KEY]['managed_slugs']).count(), 7)

    def test_daily_merge_split_preserve_records_and_reject_stale_refresh(self):
        from copy import deepcopy
        from datetime import date
        from django.db import transaction
        from django.utils import timezone
        from content_factory.island_selection import STATE_KEY, apply_evolution, dynamic_scopes
        from content_factory.custom_islands import resolve_island_discovery_scope
        run = self.start()
        other = deepcopy(PROPOSAL)
        other.update(id='second', name='AI implementation', pillar_keyword='ai implementation', centroid_embedding=[0., 1.])
        for index, row in enumerate(other['keywords']):
            row['keyword'] = other['members'][index]['keyword_normalized'] = f'ai implementation {index}'
        self.complete(run, [PROPOSAL, other])
        response = self.client.post(f'{URL}/{run.run_id}/adopt', {'proposalIds': [PROPOSAL['id'], 'second']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        slugs = {i['slug'] for i in response.data['islands']}
        original_members = ContentIslandKeyword.objects.count()
        from content_factory.models import ResearchedKeyword, WrittenArticle
        article = WrittenArticle.objects.create(organization=self.organization, title='AI guide',
            slug='ai-guide', category='AI', primary_keyword=PROPOSAL['pillar_keyword'])
        keyword = ResearchedKeyword.objects.get(organization=self.organization, keyword_normalized=PROPOSAL['pillar_keyword'])
        keyword.written_article, keyword.status = article, 'written'
        keyword.save(update_fields=['written_article', 'status'])
        def proposal(parts):
            scope = dynamic_scopes(self.organization)[0]
            return [{'run_id': run.run_id, 'revision': scope['revision'], 'groups': parts}]
        def group(parts):
            return {'name': 'Evolving theme', 'description': 'Measured theme', 'pillar_keyword': parts[0]['pillar_keyword'],
                'keywords': [k['keyword'] for p in parts for k in p['keywords']],
                'members': [k for p in parts for k in p['members']], 'centroid_embedding': [1., 0.],
                'metrics': {**PROPOSAL['metrics'], 'keyword_count': 3 * len(parts), 'total_volume': 600 * len(parts)}}
        merged = proposal([group([PROPOSAL, other])])
        with transaction.atomic():
            self.assertEqual(apply_evolution(self.organization, merged, date(2026, 9, 16), timezone.now()), [])
            self.assertEqual(apply_evolution(self.organization, merged, date(2026, 9, 16), timezone.now()), [])
            self.assertEqual(len(apply_evolution(self.organization, merged, date(2026, 9, 17), timezone.now())), 1)
        self.assertEqual(ContentIsland.objects.filter(slug__in=slugs).count(), 2)
        retired = ContentIsland.objects.get(slug__in=slugs, status='archived')
        self.assertEqual(retired.memberships.count(), 3)
        scope = resolve_island_discovery_scope(self.organization, self.config, retired.slug)
        self.assertTrue(scope['keyword'])
        split = proposal([group([PROPOSAL]), group([other])])
        with transaction.atomic():
            self.assertEqual(apply_evolution(self.organization, split, date(2026, 9, 18), timezone.now()), [])
            self.assertEqual(len(apply_evolution(self.organization, split, date(2026, 9, 19), timezone.now())), 1)
            self.assertEqual(apply_evolution(self.organization, merged, date(2026, 9, 20), timezone.now()), [])
        self.assertEqual(len(dynamic_scopes(self.organization)[0]['islands']), 2)
        self.assertGreaterEqual(ContentIslandKeyword.objects.count(), original_members)
        run.refresh_from_db()
        self.assertEqual(len(run.result[STATE_KEY]['history']), 2)
        keyword.refresh_from_db()
        self.assertEqual(keyword.written_article_id, article.pk)
        self.assertEqual(keyword.status, 'written')
        self.assertEqual(ContentIsland.objects.filter(organization=self.organization, status='visible',
            memberships__keyword=keyword).count(), 1)
