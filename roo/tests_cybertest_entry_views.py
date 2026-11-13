"""No-database endpoint contracts; real identity/storage is explicitly mocked."""
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from django.core.cache import cache
from rest_framework.test import APIRequestFactory, force_authenticate
from roo.cybertest_entry import EntryError
from roo import cybertest_entry_views as views
from roo.cybertest_checkout import _payer
from roo import cybertest_checkout as checkout
from roo.cybertest_policy import CheckoutError

COMPANY='10000000-0000-4000-8000-000000000001'
WORKSPACE='20000000-0000-4000-8000-000000000001'


@override_settings(DEBUG=False, APP_ENV='production',
    CYBERTEST_ENTRY_PRODUCTION_ENABLED=True, CYBERTEST_ENTRY_LOCAL_ENABLED=False,
    CYBERTEST_ENTRY_STAGING_ENABLED=False, CYBERTEST_APP_ORIGIN='https://chat.mlai.au',
    CYBERTEST_BASE_PATH='/cybertest', CYBERTEST_BILLING_MODE='live',
    CYBERTEST_LIVE_BILLING_ENABLED=True, CYBERTEST_PHASED_SCANS_ENABLED=True,
    CYBERTEST_BILLING_API_KEY='p'*64, CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
    CYBERTEST_PRODUCTION_COMPANY_IDS=[COMPANY],
    CACHES={'default': {'BACKEND':'django.core.cache.backends.redis.RedisCache'}})
