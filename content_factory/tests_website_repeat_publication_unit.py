"""Ordinary repeat polls reuse exact saved source and existing live-body proof."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import website_repeat_publication as repeat
from .models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle


class RepeatPublicationTests(SimpleTestCase):
    def setUp(self):
        self.organization = SimpleNamespace(pk=7, id=7, domain="fixture.test")
        self.context = SimpleNamespace(organization=self.organization, company=SimpleNamespace(pk="company", organization_id=7))
        self.connection = SimpleNamespace(pk="bcb13798-de52-4dd0-a1b8-6636bb7f2cc2", organization_id=7,
            organization=self.organization, generation=3, repository_id=19, github_repo="fixture/site",
            installation_id="11", branch="main", app_root="", state="connected", blockers=[])
        self.connection.id=self.connection.pk
        self.config = SimpleNamespace(pk=1, website_connection=self.connection, github_repo="fixture/site", github_installation_id="11")
        request = {"website_connection_id":str(self.connection.pk),"connection_generation":3,"repository_id":19,
            "github_repo":"fixture/site","domain":"fixture.test","deletion_epoch":0,"branch":"main","app_root":"",
            "expected_source_sha":"a"*40,"operation_id":"cdf32198-9745-4dd0-a1b8-6636bb7f2cc2","operation_attempt":1,
            "client_request_id":"ordinary-repeat-charge","roo_points_ledger_id":"ordinary-spend","delivery_mode":"website_preview"}
        self.body = "<article><h1>Repeat article</h1><p>" + "The ordinary publication keeps the original website source and content identity intact. "*8 + "</p></article>"
        self.writing = SimpleNamespace(pk=1,run_id="ordinary-writer",workflow="direct_generate",status="completed",
            organization_id=7,domain="fixture.test",github_repo="fixture/site",run_request=request,
            result={"content_package":{"article_html":self.body},"publish_child_run_id":"ordinary-publisher",
                "promoted_publish_job_id":"ordinary-publisher","presentation_only_child_state":"merged"},save=Mock())
        self.child = SimpleNamespace(pk=2,run_id="ordinary-publisher",workflow="direct_generate",status="completed",
            organization_id=7,domain="fixture.test",github_repo="fixture/site",
            run_request={**request,"source_run_id":self.writing.run_id,"delivery_mode":"publish_code",
                "operation_id":"ddf32198-9745-4dd0-a1b8-6636bb7f2cc2"},
            result={"merge_status":"merged","merge_sha":"b"*40,"merge_response":{"merged":True,"sha":"b"*40},
                "pr_url":"https://github.com/fixture/site/pull/2"},save=Mock())
        self.original = deepcopy((self.writing.run_request,self.child.run_request))
        self.selected = Mock(); self.selected.select_related.return_value.filter.return_value.first.return_value=self.config
        self.enterContext(patch.object(OrganizationContentConfig,"objects",self.selected))
        self.operations = self.enterContext(patch("content_factory.website_operations.validate_operation"))
        self.throttle = self.enterContext(patch.object(repeat.cache,"add",return_value=True))
        self.refresh = self.enterContext(patch("content_factory.article_publish_status.refresh_publish_statuses",return_value=[]))
        self.child_resolver = self.enterContext(patch("content_factory.vibe_marketing_views._publish_child_for_run_state",return_value=self.child))
        self.source_resolver = self.enterContext(patch("content_factory.vibe_marketing_views._publish_source_run_for_child",return_value=self.writing))

    def refresh_owned(self, **kwargs):
        return repeat.refresh_owned_publication(run=self.writing,context=self.context,**kwargs)

    def test_exact_merged_ordinary_pair_uses_cooldown_and_capped_scoped_budget(self):
        self.assertEqual(self.refresh_owned(max_seconds=90),[])
        self.refresh.assert_called_once_with(self.organization,self.config,limit=2,force=False,max_seconds=3.0,
            source_run_ids=(self.writing.run_id,self.child.run_id))
        self.assertEqual(self.operations.call_count,2)
        self.assertEqual([call.args[1]["run_id"] for call in self.operations.call_args_list],
            [self.writing.run_id,self.child.run_id])
        self.throttle.assert_called_once()
        self.assertEqual(self.throttle.call_args.args[-1],30)
        self.assertEqual((self.writing.run_request,self.child.run_request),self.original)
        self.writing.save.assert_not_called();self.child.save.assert_not_called()

    def test_no_second_provider_refresh_on_fast_poll_and_no_force_bypass(self):
        self.throttle.side_effect=[True,False]
        self.refresh_owned();self.refresh_owned()
        self.assertEqual(self.refresh.call_count,1)
        self.assertIs(self.refresh.call_args.kwargs["force"],False)

    def test_first_goal_legacy_foreign_pending_portable_or_invalid_inputs_skip_before_queries(self):
        for damage in ("first_article_goal_id","first_article_goal_operation_id","strategy_goal_id","goal_budget", "goalBudget",
                "unbound","pending","foreign","portable","malformed-domain","malformed-source"):
            with self.subTest(damage=damage):
                original=deepcopy(self.writing.run_request)
                status,org=self.writing.status,self.writing.organization_id
                if damage in repeat.GOAL_FIELDS:self.writing.run_request[damage]=None
                elif damage=="unbound":self.writing.run_request.pop("website_connection_id")
                elif damage=="pending":self.writing.status="running"
                elif damage=="foreign":self.writing.organization_id=8
                elif damage=="portable":self.writing.run_request["delivery_mode"]="content_only"
                elif damage=="malformed-domain":self.writing.run_request["domain"]={"unsafe":"input"}
                else:self.writing.run_request["expected_source_sha"]=1
                self.assertEqual(self.refresh_owned(),[])
                self.writing.run_request=original;self.writing.status=status;self.writing.organization_id=org
        self.selected.select_related.assert_not_called();self.child_resolver.assert_not_called();self.refresh.assert_not_called()

    def test_unmerged_missing_or_foreign_publisher_never_starts_live_probe(self):
        for damage in ("unmerged","no-sha","wrong-pr","wrong-parent","wrong-source","wrong-child-pointer","child-goal","foreign"):
            with self.subTest(damage=damage):
                result,request=deepcopy(self.child.result),deepcopy(self.child.run_request)
                pointer=self.writing.result["publish_child_run_id"];organization=self.child.organization_id
                if damage=="unmerged":self.child.result["merge_status"]="pending"
                elif damage=="no-sha":self.child.result.pop("merge_sha")
                elif damage=="wrong-pr":self.child.result["pr_url"]="https://github.com/other/site/pull/2"
                elif damage=="wrong-parent":self.child.run_request["source_run_id"]="another-writer"
                elif damage=="wrong-source":self.child.run_request["expected_source_sha"]="c"*40
                elif damage=="wrong-child-pointer":self.writing.result["publish_child_run_id"]="another-publisher"
                elif damage=="child-goal":self.child.run_request["first_article_goal_id"]="past-goal"
                else:self.child.organization_id=8
                self.assertEqual(self.refresh_owned(),[])
                self.child.result=result;self.child.run_request=request;self.child.organization_id=organization
                self.writing.result["publish_child_run_id"]=pointer
        self.selected.select_related.assert_not_called();self.refresh.assert_not_called()

    def test_current_selection_connection_generation_branch_root_epoch_and_installation_fence(self):
        for field,value in (("generation",4),("repository_id",20),("github_repo","other/site"),("branch","other"),
                ("app_root","other"),("installation_id","12"),("state","disconnected"),
                ("blockers",[{"deletion_epoch":1}])):
            with self.subTest(field=field):
                before=getattr(self.connection,field);setattr(self.connection,field,value)
                self.assertEqual(self.refresh_owned(),[])
                setattr(self.connection,field,before)
        self.refresh.assert_not_called()
        with patch("content_factory.website_operations.validate_operation",side_effect=ValueError("revoked original attempt")):
            self.assertEqual(self.refresh_owned(),[])
        self.refresh.assert_not_called()

    def test_no_outbound_calls_under_transaction_or_invalid_budget_and_provider_failure_is_inert(self):
        with patch.object(repeat.database,"in_atomic_block",True):self.assertEqual(self.refresh_owned(),[])
        for budget in (0,-1,float("inf"),float("nan"),True,"3"):
            self.assertEqual(self.refresh_owned(max_seconds=budget),[])
        self.refresh.assert_not_called();self.selected.select_related.assert_not_called()
        self.refresh.side_effect=RuntimeError("private provider response")
        self.assertEqual(self.refresh_owned(),[])
        self.assertEqual((self.writing.run_request,self.child.run_request),self.original)

    def test_viewing_exact_publish_child_resolves_only_its_owned_writer(self):
        repeat.refresh_owned_publication(run=self.child,context=self.context,child=self.child)
        self.source_resolver.assert_called_once_with(self.child,self.context)
        self.refresh.assert_called_once()

    def test_actual_refresher_advances_second_writer_live_receipt_without_payment_or_consent_changes(self):
        from . import article_publish_status as status
        from workflow_runs.models import ContentFactoryRun
        # Preserve the real refresher and its body/canonical proof. Only storage,
        # DNS and provider transports below are replaced.
        self.refresh.side_effect=ACTUAL_REFRESH
        article=SimpleNamespace(id=21,organization=self.organization,organization_id=7,source_run_id=self.writing.run_id,
            slug="repeat",pr_url=self.child.result["pr_url"],publish_status=ArticlePublishStatus.MERGED,
            live_checked_at=None,pr_number=2,pr_merged_at=None,merge_commit_sha="",on_main_verified_at=None,
            on_main_commit_sha="",live_verified_at=None,live_url="",canonical_url="",canonical_path="",save=Mock())
        from unittest.mock import MagicMock
        query=MagicMock();query.filter.return_value=query;query.order_by.return_value=query;query.__getitem__.return_value=[article]
        articles=Mock();articles.filter.return_value=query
        runs=Mock();runs.select_for_update.return_value.get.return_value=self.writing
        url="https://fixture.test/articles/repeat"
        def provider(address,**kwargs):
            response=Mock(status_code=200)
            if address.endswith("/sitemap.xml"):
                response.content=("<urlset><url><loc>"+url+"</loc></url></urlset>").encode()
            elif address=="https://api.github.com/repos/fixture/site/pulls/2":
                response.json.return_value={"merged":True,"merge_commit_sha":"b"*40,"base":{"ref":"main"}}
            elif address==url:
                response.iter_content.return_value=[('<link rel="canonical" href="'+url+'">'+self.body).encode()]
            else:self.fail("Unscoped provider address")
            return response
        with patch.object(WrittenArticle,"objects",articles),patch.object(ContentFactoryRun,"objects",runs), \
                patch.object(status,"_source_run_for_article",return_value=self.writing), \
                patch.object(status,"_github_read_token",return_value="synthetic-read-token"), \
                patch.object(status.http_requests,"get",side_effect=provider), \
                patch("content_factory.article_live_evidence.socket.getaddrinfo",return_value=[(2,1,6,"",("8.8.8.8",443))]), \
                patch.object(status.transaction,"atomic",side_effect=lambda:nullcontext()), \
                patch.object(status.cache,"get",return_value=None),patch.object(status.cache,"set"):
            self.assertEqual(self.refresh_owned(),[article])
        receipt=self.writing.result["release_observations"]["21"]
        self.assertEqual(receipt["state"],"verified")
        self.assertEqual(receipt["deployment_state"],"matching_content_served")
        self.assertEqual(receipt["merge_commit_sha"],"b"*40)
        self.assertEqual(article.publish_status,ArticlePublishStatus.LIVE)
        self.assertEqual(self.writing.result["presentation_only_child_state"],"merged")
        self.assertEqual((self.writing.run_request,self.child.run_request),self.original)
        query.filter.assert_called_with(source_run_id__in=(self.writing.run_id,self.child.run_id))


from .article_publish_status import refresh_publish_statuses as ACTUAL_REFRESH
