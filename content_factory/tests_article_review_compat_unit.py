"""New setup review behavior retains production authority and portable boundaries."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
from django.test import SimpleTestCase
from . import article_review_views as review
from . import article_preview_lease as lease


class CompatibilityGuardTests(SimpleTestCase):
    def setUp(self):
        self.context=SimpleNamespace(organization=SimpleNamespace(domain='owned.test'))
        self.run=SimpleNamespace(run_id='completed-setup', workflow='article_system_setup',
            domain='owned.test', github_repo='fixture/site', result={}, run_request={})
        self.request=SimpleNamespace(method='POST', data={'action':'applyUpdate','operationId':'operation-1'})
        for context in (patch.object(review.views,'_resolve_context_or_response',return_value=(self.context,None)),
            patch.object(review.views,'get_object_or_404',return_value=self.run),
            patch.object(review.views,'_run_belongs_to_context',return_value=True),
            patch.object(review.views,'_run_has_external_publish_evidence',return_value=True),
            patch.object(review.views,'_latest_review_ready_component_revision',return_value=None)):
            context.start(); self.addCleanup(context.stop)

    def test_completed_setup_review_and_comments_are_allowed_while_article_stays_sealed(self):
        for view in (review.VibeMarketingArticleReviewView(),review.views.VibeMarketingRunCommentsView(),
                     review.views.VibeMarketingRunCommentDetailView()):
            self.assertIsNone(view._resolve_run(self.request,self.run.run_id)[2])
        self.run.workflow='article_generation'
        self.assertEqual(review.VibeMarketingArticleReviewView()._resolve_run(self.request,self.run.run_id)[2].status_code,409)
        self.run.workflow='article_system_setup'
        self.assertEqual(review.views.VibeMarketingRunCommentsMixin()._resolve_run(self.request,self.run.run_id)[2].status_code,409)

    def test_accepted_retry_passes_mixin_without_allowing_new_update_on_superseded_source(self):
        self.run.result={'article_review_updates':{'operation-1':{'status':'accepted'}}}
        with patch.object(review.views,'_latest_review_ready_component_revision',return_value=SimpleNamespace(run_id='child')):
            self.assertIsNone(review.VibeMarketingArticleReviewView()._resolve_run(self.request,self.run.run_id)[2])
            self.request.data['operationId']='different-operation'
            self.assertEqual(review.VibeMarketingArticleReviewView()._resolve_run(self.request,self.run.run_id)[2].status_code,409)

    def test_fresh_operation_uses_original_consent_and_stable_review_key(self):
        original={'website_connection_id':str(uuid4()),'connection_generation':2,'repository_id':3,
                  'operation_id':str(uuid4()),'operation_attempt':1,'deletion_epoch':0}
        reserved=str(uuid4()); website=SimpleNamespace(pk=original['website_connection_id'])
        payload={'operationId':'operation-1','expectedRevision':'changed','textEdits':[],
                 'comments':[{'comment_id':str(uuid4()),'body':'Saved feedback','context':{}}]}
        def reserve(connection,*,workflow,payload):
            self.assertIs(connection,website)
            self.assertEqual(workflow,'article_system_setup')
            self.assertEqual(payload['client_request_id'],'review-operation-1')
            self.assertNotIn('expectedRevision',payload)
            self.assertNotIn('operation_id',payload)
            payload.update(operation_id=reserved,operation_attempt=1,deletion_epoch=0)
        with patch.object(review.views,'scoped_run_contract',return_value=original), \
             patch.object(review.views,'authority_guard',return_value=nullcontext(website)) as guard, \
             patch('content_factory.website_operations.reserve_workflow_operation',side_effect=reserve):
            contract=review._review_dispatch_contract(self.run,payload)
        guard.assert_called_once_with(original,action='read')
        self.assertEqual(contract['operation_id'],reserved)
        self.assertEqual(contract['website_connection_id'],original['website_connection_id'])
        self.assertEqual(contract['client_request_id'],'review-operation-1')

    def test_revoked_original_consent_cannot_reserve_review_and_portable_never_acquires_it(self):
        original={'website_connection_id':str(uuid4()),'connection_generation':2}
        with patch.object(review.views,'scoped_run_contract',return_value=original), \
             patch.object(review.views,'authority_guard',side_effect=review.views.WebsiteAuthorityError('website_disconnected','Disconnected')), \
             patch('content_factory.website_operations.reserve_workflow_operation') as reserve:
            self.assertEqual(review._review_dispatch_contract(self.run,{}).status_code,409)
            reserve.assert_not_called()
        with patch.object(review.views,'scoped_run_contract',return_value={'delivery_mode':'content_only'}), \
             patch.object(review.views,'authority_guard') as guard:
            self.assertEqual(review._review_dispatch_contract(self.run,{}),{})
            guard.assert_not_called()

    def test_remote_write_checks_new_review_operation_and_rejects_changed_binding(self):
        binding={'website_connection_id':str(uuid4()),'connection_generation':2,'repository_id':3}
        new_operation=str(uuid4())
        response=SimpleNamespace(status_code=200,json=lambda:{'revision':'saved','fields':[]})
        with patch.object(review.views,'scoped_run_contract',return_value={**binding,'operation_id':'completed-source'}), \
             patch.object(review.views,'authority_guard',side_effect=lambda *args,**kwargs:nullcontext()) as guard, \
             patch.object(review.views,'_content_factory_remote_config',return_value={'enabled':True,'base_url':'https://factory.invalid'}), \
             patch.object(review.views,'_content_factory_headers',return_value={'X-API-Key':'synthetic'}), \
             patch.object(review.views.http_client,'request',return_value=response) as request:
            payload={'action':'applyUpdate','operation_id':new_operation,'operation_attempt':1,'deletion_epoch':0}
            self.assertEqual(review.remote_review(self.run,payload=payload)['revision'],'saved')
            self.assertEqual(guard.call_args.args[0]['operation_id'],new_operation)
            self.assertEqual(request.call_args.kwargs['json']['website_connection_id'],binding['website_connection_id'])
            request.reset_mock()
            self.assertEqual(review.remote_review(self.run,payload={**payload,'website_connection_id':str(uuid4()),'connection_generation':2}).status_code,409)
            request.assert_not_called()

    def test_capability_path_traversal_is_rejected_with_existing_authority_checks_retained(self):
        for path in ('../other','%252e%252e/other','assets\\..\\other','/api'):
            self.assertFalse(lease.safe_preview_path(path))
        self.assertTrue(lease.safe_preview_path('assets/a%20b.js'))

    def test_article_text_only_retains_original_operation_without_reserving_child(self):
        original={'website_connection_id':str(uuid4()),'connection_generation':2,'repository_id':3,
                  'operation_id':str(uuid4()),'operation_attempt':2,'deletion_epoch':0,
                  'client_request_id':'original-article'}
        self.run.workflow='article_generation'
        with patch.object(review.views,'scoped_run_contract',return_value=original), \
             patch('content_factory.website_operations.reserve_workflow_operation') as reserve:
            self.assertEqual(review._review_dispatch_contract(self.run,{'commentIds':[]}),original)
        reserve.assert_not_called()

    def test_article_comments_reserve_separate_revision_fence(self):
        original={'website_connection_id':str(uuid4()),'connection_generation':2,'repository_id':3,
                  'operation_id':str(uuid4()),'operation_attempt':2,'deletion_epoch':0}
        self.run.workflow='article_generation'
        def reserve(website,*,workflow,payload):
            self.assertEqual(workflow,'article_revision')
            payload.update(operation_id='new-article-revision',operation_attempt=1,deletion_epoch=0)
        with patch.object(review.views,'scoped_run_contract',return_value=original), \
             patch.object(review.views,'authority_guard',return_value=nullcontext(SimpleNamespace())), \
             patch('content_factory.website_operations.reserve_workflow_operation',side_effect=reserve):
            result=review._review_dispatch_contract(self.run,{'operationId':'operation-1',
                'commentIds':[str(uuid4())],'comments':[{'body':'Saved'}],'textEdits':[]})
        self.assertEqual(result['operation_id'],'new-article-revision')


class CompatibilityDispatchReplayTests(SimpleTestCase):
    def setUp(self):
        from .tests_article_review_update_unit import Comments
        from . import article_review_callbacks as callbacks
        self.callbacks=callbacks
        self.binding={'website_connection_id':str(uuid4()),'connection_generation':2,'repository_id':3,
                      'operation_id':str(uuid4()),'operation_attempt':1,'deletion_epoch':0}
        self.operation_id=str(uuid4())
        self.context=SimpleNamespace(organization=SimpleNamespace(pk=7,id=7,domain='owned.test'))
        self.source=SimpleNamespace(pk=1,run_id='merged-setup',workflow='article_system_setup',
            domain='owned.test',github_repo='fixture/site',status='completed',approval_state='approved',
            run_request=self.binding,result={'merged':True,'pr_url':'https://github.invalid/pull/1'},save=Mock())
        self.child=SimpleNamespace(pk=2,run_id='setup-child',workflow='article_system_setup',
            domain='owned.test',github_repo='fixture/site',status='completed',result={'previewProof':'new'},save=Mock())
        self.comment=SimpleNamespace(id=uuid4(),run=self.source,status='draft',batch_id='',
            component_id='hero',component_type='heading',component_label='Hero',
            context={'domPath':'body > main'},anchor={},selector='[data-cf-component-id=hero]',
            body='Make it welcoming',save=Mock())
        self.storage=Comments([self.comment])
        self.runs=Mock();self.runs.filter.return_value.first.return_value=self.source
        self.runs.get.side_effect=lambda **kwargs:self.source if kwargs['pk']==1 else self.child
        self.view=review.VibeMarketingArticleReviewView()
        self.view._resolve_run=Mock(return_value=(self.context,self.source,None))
        self.request=SimpleNamespace(user=SimpleNamespace(pk=9),data={'action':'applyUpdate',
            'operationId':'operation-1','expectedRevision':'before','textEdits':[{'fieldId':'field-1',
            'value':'Exact manual text','originalValue':'Old'}],'commentIds':[str(self.comment.id)]})
        self.snapshot={'articleId':self.child.run_id,'revision':'after','fields':[],
                       'previewPending':True,'revisionRunId':self.child.run_id}
        def reserve(connection,*,workflow,payload):
            payload.update(operation_id=self.operation_id,operation_attempt=1,deletion_epoch=0)
        for context in (patch.object(review.transaction,'atomic',nullcontext),
            patch.object(review.views.ContentFactoryRun.objects,'select_for_update',return_value=self.runs),
            patch.object(review.views.VibeMarketingComponentComment,'objects',self.storage),
            patch.object(review,'_revision_authorization',return_value=None),
            patch.object(review.views,'_latest_review_ready_component_revision',return_value=None),
            patch.object(review.views,'_run_has_external_publish_evidence',return_value=True),
            patch.object(review.views,'scoped_run_contract',side_effect=lambda run:run.run_request),
            patch.object(review.views,'authority_guard',side_effect=lambda *args,**kwargs:nullcontext(SimpleNamespace(pk='website'))),
            patch.object(review.views,'_create_local_run',return_value=self.child),
            patch('content_factory.website_operations.reserve_workflow_operation',side_effect=reserve),
            patch('content_factory.website_operations.bind_operation_run'),
            patch('content_factory.website_models.WebsiteConnectionOperation.objects.get',return_value=SimpleNamespace(pk=self.operation_id))):
            result=context.start();self.addCleanup(context.stop)
            if context.attribute=='reserve_workflow_operation':self.reserve=result
        self.callback={'review_update_operation_id':'operation-1','feedback_batch_id':'operation-1',
            'source_setup_run_id':self.source.run_id,'comment_outcomes':[{'commentId':str(self.comment.id),
            'status':'addressed','summary':'Heading regenerated'}]}

    def assert_original_setup(self):
        self.assertEqual((self.source.status,self.source.approval_state),('completed','approved'))
        self.assertTrue(self.source.result['merged'])
        self.assertEqual(self.source.result['pr_url'],'https://github.invalid/pull/1')

    def test_fast_callback_then_lost_response_returns_same_child_and_keeps_outcomes(self):
        from rest_framework.response import Response
        def dispatch(run,*,payload=None):
            if payload is None:return self.snapshot
            entry=run.result['article_review_updates']['operation-1']
            self.assertEqual(entry['status'],'submitted')
            self.assertEqual(entry['remoteComments'],payload['comments'])
            self.assertEqual(entry['dispatchContract']['operation_id'],self.operation_id)
            self.assertEqual(payload['operation_id'],self.operation_id)
            self.assertEqual(run.result['component_feedback_latest_batch']['status'],'submitted')
            self.assertTrue(self.callbacks.reconcile_setup_review_outcomes(self.callback,self.child))
            return Response({'detail':'Response lost'},status=502)
        with patch.object(review,'remote_review',side_effect=dispatch) as remote:
            self.assertEqual(self.view.post(self.request,self.source.run_id).status_code,502)
            self.assertEqual(self.source.result['article_review_updates']['operation-1']['status'],'accepted')
            result=self.view.post(self.request,self.source.run_id)
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.data['revisionRunId'],self.child.run_id)
            self.assertEqual(len([call for call in remote.call_args_list if call.kwargs.get('payload')]),1)
        self.reserve.assert_called_once()
        self.assertEqual(self.source.result['component_feedback_latest_batch']['status'],'completed')
        self.assertEqual(self.comment.context['reviewOutcome']['status'],'addressed')
        self.assert_original_setup()

    def test_uncertain_retry_reuses_operation_and_frozen_context_before_completion(self):
        from rest_framework.response import Response
        captured=[]
        def dispatch(run,*,payload):
            captured.append(payload)
            return Response({'detail':'Response lost'},status=502) if len(captured)==1 else self.snapshot
        with patch.object(review,'remote_review',side_effect=dispatch):
            self.assertEqual(self.view.post(self.request,self.source.run_id).status_code,502)
            self.comment.context={**self.comment.context,'reviewOutcome':{'status':'addressed'}}
            self.assertEqual(self.view.post(self.request,self.source.run_id).status_code,202)
        self.assertEqual(captured[0]['comments'],captured[1]['comments'])
        self.assertEqual(captured[0]['operation_id'],captured[1]['operation_id'])
        self.reserve.assert_called_once()
        self.assertTrue(self.callbacks.reconcile_setup_review_outcomes(self.callback,self.child))
        self.assertEqual(self.source.result['component_feedback_latest_batch']['revisionRunId'],self.child.run_id)
        self.assert_original_setup()


class PostApprovalCallbackReplayTests(SimpleTestCase):
    assert_original_setup = CompatibilityDispatchReplayTests.assert_original_setup

    def setUp(self):
        CompatibilityDispatchReplayTests.setUp(self)
        from . import website_connections as authority, website_operations as operations
        self.authority,self.operations=authority,operations
        self.child.run_request={**self.binding,'operation_id':self.operation_id,
            'operation_attempt':1,'client_request_id':'review-operation-1'}
        self.child.organization_id=7
        self.source.organization_id=7
        self.callback.update(**self.child.run_request,event='article_system_setup_revision_ready',
            event_type='article_system_setup_revision_ready',event_id='processed-event-1',
            run_id=self.child.run_id,job_id=self.child.run_id,workflow='article_system_setup',
            domain=self.source.domain,github_repo=self.source.github_repo)
        with patch.object(review,'remote_review',return_value=self.snapshot):
            self.assertEqual(self.view.post(self.request,self.source.run_id).status_code,202)
        self.assertTrue(self.callbacks.reconcile_setup_review_outcomes(
            {**self.callback,'_execution_version_validated':True},self.child))
        self.child.status='completed';self.child.approval_state='approved'
        self.child.result.update(merged=True,pr_url='https://github.invalid/pull/2')
        self.source.save.reset_mock();self.child.save.reset_mock()
        self.website=SimpleNamespace(pk=self.binding['website_connection_id'],organization_id=7,
            generation=2,blockers=[],operations=Mock())
        self.operation=SimpleNamespace(pk=self.operation_id,generation=2,state='completed',
            action='workflow',payload={'attempt':1,'run_id':self.child.run_id})
        self.website.operations.filter.return_value.first.return_value=self.operation
        self.processed=Mock(return_value=True)
        context=patch.object(self.callbacks.ContentFactoryRun.objects,'filter',
            side_effect=lambda **kw:SimpleNamespace(first=lambda:self.source if kw['run_id']==self.source.run_id else self.child))
        context.start();self.addCleanup(context.stop)
        context=patch.object(self.callbacks.ContentFactoryCallbackEvent.objects,'filter')
        context.start().return_value.exists=self.processed
        self.addCleanup(context.stop)

    def guard(self,data):
        return self.operations.validate_operation(self.website,data)

    def test_processed_ready_callback_after_approval_acknowledges_without_handlers_or_writes(self):
        self.assertIs(self.guard(self.callback),self.operation)
        handler=Mock(side_effect=AssertionError('Never replay approved child state'))
        def guarded(payload,**kwargs):
            self.guard(payload)
            return nullcontext(self.website)
        request=SimpleNamespace(method='POST',data=self.callback)
        decorated=self.authority.guarded_service_write('config_write',only_repository=True)(handler)
        with patch.object(self.authority,'authority_guard',side_effect=guarded), \
             patch.object(self.authority,'record_scan_evidence') as evidence:
            response=decorated(SimpleNamespace(),request)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['status'],'duplicate')
        handler.assert_not_called();evidence.assert_not_called()
        self.child.save.assert_not_called();self.source.save.assert_not_called()
        self.assertEqual((self.child.status,self.child.approval_state),('completed','approved'))
        self.assertTrue(self.child.result['merged']);self.assert_original_setup()

    def test_new_changed_or_unacknowledged_callback_still_rejected(self):
        from copy import deepcopy
        from .website_contract import WebsiteAuthorityError
        cases=[{**self.callback,'event_id':'new-event'},
            {**self.callback,'source_setup_run_id':'wrong-source'},
            {**self.callback,'operation_id':self.binding['operation_id']},
            {**self.callback,'feedback_batch_id':'wrong-batch'}]
        changed=deepcopy(self.callback);changed['comment_outcomes'][0]['summary']='Forged new outcome'
        cases.append(changed)
        for data in cases:
            with self.subTest(data=data),self.assertRaises(WebsiteAuthorityError):self.guard(data)
        self.processed.return_value=False
        with self.assertRaises(WebsiteAuthorityError):self.guard(self.callback)

    def test_revoked_authority_is_checked_before_processed_replay(self):
        from .website_contract import WebsiteAuthorityError
        handler=Mock()
        decorated=self.authority.guarded_service_write('config_write',only_repository=True)(handler)
        with patch.object(self.authority,'authority_guard',side_effect=WebsiteAuthorityError('website_disconnected','Revoked')), \
             patch.object(self.authority,'record_denied_terminal_callback',return_value=False):
            result=decorated(SimpleNamespace(),SimpleNamespace(method='POST',data=self.callback))
        self.assertEqual(result.status_code,409);handler.assert_not_called()

    def test_text_only_review_receipt_also_acknowledges_exact_post_approval_replay(self):
        self.callback['comment_outcomes']=[]
        self.source.result['article_review_updates']['operation-1']['commentIds']=[]
        self.assertTrue(self.callbacks.reconcile_setup_review_outcomes(self.callback,self.child))
        self.assertIs(self.guard(self.callback),self.operation)
        self.assertEqual(self.source.result['article_review_outcomes']['operation-1']['outcomes'],[])