class ProductionEntryViewTests(SimpleTestCase):
    def test_production_origin_and_metadata_match_the_production_browser(self):
        self.assertEqual(views.local_origin(), 'https://chat.mlai.au')
        self.assertEqual(views.entry_metadata(), {'local_test': False, 'entry_mode': 'production'})

    def test_login_remains_available_with_new_billing_disabled(self):
        with self.settings(CYBERTEST_BILLING_MODE='disabled', CYBERTEST_LIVE_BILLING_ENABLED=False):
            self.assertEqual(views.local_origin(), 'https://chat.mlai.au')

    def test_incompatible_runtime_configuration_never_issues_authority(self):
        for values in [dict(DEBUG=True), dict(APP_ENV='chat-staging'),
                       dict(CYBERTEST_ENTRY_STAGING_ENABLED=True),
                       dict(CYBERTEST_ENTRY_LOCAL_ENABLED=True),
                       dict(CYBERTEST_BILLING_MODE='test'),
                       dict(CYBERTEST_LIVE_BILLING_ENABLED=False),
                       dict(CYBERTEST_PHASED_SCANS_ENABLED=False),
                       dict(CYBERTEST_PERSISTENT_BINDINGS_ENABLED=False),
                       dict(CYBERTEST_PRODUCTION_COMPANY_IDS=[]),
                       dict(CYBERTEST_APP_ORIGIN='https://chat-staging.mlai.au'),
                       dict(CYBERTEST_BASE_PATH=''),
                       dict(CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})]:
            with self.subTest(fields=list(values)), self.settings(**values), self.assertRaises(EntryError):
                views.local_origin()

    def test_non_enabled_company_is_denied_before_session_lookup(self):
        with patch.object(views.CommunityChatAccountSession.objects, 'select_related') as sessions:
            with self.assertRaises(EntryError) as caught:
                views.authorize('session', '30000000-0000-4000-8000-000000000001')
            self.assertEqual(caught.exception.code, 'COMPANY_UNAVAILABLE')
            sessions.assert_not_called()

    def test_allowlisted_company_still_requires_session_owner_and_active_binding(self):
        user = SimpleNamespace(pk='user-1')
        session = SimpleNamespace(user=user, user_id=user.pk)
        company = SimpleNamespace(pk=COMPANY, name='Synthetic company', organization_id=None)
        with patch.object(views.CommunityChatAccountSession.objects, 'select_related') as sessions, \
             patch.object(views.VibeRaisingCompany.objects, 'select_related') as companies, \
             patch.object(views, '_valid_session', return_value=True), \
             patch.object(views, '_validate_device_owner') as device, \
             patch.object(views, 'require_community_access') as access, \
             patch.object(views, 'workspace_for_company', return_value=WORKSPACE) as workspace:
            sessions.return_value.filter.return_value.first.return_value = session
            companies.return_value.filter.return_value.first.return_value = company
            result = views.authorize('session', COMPANY)
            self.assertEqual(result['workspace_id'], WORKSPACE)
            companies.return_value.filter.assert_called_once_with(pk=COMPANY, profile__user=user)
            device.assert_called_once_with(session)
            access.assert_called_once_with(user)
            workspace.assert_called_once_with(company)
            companies.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(EntryError) as caught:
                views.authorize('session', COMPANY)
            self.assertEqual(caught.exception.code, 'COMPANY_UNAVAILABLE')

    @override_settings(CYBERTEST_PRODUCTION_ALL_COMPANIES=True, CYBERTEST_PRODUCTION_COMPANY_IDS=[])
    def test_general_access_keeps_owner_checks_before_workspace_creation(self):
        self.assertEqual(views.local_origin(), 'https://chat.mlai.au')
        user = SimpleNamespace(pk='owner')
        session = SimpleNamespace(user=user, user_id=user.pk)
        company = SimpleNamespace(pk=COMPANY, name='New company', organization_id=None)
        with patch.object(views.CommunityChatAccountSession.objects, 'select_related') as sessions, \
             patch.object(views.VibeRaisingCompany.objects, 'select_related') as companies, \
             patch.object(views, '_valid_session', return_value=True), \
             patch.object(views, '_validate_device_owner'), \
             patch.object(views, 'require_community_access'), \
             patch.object(views, 'workspace_for_company', return_value=WORKSPACE) as workspace:
            sessions.return_value.filter.return_value.first.return_value = session
            companies.return_value.filter.return_value.first.return_value = None
            with self.assertRaises(EntryError): views.authorize('session', COMPANY)
            workspace.assert_not_called()
            companies.return_value.filter.return_value.first.return_value = company
            result = views.authorize('session', COMPANY)
            self.assertTrue(result['workspace_provisioning'])
            companies.return_value.filter.assert_called_with(pk=COMPANY, profile__user=user)
            workspace.assert_called_once_with(company)

    @override_settings(CYBERTEST_PRODUCTION_ALL_COMPANIES=True)
    def test_existing_workspace_lookup_preserves_revocation_without_reprovisioning(self):
        company = SimpleNamespace(pk=COMPANY)
        row = SimpleNamespace(workspace_id=WORKSPACE, is_active=True, verified_at='verified')
        with patch.object(views.CybertestCompanyBinding.objects, 'filter') as rows, \
             patch.object(views.CybertestCompanyBinding.objects, 'get_or_create') as create:
            rows.return_value.first.return_value = row
            self.assertEqual(views.workspace_for_company(company), WORKSPACE)
            row.is_active = False
            with self.assertRaises(EntryError): views.workspace_for_company(company)
            create.assert_not_called()

    @override_settings(CYBERTEST_PRODUCTION_ALL_COMPANIES=True)
    def test_concurrent_existing_revoked_binding_wins_over_new_defaults(self):
        company = SimpleNamespace(pk=COMPANY, profile=SimpleNamespace(user_id='owner'))
        row = SimpleNamespace(workspace_id=WORKSPACE, is_active=False, verified_at='verified')
        with patch.object(views.CybertestCompanyBinding.objects, 'filter') as rows, \
             patch.object(views.CybertestCompanyBinding.objects, 'get_or_create', return_value=(row, False)) as create:
            rows.return_value.first.return_value = None
            with self.assertRaises(EntryError): views.workspace_for_company(company)
            self.assertEqual(create.call_args.kwargs['defaults']['created_by_id'], 'owner')


