"""Public platform hints cannot manufacture access, consent or live evidence."""

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from django.test import SimpleTestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from organizations.models import Organization
from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection
from .website_contract import WebsiteAuthorityError
from . import website_channel_observation as public
from . import website_publishing_channels as channels
from . import website_views as views
# Load lazy transaction decorators before an individual test replaces atomic.
from . import vibe_marketing_views as marketing
from .tests_site_fingerprint_unit import example, stamp, scope


class PublicChannelObservationTests(SimpleTestCase):
    def test_minimized_observed_public_platform_fixtures(self):
        fixtures = json.loads((Path(__file__).parent / "fixtures/publishing_channels/public-platforms-v1.json").read_text())["fixtures"]
        self.assertEqual({row["platform"] for row in fixtures}, channels.PLATFORMS - {"unknown"})
        for row in fixtures:
            with self.subTest(platform=row["platform"]):
                result = public.classify_prefix(row["html"].encode(), link_header=row.get("link_header", ""))
                self.assertEqual(result["platform"], row["platform"])
                recommendation = channels.recommend(result)
                expected = "hosted" if row["platform"] == "squarespace" else "cms"
                self.assertEqual(recommendation["recommended_channel"], expected)
                self.assertTrue({"hosted", "portable"} <= set(recommendation["options"]))
                self.assertFalse({"publishingReady", "access", "credentials", "html", "headers"} & result.keys())

    def test_text_scripts_comments_inert_markup_and_host_suffixes_do_not_select_cms(self):
        for html in (
            '<html><head><!-- <meta name="generator" content="WordPress"> --></head><body>Ghost Webflow Shopify Wix</body></html>',
            '<head><script>"<meta name=generator content=WordPress>"; publish()</script></head>',
            '<head><template><meta name="generator" content="Ghost"></template><noscript><meta name="generator" content="WordPress"></noscript></head>',
            '<head><script src="https://cdn.shopify.com.attacker.test/x.js"></script><link href="https://cdn.shopify.com@attacker.test/" rel="stylesheet"></head>',
            '<head><meta name="generator" name="other" content="WordPress"><meta name="generator" content="Ghostwritten"></head>',
            '<body><meta name="generator" content="WordPress"><a href="/wp-json/">WordPress</a></body>',
        ):
            with self.subTest(html=html):
                result = public.classify_prefix(html.encode())
                self.assertEqual(result["platform"], "unknown")
                self.assertEqual(channels.recommend(result)["recommended_channel"], "hosted")

    def test_conflicting_generators_remain_unknown_and_api_relation_is_not_followed(self):
        result = public.classify_prefix(b'<head><meta name="generator" content="Ghost 6"><meta name="generator" content="WordPress 7"></head>')
        self.assertEqual(result["platform"], "unknown")
        result = public.classify_prefix(b'<head><link rel="https://api.w.org/" href="http://127.0.0.1/private?token=not-stored"></head>')
        self.assertEqual(result["signals"], ["wordpress:api_relation"])
        self.assertNotIn("127.0.0.1", json.dumps(result))
        self.assertEqual(public.classify_prefix(b'\xff\xfe<html>')['platform'], 'unknown')
        with self.assertRaises(ValueError):
            public.classify_prefix(b'x' * (public.MAX_PREFIX_BYTES + 1))

    def fake_response(self, *, status=200, headers=None, body=b'<head><meta name="generator" content="Ghost"></head>'):
        response = Mock(status=status)
        headers = {"Content-Type": "text/html", **(headers or {})}
        response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
        response.read.return_value = body
        return response

    def test_prefix_is_bounded_and_requests_have_no_credentials(self):
        response = self.fake_response(body=b'x' * 65537)
        client = Mock(); client.getresponse.return_value = response
        with patch.object(public, "public_addresses", return_value=(urlsplit("https://fixture.test/"), ["8.8.8.8"])) as addresses, \
                patch.object(public, "PinnedHTTPSConnection", return_value=client):
            result = public.observe_homepage("fixture.test")
        self.assertEqual(result["coverage"], "prefix")
        self.assertEqual(response.read.call_args.args, (65537,))
        self.assertEqual(set(client.request.call_args.kwargs["headers"]), {"Host", "Connection", "Accept", "Accept-Encoding"})
        self.assertEqual(addresses.call_args.args, ("https://fixture.test/", "fixture.test"))
        response.close.assert_called_once(); client.close.assert_called_once()

    def test_real_public_address_validation_rejects_private_mixed_dns_and_cross_site_redirects(self):
        for rows in ([('127.0.0.1', 443)], [('8.8.8.8',443), ('10.1.2.3',443)]):
            with patch('content_factory.website_live_fetch.socket.getaddrinfo', return_value=[(None,None,None,None,row) for row in rows]), \
                    patch.object(public, 'PinnedHTTPSConnection') as client:
                self.assertEqual(public.observe_homepage('fixture.test')['status'], 'unavailable')
                client.assert_not_called()
        for location in ('https://attacker.test/', 'http://fixture.test/', 'https://fixture.test/private',
                         'https://user:password@fixture.test/', '/?access_token=private'):
            client = Mock(); client.getresponse.return_value = self.fake_response(status=302, headers={'Location':location})
            with patch('content_factory.website_live_fetch.socket.getaddrinfo', return_value=[(None,None,None,None,('8.8.8.8',443))]), \
                    patch.object(public, 'PinnedHTTPSConnection', return_value=client) as constructor:
                self.assertEqual(public.observe_homepage('fixture.test')['status'], 'unavailable')
                self.assertEqual(constructor.call_count,1)

    def test_invalid_domains_status_encoding_and_timeouts_remain_unknown(self):
        for domain in ('https://fixture.test/', 'user:pass@fixture.test', 'localhost', 'fixture.test:443', '//fixture.test', 'fixture.test\n'):
            with patch.object(public, 'PinnedHTTPSConnection') as client:
                self.assertEqual(public.observe_homepage(domain)['status'], 'unavailable')
                client.assert_not_called()
        for response in (self.fake_response(status=503), self.fake_response(headers={'Content-Encoding':'gzip'}),
                         self.fake_response(headers={'Content-Type':'application/json'})):
            client = Mock(); client.getresponse.return_value = response
            with patch.object(public,'public_addresses',return_value=(urlsplit('https://fixture.test/'),['8.8.8.8'])), \
                    patch.object(public,'PinnedHTTPSConnection',return_value=client):
                self.assertEqual(public.observe_homepage('fixture.test')['status'],'unavailable')
                response.read.assert_not_called()
        with patch.object(public, 'public_addresses', side_effect=TimeoutError):
            self.assertEqual(public.observe_homepage('fixture.test')['status'],'unavailable')

    @override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=True)
    def test_current_known_git_facts_take_precedence_but_foreign_or_manual_facts_do_not(self):
        facts = example()
        observation = public.classify_prefix(b'<head><meta name="generator" content="Ghost"></head>')
        result = channels.recommend(observation, fingerprint=facts, repository_scope=scope(facts))
        self.assertEqual(result['recommended_channel'],'git')
        for changes in ({'branch':'other'}, {'source_sha':'b'*40}, {'app_root':'apps/other'}, {'github_repo':'other/site'}):
            self.assertEqual(channels.recommend(observation,fingerprint=facts,repository_scope={**scope(facts),**changes})['recommended_channel'],'cms')
        for unsupported in ('manual_adapter_required','future_adapter',{'name':'astro'}):
            facts['fields']['adapter']['value']=unsupported
            self.assertEqual(channels.recommend(observation,fingerprint=stamp(facts),repository_scope=scope(facts))['recommended_channel'],'cms')
        with override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=False):
            self.assertEqual(channels.recommend(observation,fingerprint=example(),repository_scope=scope(example()))['recommended_channel'],'cms')


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True)
class PublishingChannelChoiceTests(SimpleTestCase):
    def setUp(self):
        self.org = Organization(pk=7, domain='fixture.test')
        self.profile = VibeRaisingProfile(pk=3,role='founder')
        self.company = VibeRaisingCompany(profile=self.profile,organization=self.org,domain='fixture.test')
        self.context = SimpleNamespace(organization=self.org,company=self.company,profile=self.profile)
        self.config = OrganizationContentConfig(pk=9,organization=self.org)
        self.config.refresh_from_db = Mock(); self.config.save = Mock()
        self.org.refresh_from_db = Mock(); self.profile.refresh_from_db = Mock()
        self.patches = [patch('django.db.transaction.atomic', side_effect=lambda: nullcontext()),
            patch.object(Organization.objects,'select_for_update'),patch.object(VibeRaisingCompany.objects,'select_for_update'),
            patch.object(channels,'observe_homepage',return_value=public.classify_prefix(b'<head><meta name="generator" content="Ghost"></head>'))]
        values = [p.start() for p in self.patches]
        values[1].return_value.get.return_value = self.org
        values[2].return_value.get.return_value = self.company
        self.observe = values[3]
        for p in self.patches:self.addCleanup(p.stop)

    def update(self, *, selected=None, revision=None, expected_scope=None):
        revision = channels._revision(self.config) if revision is None else revision
        return channels.update_choice(self.context,self.config,expected_revision=revision,
            expected_scope=expected_scope or channels.scope_for(self.context,self.config),selected=selected)

    def test_no_github_required_and_owner_selection_never_creates_readiness(self):
        inspected = self.update()
        self.assertEqual(inspected['recommendation']['recommended_channel'],'cms')
        self.assertIsNone(inspected['selection'])
        self.observe.reset_mock()
        selected = self.update(selected='portable')
        self.observe.assert_not_called()
        self.assertEqual(selected['selection'],{'channel':'portable','platform':'unknown'})
        self.assertEqual(channels.projection(self.context,self.config),selected)
        self.assertFalse(self.config.articles_scaffolded)
        self.assertIsNone(self.config.website_connection)
        self.config.save.assert_called_with(update_fields=['article_system','updated_at'])

    def test_company_scope_revision_and_disabled_requests_make_no_public_request(self):
        original = channels.scope_for(self.context,self.config)
        for supplied in ({**original,'company_id':'other'}, {**original,'domain':'other.test'}, None):
            with self.assertRaises(WebsiteAuthorityError):
                channels.update_choice(self.context,self.config,expected_scope=supplied,expected_revision=0)
        for revision in (True,'0',1,-1):
            with self.assertRaises(WebsiteAuthorityError):self.update(revision=revision)
        with override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=False):
            with self.assertRaises(WebsiteAuthorityError):self.update()
            self.assertIsNone(channels.projection(self.context,self.config))
        self.observe.assert_not_called()

    def test_late_observation_cannot_overwrite_owner_selection(self):
        self.update()
        newer = self.update(selected='portable')
        before = deepcopy(self.config.article_system)
        def race(_domain):
            self.config.article_system['publishing_channel']={**newer,'revision':newer['revision']+1,
                'selection':{'channel':'hosted','platform':'unknown'}}
            return public.classify_prefix(b'<head><meta name="generator" content="WordPress"></head>')
        self.observe.side_effect=race
        with self.assertRaises(WebsiteAuthorityError):self.update()
        self.assertEqual(self.config.article_system['publishing_channel']['selection']['channel'],'hosted')
        self.assertEqual(before['publishing_channel']['selection']['channel'],'portable')

    def test_scope_change_or_owner_transfer_during_http_rejects_save(self):
        for change in ('domain','owner','generation','source','configuration'):
            self.config.website_connection=WebsiteConnection(organization=self.org,github_repo='fixture/site',generation=2)
            original_scope=channels.scope_for(self.context,self.config)
            def race(_domain):
                if change=='domain':self.org.domain='changed.test'
                elif change=='owner':self.company.profile_id=999
                elif change=='generation':self.config.website_connection.generation+=1
                elif change=='configuration':self.config.website_connection.configuration_version+=1
                else:self.config.last_scanned_sha='b'*40
                return public.classify_prefix(b'<html/>')
            self.observe.side_effect=race
            with self.assertRaises(WebsiteAuthorityError):self.update(expected_scope=original_scope)
            self.config.save.assert_not_called()
            self.org.domain='fixture.test';self.company.profile=self.profile
            self.config.last_scanned_sha=''

    def test_status_projection_is_local_and_normal_scans_preserve_owner_choice(self):
        self.update();self.update(selected='portable')
        self.observe.reset_mock()
        self.config.last_scanned_sha='b'*40
        result=channels.projection(self.context,self.config)
        self.assertEqual(result['revision'],2)
        self.assertEqual(result['selection']['channel'],'portable')
        self.assertIsNotNone(result['recommendation'])
        self.observe.assert_not_called()
        self.org.domain=self.company.domain='changed.test'
        stale=channels.projection(self.context,self.config)
        self.assertEqual(stale['revision'],2)
        self.assertIsNone(stale['recommendation']);self.assertIsNone(stale['selection'])
        self.update(revision=result['revision'])
        self.assertEqual(channels.projection(self.context,self.config)['revision'],3)

    def test_untrusted_normalization_and_merges_cannot_replace_existing_owner_record(self):
        from .article_system import normalize_article_system, merge_article_system, resolve_article_system
        from .article_setup_reset import carry_reset_markers
        self.update();selected=self.update(selected='portable')
        forged={**selected,'selection':{'channel':'cms','platform':'wordpress'}}
        incoming={'state':'existing','confidence':'high','publishing_channel':forged}
        self.assertNotIn('publishing_channel',normalize_article_system(incoming))
        self.assertNotIn('publishing_channel',merge_article_system({},incoming))
        self.assertEqual(merge_article_system(self.config.article_system,incoming)['publishing_channel'],selected)
        self.assertEqual(carry_reset_markers(self.config.article_system,incoming)['publishing_channel'],selected)
        self.assertEqual(resolve_article_system(self.config)['publishing_channel'],selected)

    def test_invalid_or_unoffered_choice_and_malformed_records_are_rejected(self):
        self.update()
        for selected in ('git','unexpected',[],True):
            with self.assertRaises(WebsiteAuthorityError):self.update(selected=selected)
        good=deepcopy(self.config.article_system)
        for change in ({'schema_version':True},{'observed_at':'bad'},{'revision':True},{'secret':'must-not-copy'}):
            candidate={**good['publishing_channel'],**change}
            self.assertIsNone(channels.saved_record({'publishing_channel':candidate}))
        self.assertIsNone(channels.saved_record({'publishing_channel':{'selection':'malformed'}}))

    def request(self, data):
        request=APIRequestFactory().post('/website-connection/inspect-channel',
            {"company_id":str(self.company.pk),**data},format='json')
        force_authenticate(request,user=get_user_model()(pk=4))
        return request

    def test_owner_http_inspection_and_selection_use_the_existing_company_boundary(self):
        with patch.object(marketing,'_resolve_context_or_response',return_value=(self.context,None)), \
                patch.object(marketing,'_get_config',return_value=self.config), \
                patch.object(marketing,'_article_capabilities_for_context',side_effect=AssertionError('No GitHub probe')):
            request=self.request({'channel_scope':channels.scope_for(self.context,self.config),'channel_revision':0})
            inspected=views.WebsiteConnectionActionView.as_view()(request,action='inspect-channel')
            self.assertEqual(inspected.status_code,200)
            record=inspected.data['publishingChannel']
            selected=views.WebsiteConnectionActionView.as_view()(self.request({'channel_scope':record['scope'],
                'channel_revision':record['revision'],'channel':'portable'}),action='select-channel')
            self.assertEqual(selected.status_code,200)
            self.assertEqual(selected.data['publishingChannel']['selection']['channel'],'portable')
            self.assertEqual(self.observe.call_count,1)
            stale=views.WebsiteConnectionActionView.as_view()(self.request({'channel_scope':record['scope'],
                'channel_revision':record['revision']}),action='inspect-channel')
            self.assertEqual(stale.status_code,409)
            with patch.object(marketing,'_resolve_context_or_response',return_value=(None,Response({'detail':'Forbidden'},status=403))):
                denied=views.WebsiteConnectionActionView.as_view()(self.request({}),action='inspect-channel')
                self.assertEqual(denied.status_code,403)
            self.assertEqual(self.observe.call_count,1)

    def test_bind_observes_after_binding_and_default_off_preserves_response(self):
        connection=WebsiteConnection(organization=self.org,github_repo='fixture/site',generation=2)
        def bind(*args,**kwargs):
            self.config.website_connection=connection
            return connection
        with patch.object(views,'_context',return_value=(self.context,self.config,None)), \
                patch.object(views,'bind_website',side_effect=bind), \
                patch.object(views,'summary_for',return_value={'state':'connected'}):
            with override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=False):
                result=views.WebsiteConnectionView.as_view()(self.request({'github_repo':'fixture/site'}))
                self.assertEqual(result.status_code,200)
                self.assertNotIn('publishingChannel',result.data)
                self.observe.assert_not_called()
            result=views.WebsiteConnectionView.as_view()(self.request({'github_repo':'fixture/site'}))
            self.assertEqual(result.status_code,200)
            self.assertIsNotNone(result.data['publishingChannel']['recommendation'])
            self.assertEqual(self.observe.call_count,1)

    def test_status_addition_never_fetches_or_schedules(self):
        from .website_status import add_status
        self.update();self.observe.reset_mock()
        with override_settings(ARTICLES_WEBSITE_STATUS_V1_ENABLED=False):
            payload=add_status({},self.context,self.config,journey={})
        self.assertEqual(payload['publishingChannel']['revision'],1)
        self.assertNotIn('websiteStatus',payload)
        self.observe.assert_not_called()

    @override_settings(ARTICLES_SITE_FINGERPRINT_ENABLED=True)
    def test_fresh_scan_recommends_git_without_http_or_replacing_owner_preference(self):
        facts=example()
        self.config.website_connection=WebsiteConnection(organization=self.org,github_repo=facts['github_repo'],
            branch=facts['branch'],app_root=facts['app_root'])
        self.update();selected=self.update(selected='portable')
        self.config.last_scanned_sha=facts['source_sha']
        self.observe.reset_mock()
        channels.refresh_repository_hint(self.config.website_connection,self.config,source_sha=facts['source_sha'],inventory={'site_fingerprint':facts})
        result=channels.projection(self.context,self.config)
        self.assertEqual(result['recommendation']['recommended_channel'],'git')
        self.assertEqual(result['selection'],selected['selection'])
        self.assertEqual(result['observed_at'],selected['observed_at'])
        self.assertEqual(result['revision'],3)
        self.observe.assert_not_called()
        self.config.website_connection.configuration_version+=1
        self.assertEqual(channels.projection(self.context,self.config)['selection'],selected['selection'])
        self.config.website_connection.generation+=1
        before=deepcopy(self.config.article_system)
        channels.refresh_repository_hint(self.config.website_connection,self.config,source_sha=facts['source_sha'],inventory={'site_fingerprint':facts})
        self.assertEqual(self.config.article_system,before)
