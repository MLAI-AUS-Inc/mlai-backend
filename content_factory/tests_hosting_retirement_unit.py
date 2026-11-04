"""Retirement policy and orchestration with synthetic storage/provider boundaries."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from . import hosted_articles as hosting, hosted_auto_register as registration, hosted_retirement as retirement
from .hosted_article_models import HostedArticleSite
from .hosted_article_views import HostedArticlePublishView
from .publish_reviewed_draft import _send_dispatch, PublishReviewedError


class RetirementPolicyTests(SimpleTestCase):
    @override_settings(ARTICLES_HOSTED_SITES_ENABLED=True, ARTICLES_HOSTED_AUTO_REGISTER_ENABLED=True,
        ARTICLES_HOSTED_RUNG_ORGANIZATIONS='all')
    def test_retired_policy_is_inert_even_with_every_old_flag_enabled(self):
        with patch.object(HostedArticleSite.objects, 'filter', side_effect=AssertionError('No lookup')):
            self.assertTrue(hosting.hosting_retired())
            self.assertIsNone(hosting.active_site(SimpleNamespace(pk=1)))
            self.assertEqual(hosting.hosted_capabilities(SimpleNamespace()), {})
            self.assertFalse(registration.hosted_rung_ready(SimpleNamespace(pk=1)))
            self.assertEqual(registration.run_hosted_sweep(), {'status': 'retired'})
        with self.assertRaises(hosting.HostedPublishError) as error:
            hosting.register_site(organization=None, profile={})
        self.assertEqual(error.exception.reason, 'hosting_retired')

    def test_post_is_gone_and_does_not_dispatch(self):
        with patch('integrations.http_client.post') as post:
            response = HostedArticlePublishView().post(APIRequestFactory().post('/'), 'run')
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.data['reason'], 'hosting_retired')
        post.assert_not_called()

    def test_old_dispatch_cannot_reserve_or_call_content_factory(self):
        with patch('integrations.http_client.post') as post:
            with self.assertRaises(PublishReviewedError) as error:
                _send_dispatch(SimpleNamespace(), SimpleNamespace(run_request={'hosted_site_id': 'old'}))
        self.assertEqual(error.exception.reason, 'not_offered')
        post.assert_not_called()

    def test_legacy_pin_falls_back_and_new_ladder_has_no_hosting(self):
        from .website_strategy import RUNGS
        from .failure_policy_engine import _rung
        self.assertNotIn('hosted', RUNGS)
        self.assertEqual(_rung({}, {'strategy_pin': 'hosted'}), 'native_collection')

    def test_active_hosting_does_not_override_verified_repo(self):
        from . import publish_reviewed_draft as publishing
        from .portable_drafts import has_connected_website
        target = SimpleNamespace(capabilities={'publishingReady': True})
        connection = SimpleNamespace(state='connected', verified_sha='a'*40, generation=1,
            capabilities={'publishingReady': True}, targets=MagicMock())
        connection.targets.filter.return_value.first.return_value = target
        config = SimpleNamespace(website_connection=connection, website_connection_id='conn', default_publish_target_id='blog')
        query = MagicMock(); query.select_related.return_value.filter.return_value.first.return_value = config
        with patch.object(publishing, 'OrganizationContentConfig', SimpleNamespace(objects=query)), patch.object(
                publishing, 'read_marker', return_value={'publishAutomatically': {'enabledAt': 'today'}}):
            self.assertEqual(publishing._destination(SimpleNamespace(pk=1)), (config, connection, None))
        self.assertTrue(has_connected_website(config))


class RetirementFlowTests(SimpleTestCase):
    def setUp(self):
        self.site = SimpleNamespace(pk='site', domain='example.test', base_path='/blog/', profile={}, state='active',
            worker_url='https://worker.example', organization=SimpleNamespace(pk=1), publications=MagicMock())
        self.site.publications.filter.return_value.select_related.return_value.order_by.return_value = []
        self.events = []
        def save(site, receipt, **fields):
            self.events.append(('save', deepcopy(receipt), fields))
            site.profile = {'retirement': deepcopy(receipt)}
            for key, value in fields.items():setattr(site, key, value)
        self.save = patch.object(retirement, '_save', side_effect=save).start()
        self.addCleanup(patch.stopall)
        patch('content_factory.hosted_articles._source_connection', side_effect=hosting.HostedPublishError('not_connected', 'Not connected')).start()
        self.destination = patch('content_factory.publish_reviewed_draft._destination', return_value=None).start()
        self.remote = patch.object(retirement, '_remote', side_effect=lambda site, action, **fields:
            self.events.append(('remote', action)) or {'status': 'awaiting_founder_merge'}).start()
        self.served = patch.object(retirement, '_founder_serves', return_value=False).start()
        patch.object(retirement, '_alert', return_value='alert').start()

    def test_dry_run_has_no_writes_or_provider_calls(self):
        result = retirement.retire_site(self.site, dry_run=True)
        self.assertTrue(result['dry_run']); self.save.assert_not_called(); self.remote.assert_not_called()

    def test_freeze_first_then_proxy_pr_and_no_delete_while_proxy_serves(self):
        result = retirement.retire_site(self.site)
        self.assertEqual(self.events[0][2]['state'], 'paused')
        self.assertEqual(result['status'], 'waiting_for_proxy_merge')
        self.remote.assert_called_once_with(self.site, 'proxy_removal_pr')
        retirement.retire_site(self.site)
        self.assertEqual([call.args[1] for call in self.remote.call_args_list],
                         ['proxy_removal_pr', 'proxy_removal_pr'])

    def test_delete_only_after_public_verification_and_retired_rerun_is_noop(self):
        self.served.return_value = True
        self.remote.side_effect = lambda site, action, **fields: (
            {'status': 'proxy_absent'} if action == 'proxy_removal_pr' else {'deleted': True})
        with patch('content_factory.hosted_retirement.transaction.atomic', return_value=MagicMock()):
            result = retirement.retire_site(self.site)
        self.assertEqual(result['status'], 'retired')
        self.assertEqual(self.remote.call_args.args[1], 'delete_worker')
        calls = len(self.events)
        self.assertEqual(retirement.retire_site(self.site), result)
        self.assertEqual(len(self.events), calls)

    def test_missing_header_cannot_bypass_founder_proxy_merge(self):
        self.served.return_value = True
        pending = {'status': 'awaiting_founder_merge', 'pr_url': 'https://github.com/example/site/pull/1',
                   'head_sha': 'a' * 40}
        self.remote.side_effect = lambda site, action, **fields: pending
        with patch('content_factory.hosted_retirement.transaction.atomic', return_value=MagicMock()):
            receipt = retirement.retire_site(self.site)
        self.assertEqual(receipt['status'], 'waiting_for_proxy_merge')
        self.assertEqual(self.site.state, 'paused')
        self.remote.assert_called_once_with(self.site, 'proxy_removal_pr')
        self.remote.side_effect = lambda site, action, **fields: (
            {'status': 'proxy_absent'} if action == 'proxy_removal_pr' else {'deleted': True})
        with patch('content_factory.hosted_retirement.transaction.atomic', return_value=MagicMock()):
            receipt = retirement.retire_site(self.site)
        self.assertEqual(receipt['status'], 'retired')
        self.assertEqual(receipt['proxy_removal']['pr_url'], pending['pr_url'])
        self.assertEqual([call.args[1] for call in self.remote.call_args_list],
                         ['proxy_removal_pr', 'proxy_removal_pr', 'delete_worker'])

    def test_unproven_proxy_receipt_cannot_delete_worker(self):
        self.served.return_value = True
        self.remote.side_effect = lambda site, action, **fields: {}
        with patch('content_factory.hosted_retirement.transaction.atomic', return_value=MagicMock()):
            receipt = retirement.retire_site(self.site)
        self.assertEqual(receipt['status'], 'waiting_for_proxy_merge')
        self.remote.assert_called_once_with(self.site, 'proxy_removal_pr')

    def test_deadline_is_durable_and_allows_explicit_grace_expiry(self):
        retirement.retire_site(self.site, now=timezone.now()-timedelta(days=31))
        with patch('content_factory.hosted_retirement.transaction.atomic', return_value=MagicMock()):
            result = retirement.retire_site(self.site)
        self.assertTrue(result['grace_expired'])
        self.assertEqual(result['status'], 'retired')

    def _article(self):
        publication = SimpleNamespace(pk='pub', run_id='source', written_article_id=1,
            written_article=SimpleNamespace(source_run_id='source'), requested_by=SimpleNamespace(pk=7), slug='same-slug')
        self.site.publications.filter.return_value.select_related.return_value.order_by.return_value = [publication]
        return publication

    def test_repository_delivery_preserves_slug_and_is_reserved_once(self):
        self._article()
        self.destination.return_value = (SimpleNamespace(), SimpleNamespace(), None)
        child = SimpleNamespace(run_id='publish', run_request={'retirement_slug': 'same-slug'})
        with patch('workflow_runs.models.ContentFactoryRun.objects.filter') as runs, patch(
            'content_factory.publish_reviewed_draft.start_publish', return_value=(child, True)) as publish, patch(
            'content_factory.models.WrittenArticle.objects.filter') as written:
            runs.return_value.first.return_value = SimpleNamespace(run_id='source')
            written.return_value.exists.return_value = False
            receipt = retirement.retire_site(self.site)
            self.assertEqual(publish.call_args.kwargs['retirement_slug'], 'same-slug')
            self.assertEqual(receipt['articles'][0]['url'], 'https://example.test/blog/same-slug/')
            self.remote.assert_not_called()
            retirement.retire_site(self.site)
            publish.assert_called_once()

    def test_no_repo_delivers_copy_ready_and_an_alert_before_waiting(self):
        self._article()
        with patch('workflow_runs.models.ContentFactoryRun.objects.filter') as runs, patch(
            'content_factory.publish_reviewed_draft.OrganizationContentConfig.objects.filter') as configs, patch(
            'content_factory.channel_switch.switch_to_copy_ready', return_value={'channel': 'copy_ready', 'dispatch_state': 'accepted'}) as switch:
            runs.return_value.first.return_value = SimpleNamespace(run_id='source')
            configs.return_value.first.return_value = None
            receipt = retirement.retire_site(self.site)
            self.assertEqual(receipt['articles'][0]['delivery'], 'copy_ready')
            self.assertEqual(receipt['alert_id'], 'alert')
            self.assertEqual(switch.call_args.kwargs['reason'], 'hosting_retired')
            self.remote.assert_not_called()

    def test_real_run_freezes_before_publication_inventory(self):
        original = self.site.publications.filter.return_value
        def inventory(*args, **kwargs):
            self.assertEqual(self.site.state, 'paused')
            return original
        self.site.publications.filter.side_effect = inventory
        retirement.retire_site(self.site)
        self.assertTrue(self.site.profile['retirement']['inventory_complete'])

    def test_historical_hosted_live_status_cannot_stand_in_for_repo_merge(self):
        article = {'delivery': 'repository', 'publish_run_id': 'publish', 'written_article_id': 1, 'slug': 'same-slug'}
        with patch('workflow_runs.models.ContentFactoryRun.objects.filter') as runs, patch(
            'content_factory.models.WrittenArticle.objects.filter') as written:
            child = SimpleNamespace(result={}, run_request={})
            runs.return_value.first.return_value = child
            written.return_value.exists.return_value = True
            self.assertFalse(retirement._repository_delivery_merged(self.site, article))
            child.result = {'merge_status': 'merged', 'merge_v1': {'merged_at': 'now', 'head_sha': 'a'*40},
                'publish_pr_v1': {'head_sha': 'a'*40}}
            self.assertTrue(retirement._repository_delivery_merged(self.site, article))
