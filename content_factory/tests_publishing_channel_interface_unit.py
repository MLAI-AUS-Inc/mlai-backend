"""The common interface must preserve existing authority and original run intent."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings

from . import publishing_channels as channels
from .publishing_channel_contract import ChannelRequest, OPERATIONS
from .publishing_channel_adapters import CmsChannel, HostedChannel, GitChannel, PortableChannel
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection
from . import tests_publishing_channels_unit as choice_fixtures
from . import vibe_marketing_views as views
from . import website_prepare as prepare


@override_settings(ARTICLES_PUBLISHING_CHANNELS_ENABLED=True, ARTICLES_CHANNEL_DISCOVERY_ENABLED=True)
class ChannelInterfaceTests(SimpleTestCase):
    update = choice_fixtures.PublishingChannelChoiceTests.update

    def setUp(self):
        # Import decorated billing services before the shared fixture replaces
        # transaction.atomic; lazy decorators must see the real implementation.
        from roo import services
        choice_fixtures.PublishingChannelChoiceTests.setUp(self)
        self.user = get_user_model()(pk=4)
        self.profile.user_id = self.user.pk
        self.config.website_connection = WebsiteConnection(organization=self.org, github_repo='fixture/site', branch='main')
        self.operation = SimpleNamespace(receipt={}, payload={})
        self.request = ChannelRequest(context=self.context, config=self.config, user=self.user,
            operation=self.operation, step='setup', data={'journey': {'repository': {'observedSourceSha': 'a'*40}}})

    def test_git_scan_setup_and_verification_keep_the_same_original_context_and_result(self):
        for action, step in (('capture_existing','scan'),('prepare','setup'),('verify','ci-attestation'),('verify','verify')):
            with self.subTest(action=action,step=step), patch.object(prepare,'_dispatch_git_prepare_step',return_value={'status':'queued','run_id':'original'}) as legacy:
                request=replace(self.request,step=step)
                self.assertEqual(channels.execute(action,request),{'status':'queued','run_id':'original'})
                legacy.assert_called_once_with(self.operation,self.context,self.config,step,user=self.user,
                    journey=request.data['journey'])

    def test_user_data_cannot_choose_an_adapter_or_handler(self):
        request=replace(self.request,data={'journey':{},'channel':'cms','adapter':'hosted','handler':'publish','provider':'wordpress'})
        with patch.object(prepare,'_dispatch_git_prepare_step',return_value={'status':'queued'}) as git:
            channels.execute('prepare',request)
            git.assert_called_once()
        for invalid in ('arbitrary','__dict__','_unavailable',None):
            with self.assertRaises(WebsiteAuthorityError):channels.execute(invalid,request)

    def test_effects_require_current_founder_and_company_scope(self):
        for candidate in (replace(self.request,user=get_user_model()(pk=99)),
                          replace(self.request,context=SimpleNamespace(organization=self.org,company=SimpleNamespace(
                              domain=self.org.domain,organization_id=999,profile_id=self.profile.pk),profile=self.profile))):
            with patch.object(prepare,'_dispatch_git_prepare_step') as git, self.assertRaises(WebsiteAuthorityError):
                channels.execute('prepare',candidate)
            git.assert_not_called()

    def test_inspection_is_local_for_every_registered_channel(self):
        self.update()
        with patch.object(prepare,'_dispatch_git_prepare_step',side_effect=AssertionError('No work from inspect')):
            for adapter in (GitChannel(),CmsChannel(),HostedChannel(),PortableChannel()):
                result=adapter.inspect(self.request)
                self.assertEqual(result['channel'],adapter.name)
                self.assertEqual(result['planning']['revision'],1)
                self.assertNotIn('publishingReady',result)

    def test_unimplemented_provider_operations_never_fall_through_to_git(self):
        for adapter in (CmsChannel(),HostedChannel()):
            for action in OPERATIONS-{'inspect'}:
                with self.subTest(channel=adapter.name,action=action), self.assertRaises(WebsiteAuthorityError):
                    getattr(adapter,action)(self.request)
        self.update();self.update(selected='cms')
        with patch.object(prepare,'_dispatch_git_prepare_step') as git, self.assertRaises(WebsiteAuthorityError):
            channels.execute('prepare',self.request)
        git.assert_not_called()

    def test_hosted_strategy_rung_cannot_dispatch_another_repository_setup(self):
        from .website_strategy import RUNGS
        self.assertIn('hosted',RUNGS)
        self.operation.receipt={'goal':{'strategy_pin':{'version':1,'rung':'hosted','revision':6,
            'binding':'a'*64,'fingerprint_digest':'b'*64}}}
        with patch.object(prepare,'_dispatch_git_prepare_step') as git, self.assertRaises(WebsiteAuthorityError):
            channels.execute('prepare',self.request)
        git.assert_not_called()

    def test_original_git_publication_stays_git_after_portable_preference(self):
        self.update();self.update(selected='portable')
        run=SimpleNamespace(run_id='original',github_repo='fixture/site',domain=self.org.domain,
            workflow='article_generation',run_request={'website_connection_id':str(self.config.website_connection.pk),
                'connection_generation':1})
        request=replace(self.request,operation=None,run=run,step='publication',
            data={'action':'publish-pr','data':{'auto_merge':True},'run_id':None,'goal_operation_id':None})
        with patch.object(views,'_control_article_run_for_channel',return_value=Mock(status_code=202)) as legacy:
            response=channels.execute('publish',request)
            self.assertEqual(response.status_code,202)
            legacy.assert_called_once_with(user=self.user,context=self.context,run=run,**request.data)

    def test_portable_generation_keeps_explicit_confirmation_and_has_no_repository_authority(self):
        data={'delivery_mode':'content_only','delivery_mode_confirmed':True,'topic':'Owner topic'}
        original=deepcopy(data)
        request=replace(self.request,operation=None,step='generation',data={'data':data,'website_contract':None,
            'goal_operation_id':None,'request_hash':'original-hash'})
        with patch.object(views,'_start_article_generation_for_channel',return_value={'artifact_id':'export','status':'content_only'}) as legacy:
            result=channels.execute('publish',request)
            self.assertEqual(result,{'artifact_id':'export','status':'content_only'})
            legacy.assert_called_once_with(user=self.user,context=self.context,**request.data)
            self.assertEqual(data,original)
            with self.assertRaises(WebsiteAuthorityError):
                channels.execute('publish',replace(request,data={**request.data,'website_contract':{'website_connection_id':'foreign'}}))
        self.update();self.update(selected='portable')
        with self.assertRaises(WebsiteAuthorityError):
            channels.execute('publish',replace(request,data={**request.data,'data':{'delivery_mode':'content_only'}}))
        with self.assertRaises(WebsiteAuthorityError):
            PortableChannel().publish(replace(request,step='first-article'))

    def test_disconnect_follows_actual_git_binding_without_unpublishing(self):
        self.update();self.update(selected='portable')
        data={'website_connection_id':str(self.config.website_connection.pk),'connection_generation':1,'remove_setup':False}
        with patch('content_factory.website_disconnect.start_disconnect',return_value='saved-disconnect') as legacy:
            self.assertEqual(channels.execute('disconnect',replace(self.request,operation=None,data=data)),'saved-disconnect')
            legacy.assert_called_once_with(self.config,user=self.user,data=data)
        with self.assertRaises(WebsiteAuthorityError):channels.execute('unpublish',self.request)

    def test_disabled_dispatch_and_private_repr_reveal_no_content_or_credentials(self):
        request=replace(self.request,data={'password':'private-test-value','article':'private article body'})
        self.assertNotIn('private-test-value',repr(request));self.assertNotIn('private article body',repr(request))
        with override_settings(ARTICLES_PUBLISHING_CHANNELS_ENABLED=False), self.assertRaises(WebsiteAuthorityError):
            channels.execute('prepare',request)

    def test_enabled_git_interface_preserves_real_first_article_control_flow(self):
        from . import tests_website_first_article_unit as original
        scenarios = ('test_dispatch_journals_topic_before_calling_canonical_generation',
            'test_review_first_and_mandatory_review_dispatch_review_draft',
            'test_lost_dispatch_reply_recovers_owned_run_without_new_generation',
            'test_preview_message_without_matching_quality_cannot_request_approval',
            'test_stale_source_or_cancelled_goal_cannot_start_generation',
            'test_live_wait_uses_existing_deployment_policy_instead_of_completing_at_merge')
        for scenario in scenarios:
            with self.subTest(scenario=scenario), override_settings(**original.FirstArticleControllerTests._overridden_settings):
                fixture=original.FirstArticleControllerTests(scenario)
                fixture.setUp()
                try:
                    fixture.context.profile=SimpleNamespace(pk=3,user_id=4,role='founder')
                    fixture.context.company.domain='fixture.test'
                    fixture.context.company.organization_id=7
                    fixture.context.company.profile_id=3
                    fixture.config.pk=9;fixture.config.organization_id=7;fixture.config.article_system={}
                    getattr(fixture,scenario)()
                finally:
                    fixture.doCleanups()
