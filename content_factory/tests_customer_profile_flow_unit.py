"""Pure historical-attribution tests; no database, migrations or external services."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from copy import deepcopy
from .article_editorial import snapshot_from_run, snapshot_fields, ArticleEditorialConflict
from .editorial_catalog import catalog_payload, content_hash
from .editorial_contract import AudienceOption


class CustomerProfileHistoryTests(unittest.TestCase):
    def setUp(self):
        self.org = SimpleNamespace(pk=1, domain="example.com")
        self.brief = {"audience_id":"owner", "audience_version":1, "conversion_intent":"none", "offer_id":None, "offer_version":None,
            "no_offer_reason":"Useful reference", "country":"AU", "reader_task":"Choose a workflow", "distinct_contribution":"Worked comparison", "acceptance_criteria":["Name tradeoffs"]}
        self.run = SimpleNamespace(organization_id=1, domain="example.com", run_id="writing-1", created_at=datetime(2026,9,16,tzinfo=timezone.utc), run_request={"editorial_brief":self.brief})

    def test_partial_history_never_resolves_current_catalog(self):
        snapshot = snapshot_from_run(self.run,self.org)
        self.assertEqual(snapshot["provenance_status"], "partial")
        self.assertIsNone(snapshot["admission"])
        fields=snapshot_fields(snapshot)
        self.assertEqual(fields["audience_id"],"owner")
        self.assertEqual(fields["conversion_intent"],"none")
        self.assertEqual(fields["offer_id"],"")

    def test_unknown_stays_unknown(self):
        self.run.run_request={}
        self.assertIsNone(snapshot_from_run(self.run,self.org))
        self.assertEqual(snapshot_fields(None), {})

    def test_cross_tenant_and_unrelated_incoming_evidence_rejected(self):
        with self.assertRaises(ArticleEditorialConflict):
            snapshot_from_run(self.run,SimpleNamespace(pk=2,domain="elsewhere.test"))
        with self.assertRaises(ArticleEditorialConflict):
            snapshot_from_run(self.run,self.org,{"fake":"admission"})

    def test_legacy_approval_hash_does_not_gain_new_defaults(self):
        old={"id":"owner","reader_task":"Choose", "constraints":[], "exclusions":[], "status":"approved", "version":1,
             "approved_by":"user:1", "approved_at":"2026-09-11T00:00:00Z", "allow_no_offer":True}
        receipt={"content_sha256":content_hash(old),"approved_by":old["approved_by"],"approved_at":old["approved_at"]}
        strategy={"editorial_catalog":{"schema_version":2,"version":1,"audience_options":[old],"cta_options":[],"approval_receipts":{"audience":{"owner":receipt}}}}
        self.assertEqual(catalog_payload(strategy)["audience_options"][0],old)
        changed=deepcopy(strategy)
        changed["editorial_catalog"]["audience_options"][0].update(catalog_schema_version=2,name="Owners",description="Small firm owner")
        self.assertEqual(catalog_payload(changed)["audience_options"][0]["status"],"draft")

    def test_v2_content_is_not_silently_projected_to_legacy(self):
        with self.assertRaises(ValueError):
            AudienceOption(id="owner",reader_task="Choose",name="Owners")


class ArticlePersistenceLogicTests(unittest.TestCase):
    """Exercise the real service against in-memory ORM seams; no SQL/migrations."""
    def setUp(self):
        import sys
        from unittest.mock import patch
        from contextlib import nullcontext
        from types import ModuleType
        import uuid
        self.org=SimpleNamespace(pk=1,domain='example.com')
        self.articles=[]; self.runs=[]; self.config=SimpleNamespace(pillar_strategy={})
        class Query:
            def __init__(self, rows): self.rows=rows
            def select_for_update(self): return self
            def filter(self, **kw):
                return Query([r for r in self.rows if all(str(getattr(r,k,None))==str(v) for k,v in kw.items())])
            def first(self): return self.rows[0] if self.rows else None
            def get(self, **kw): return self.filter(**kw).first()
        rows=self.articles
        class Article:
            objects=Query(rows)
            def __init__(self,**kw):
                self.pk=self.id=str(uuid.uuid4());self.analytics_id=str(uuid.uuid4());self.source_run_id='';self.published_at=None
                self.editorial_snapshot=self.original_editorial_snapshot=None;self.editorial_provenance_status='unknown'
                self.title='';self.slug='';self.pr_url='';self.publish_status='written';self.__dict__.update(kw)
            def save(self):
                if self not in rows: rows.append(self)
        modules={}
        for name,attrs in {
            'organizations.models':{'Organization':SimpleNamespace(objects=Query([self.org]))},
            'workflow_runs.models':{'ContentFactoryRun':SimpleNamespace(objects=Query(self.runs))},
            'content_factory.models':{'WrittenArticle':Article,'OrganizationContentConfig':SimpleNamespace(objects=Query([SimpleNamespace(organization=self.org,**self.config.__dict__)]))},
            'content_factory.article_publish_status':{'advance_publish_status':lambda a,s,**kw:setattr(a,'publish_status',s)},
        }.items():
            module=ModuleType(name);module.__dict__.update(attrs);modules[name]=module
        context=patch.dict(sys.modules,modules);context.start();self.addCleanup(context.stop)
        context=patch('django.db.transaction.atomic',lambda:nullcontext());context.start();self.addCleanup(context.stop)
        self.model=Article
        self.models=modules['content_factory.models']
        self.brief={'audience_id':'owner','audience_version':1,'offer_id':None,'offer_version':None,'conversion_intent':'none','no_offer_reason':'Teaching','country':'AU','reader_task':'Compare workflows','distinct_contribution':'Worked comparison','acceptance_criteria':['Explain tradeoffs']}
        from .article_editorial import upsert_written_article
        self.save=upsert_written_article

    def run_fixture(self,id,parent=None,brief=True,workflow='article_generation',**extra):
        request={'analytics_article_id':'stable-article',**extra}
        if parent:request['revision_source_run_id']=parent
        if brief:request['editorial_brief']=deepcopy(self.brief)
        run=SimpleNamespace(run_id=id,organization=self.org,organization_id=1,domain='example.com',workflow=workflow,created_at=datetime(2026,9,16,tzinfo=timezone.utc),run_request=request)
        self.runs.append(run);return run

    def write(self,run,slug='article',**defaults):
        return self.save(organization=self.org,slug=slug,source_run_id=run.run_id,defaults={'title':run.run_id,**defaults})[0]

    def test_idempotent_retries_and_conflicting_same_run(self):
        run=self.run_fixture('first');article=self.write(run)
        self.assertIs(self.write(run),article);self.assertEqual(len(self.articles),1)
        run.run_request['editorial_brief']['reader_task']='Changed behind the same run'
        with self.assertRaises(ArticleEditorialConflict):self.write(run)
        self.assertEqual(article.editorial_snapshot['brief']['reader_task'],'Compare workflows')

    def test_revision_rename_preserves_original_and_stale_callback_cannot_revert(self):
        first=self.run_fixture('first');article=self.write(first);original=deepcopy(article.original_editorial_snapshot)
        later=self.run_fixture('second',parent='first',workflow='article_revision')
        self.write(later,slug='new-slug')
        self.assertEqual(len(self.articles),1);self.assertEqual(article.source_run_id,'second')
        self.assertEqual(article.original_editorial_snapshot,original)
        self.write(first,slug='article',title='Stale title',publish_status='pr_open')
        self.assertEqual(article.slug,'new-slug');self.assertEqual(article.title,'second');self.assertEqual(article.publish_status,'written')

    def test_unrelated_source_cannot_reassign_an_existing_article(self):
        self.write(self.run_fixture('first'))
        with self.assertRaises(ArticleEditorialConflict):self.write(self.run_fixture('unrelated'))

    def test_sparse_revision_cannot_erase_attribution_or_move_writing_identity(self):
        article=self.write(self.run_fixture('first'));original=deepcopy(article.editorial_snapshot)
        self.write(self.run_fixture('sparse',parent='first',brief=False,workflow='article_revision'),title='Unknown replacement',pr_url='https://example.test/pr')
        self.assertEqual(article.source_run_id,'first');self.assertEqual(article.title,'first');self.assertEqual(article.editorial_snapshot,original)
        self.assertEqual(article.pr_url,'https://example.test/pr')

    def test_publishing_child_uses_parent_writing_snapshot(self):
        first=self.run_fixture('first');article=self.write(first)
        child=self.run_fixture('publish',brief=False,source_run_id='first',delivery_mode='publish_code')
        self.write(child,pr_url='https://example.test/pr')
        self.assertEqual(article.source_run_id,'first');self.assertEqual(article.editorial_snapshot['writing_run_id'],'first')

    def test_catalogued_new_article_cannot_materialize_without_admission(self):
        config=self.models.OrganizationContentConfig.objects.first();config.pillar_strategy={'editorial_catalog':{}}
        with self.assertRaises(ArticleEditorialConflict):self.write(self.run_fixture('partial'))
        self.assertEqual(self.articles,[])

    def test_same_keyword_distinct_article_identities_stay_separate(self):
        self.write(self.run_fixture('first'),primary_keyword='workflow')
        self.write(self.run_fixture('other',analytics_article_id='another-article'),slug='another-angle',primary_keyword='workflow')
        self.assertEqual(len(self.articles),2)

    def test_discovery_parent_is_not_mistaken_for_the_writing_run(self):
        self.run_fixture('research',brief=False,workflow='auto_discovery')
        writing=self.run_fixture('writing',source_run_id='research',delivery_mode='publish_code')
        article=self.write(writing)
        self.assertEqual(article.source_run_id,'writing')
        self.assertEqual(article.editorial_snapshot['writing_run_id'],'writing')


class ReaderTaskCoverageTests(unittest.TestCase):
    def test_profile_change_alone_does_not_clear_coverage(self):
        from .article_editorial import distinct_reader_task
        original={'reader_task':'Compare workflows.','distinct_contribution':'A comparison','audience_id':'owner'}
        self.assertFalse(distinct_reader_task({**original,'audience_id':'builder'},[{'brief':original}]))
        self.assertFalse(distinct_reader_task({**original,'reader_task':'Compare WORKFLOWS!'},[{'brief':original}]))
        self.assertFalse(distinct_reader_task({'reader_task':'Implement a workflow','distinct_contribution':'Code tutorial'},[None]))
        self.assertTrue(distinct_reader_task({'reader_task':'Implement a workflow','distinct_contribution':'Code tutorial'},[{'brief':original}]))

    def test_discovery_only_uses_the_selected_approved_revision(self):
        from .editorial_catalog import discovery_audience_context, CatalogConflict
        from .tests_editorial_catalog_unit import approved_catalog, catalog_payload
        strategy=approved_catalog(); current=catalog_payload(strategy)
        audience=current['audience_options'][0]
        payload={'preferredAudienceId':audience['id'],'expectedEditorialCatalogVersion':current['editorial_catalog_version']}
        selected=discovery_audience_context(strategy,payload)
        self.assertEqual(selected['reader_task'],audience['reader_task'])
        self.assertNotIn('approved_by',selected)
        self.assertIsNone(discovery_audience_context(strategy,{}))
        with self.assertRaises(CatalogConflict):discovery_audience_context(strategy,{**payload,'expectedEditorialCatalogVersion':-1})
        with self.assertRaises(ValueError):discovery_audience_context(strategy,{**payload,'preferredAudienceId':'foreign'})