@override_settings(DEBUG=True, CYBERTEST_ENTRY_LOCAL_ENABLED=True,
    CYBERTEST_APP_ORIGIN='http://localhost:8787', CYBERTEST_BILLING_MODE='test',
    CYBERTEST_BILLING_API_KEY='k'*32,
    CACHES={'default': {'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class EntryViewTests(SimpleTestCase):
    @override_settings(CYBERTEST_BASE_PATH='/cybertest')
    def test_checkout_returns_to_reserved_path_after_synthetic_confirmation(self):
        from roo import cybertest_views as payments
        request=self.request('post',data={'expected_points':48,'checkout_id':COMPANY,'binding_sha256':'binding'})
        request.COOKIES[payments.COOKIE]=COMPANY+'.'+'t'*43
        with patch.object(payments.checkout,'status',return_value={'binding_sha256':'binding'}), patch.object(payments.checkout,'confirm',return_value={'status':'authorized','test_mode':True}):
            response=payments.ConfirmView.as_view()(request)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data['return_url'],'http://localhost:8787/cybertest/?billing_return='+COMPANY)

    @override_settings(CYBERTEST_BASE_PATH='/cybertest')
    def test_handoff_start_and_callback_use_the_same_reserved_path(self):
        from urllib.parse import urlsplit
        with patch('community_chat.my_startup_views.get_object_or_404',return_value=self.company), patch.object(views,'authorize',return_value=self.identity):
            response=views.EntryView.as_view()(self.request())
            self.assertEqual(response.status_code,200)
            self.assertEqual(urlsplit(response.data['start_url']).path, '/cybertest/auth/mlai/start')
            response=views.AuthorizeView.as_view()(self.request('post',data={'state':'s'*43,'code_challenge':'c'*43}))
            self.assertEqual(response.status_code,200)
            self.assertEqual(urlsplit(response.data['callback_url']).path, '/cybertest/auth/mlai/callback')

    def setUp(self):
        cache.clear()
        self.factory=APIRequestFactory()
        self.user=SimpleNamespace(pk='mlai-user',is_authenticated=True,is_active=True)
        self.company=SimpleNamespace(pk=COMPANY,name='Test company',organization_id=None)
        self.identity=dict(user_id='mlai-user',company_id=COMPANY,workspace_id=WORKSPACE,company_name='Test company')

    def request(self, method='get', path=None, data=None):
        request=getattr(self.factory,method)(path or f'/api/v1/my-startup/cybertest/entry/?company_id={COMPANY}',data=data,format='json')
        force_authenticate(request,user=self.user)
        request.community_chat_account_session=SimpleNamespace(pk='session-1')
        return request

    def test_entry_checks_owned_company_and_does_not_create_checkout(self):
        with patch('community_chat.my_startup_views.get_object_or_404',return_value=self.company) as owned, patch.object(views,'authorize',return_value=self.identity):
            response=views.EntryView.as_view()(self.request())
        self.assertEqual(response.status_code,200)
        self.assertIn('company_id='+COMPANY,response.data['start_url'])
        self.assertEqual(owned.call_args.kwargs['profile__user'],self.user)

    def test_missing_company_is_rejected_before_authority_call(self):
        with patch.object(views,'authorize') as auth:
            response=views.EntryView.as_view()(self.request(path='/api/v1/my-startup/cybertest/entry/'))
        self.assertEqual(response.status_code,400)
        auth.assert_not_called()

    def test_service_exchange_requires_service_key(self):
        response=views.ExchangeView.as_view()(self.factory.post('/exchange/',{},format='json'))
        self.assertEqual(response.status_code,403)

    def inspect_request(self, grant='g'*43, key='k'*32):
        return views.InspectView.as_view()(self.factory.post('/inspect/',
            {'grant':grant},format='json',HTTP_X_CYBERTEST_KEY=key))

    def test_normal_inspection_burst_does_not_spend_checkout_limit(self):
        from roo import cybertest_views as payments
        with patch.object(views,'authority') as authority, patch.object(payments.checkout,'create_quote',return_value={'checkout_id':COMPANY}):
            authority.return_value.inspect.return_value=self.identity
            for _ in range(45):
                self.assertEqual(self.inspect_request().status_code,200)
            with self.settings(COMMUNITY_CHAT_FRONTEND_URL='http://localhost:3000'):
                for _ in range(30):
                    req=self.factory.post('/quotes/',{'checkout_id':COMPANY,'token':'t'*43,'binding':{}},format='json',HTTP_X_CYBERTEST_KEY='k'*32)
                    self.assertEqual(payments.CreateQuoteView.as_view()(req).status_code,200)
                req=self.factory.post('/quotes/',{},format='json',HTTP_X_CYBERTEST_KEY='k'*32)
                self.assertEqual(payments.CreateQuoteView.as_view()(req).status_code,429)

    def test_inspection_is_bounded_per_grant_and_across_service(self):
        with patch.object(views,'authority') as authority:
            authority.return_value.inspect.return_value=self.identity
            for _ in range(120): self.assertEqual(self.inspect_request().status_code,200)
            self.assertEqual(self.inspect_request().status_code,429)
            self.assertEqual(self.inspect_request('h'*43).status_code,200)
            cache.clear()
            for i in range(300):
                self.assertEqual(self.inspect_request(str(i).zfill(43)).status_code,200)
            self.assertEqual(self.inspect_request('z'*43).status_code,429)

    def test_inspection_requires_service_auth_before_rechecking_authority(self):
        with patch.object(views,'authority') as authority:
            self.assertEqual(self.inspect_request(key='wrong').status_code,403)
            authority.assert_not_called()

    def test_authorize_has_no_client_supplied_return_url(self):
        with patch('community_chat.my_startup_views.get_object_or_404',return_value=self.company):
            response=views.AuthorizeView.as_view()(self.request('post',data={'return_url':'https://evil.test'}))
        self.assertEqual(response.status_code,400)

    def test_nonlocal_or_live_configuration_is_rejected(self):
        for overrides in [dict(DEBUG=False),dict(CYBERTEST_APP_ORIGIN='https://cybertest.example'),dict(CYBERTEST_BILLING_MODE='live'),dict(CYBERTEST_ENTRY_LOCAL_ENABLED=False)]:
            with self.settings(**overrides),self.assertRaises(EntryError): views.local_origin()

    def test_mlai_bound_quote_rejects_other_payer(self):
        receipt=SimpleNamespace(binding={'mlai_user_id':'another-user'},user_id=None)
        with self.assertRaises(CheckoutError): _payer(receipt,self.user)
        receipt.binding['mlai_user_id']='mlai-user'
        _payer(receipt,self.user)

    def test_staging_requires_https_persisted_bindings_shared_cache_and_test_billing(self):
        staged = dict(DEBUG=False, CYBERTEST_ENTRY_LOCAL_ENABLED=False,
            CYBERTEST_ENTRY_STAGING_ENABLED=True, CYBERTEST_APP_ORIGIN='https://audit.stage.example',
            CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True, CYBERTEST_STAGING_COMPANY_IDS=[COMPANY],
            CACHES={'default': {'BACKEND':'django.core.cache.backends.redis.RedisCache','LOCATION':'redis://127.0.0.1:6379/15'}})
        with self.settings(**staged):
            self.assertEqual(views.local_origin(),'https://audit.stage.example')
            for override in [dict(CYBERTEST_APP_ORIGIN='http://audit.stage.example'),
                dict(CYBERTEST_APP_ORIGIN='https://audit.stage.example/path'),
                dict(CYBERTEST_BILLING_MODE='live'), dict(CYBERTEST_PERSISTENT_BINDINGS_ENABLED=False),
                dict(CYBERTEST_STAGING_COMPANY_IDS=[]), dict(CYBERTEST_ENTRY_LOCAL_ENABLED=True),
                dict(CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})]:
                with self.settings(**override), self.assertRaises(EntryError): views.local_origin()

    def test_staging_rejects_nonpilot_company_before_identity_lookup(self):
        with self.settings(DEBUG=False, CYBERTEST_ENTRY_LOCAL_ENABLED=False, CYBERTEST_ENTRY_STAGING_ENABLED=True,
            CYBERTEST_APP_ORIGIN='https://audit.stage.example', CYBERTEST_PERSISTENT_BINDINGS_ENABLED=True,
            CYBERTEST_STAGING_COMPANY_IDS=[COMPANY],
            CACHES={'default':{'BACKEND':'django.core.cache.backends.redis.RedisCache'}}), \
            patch.object(views.CommunityChatAccountSession.objects,'select_related') as sessions:
            with self.assertRaises(EntryError) as caught:
                views.authorize('session-1','30000000-0000-4000-8000-000000000001')
            self.assertEqual(caught.exception.code,'COMPANY_UNAVAILABLE')
            sessions.assert_not_called()

    @override_settings(CYBERTEST_ENTRY_STAGING_ENABLED=True)
    def test_staging_cannot_retrieve_live_receipts_or_enable_live_charges(self):
        receipt = SimpleNamespace(test_mode=False)
        with patch.object(checkout.Receipt.objects,'get',return_value=receipt):
            with self.assertRaises(CheckoutError): checkout._get(COMPANY)
        with self.settings(CYBERTEST_BILLING_MODE='live', CYBERTEST_LIVE_BILLING_ENABLED=True), self.assertRaises(CheckoutError):
            checkout.configured_mode()
