"""CMS bindings cannot borrow GitHub authority or expose private credential rows."""

from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_connection_authority as authority
from . import tests_cms_connection_contract_unit as credential_fixtures
from .cms_connection_contract import seal_credential
from .website_contract import WebsiteAuthorityError, validate_authority
from .website_connections import authority_guard, contract_for
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .models import OrganizationContentConfig
from organizations.models import Organization
from .website_views import WebsiteConnectionOperationView


@override_settings(ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,ARTICLES_CMS_CHANNEL_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=credential_fixtures.json.dumps({'test':credential_fixtures.TEST_KEY}),
    CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID='test')
class CmsAuthorityTests(SimpleTestCase):
    def test_cms_credentials_are_removed_from_generic_worker_and_evidence_payloads(self):
        from .website_contract import sanitized_evidence
        payload = {key: 'synthetic-private-credential' for key in ('secret', 'sealed_credential', 'application_password', 'integration_key', 'cms_credential')}
        payload['nested'] = [{'Secret': 'synthetic-private-credential', 'title': 'Article'}]
        self.assertEqual(sanitized_evidence(payload), {'nested': [{'title': 'Article'}]})

    def setUp(self):
        self.user=SimpleNamespace(pk=4,is_active=True)
        self.org=Organization(pk=7,domain='fixture.test')
        self.company=SimpleNamespace(pk=uuid4(),organization_id=7,domain='fixture.test',profile_id=3)
        self.profile=SimpleNamespace(pk=3,user_id=4,role='founder')
        self.context=SimpleNamespace(company=self.company,profile=self.profile,organization=self.org)
        self.connection=WebsiteConnection(organization=self.org,authorized_by_id=4,
            github_repo='',repository_id=None,site_url='https://fixture.test/')
        self.config=OrganizationContentConfig(pk=9,organization=self.org,website_connection=self.connection)
        self.binding={'schema_version':1,'channel':'cms','platform':'wordpress','company_id':str(self.company.pk),
            'organization_id':'7','owner_user_id':'4','connection_id':str(self.connection.pk),'generation':1,
            'public_origin':self.connection.site_url,'api_origin':'https://fixture.test/'}
        self.private=WebsiteConnectionOperation(connection=self.connection,generation=1,action='channel-credential',state='completed',
            payload={'schema_version':1,'binding':self.binding,'sealed_credential':seal_credential(self.binding,
                username='fixture-owner',secret='synthetic-application-password')})
        self.connection.capabilities={'_cms_binding':self.binding,'_cms_credential_operation_id':str(self.private.pk)}
        self.rows=Mock();self.rows.filter.return_value.first.return_value=self.private
        self.relation=patch.object(WebsiteConnection,'operations',self.rows);self.relation.start();self.addCleanup(self.relation.stop)

    def read(self):
        return authority.current_credential(self.context,self.config,self.connection,user=self.user)

    def test_current_exact_private_binding_can_be_decrypted_without_github(self):
        binding,credential=self.read()
        self.assertEqual(binding,self.binding);self.assertEqual(credential.secret,'synthetic-application-password')
        self.rows.filter.assert_called_once_with(pk=str(self.private.pk),action='channel-credential',generation=1,state='completed')

    def test_disabled_foreign_or_changed_provider_binding_is_denied_before_credential_read(self):
        for changes in ({'generation':2},{'github_repo':'owner/site'},{'repository_id':123},
                        {'installation_id':'4'},{'branch':'main'},{'app_root':'apps/site'},
                        {'state':'disconnected'},{'authorized_by_id':99},{'site_url':'https://foreign.test/'},
                        {'organization_id':99}):
            original={key:getattr(self.connection,key) for key in changes}
            for key,value in changes.items():setattr(self.connection,key,value)
            with self.subTest(changes=changes),self.assertRaises(WebsiteAuthorityError):self.read()
            for key,value in original.items():setattr(self.connection,key,value)
        self.rows.filter.assert_not_called()
        with override_settings(ARTICLES_CMS_CHANNEL_ENABLED=False),self.assertRaises(WebsiteAuthorityError):self.read()
        with override_settings(ARTICLES_PUBLISHING_CHANNELS_ENABLED=False),self.assertRaises(WebsiteAuthorityError):self.read()

    def test_owner_transfer_other_company_and_stale_selected_connection_do_not_borrow_key(self):
        for target,key,value in ((self.profile,'user_id',99),(self.profile,'role','investor'),
                                 (self.company,'profile_id',99),(self.company,'organization_id',99),
                                 (self.user,'is_active',False),(self.org,'domain','foreign.test'),
                                 (self.config,'website_connection_id',uuid4())):
            before=getattr(target,key);setattr(target,key,value)
            with self.subTest(key=key),self.assertRaises(WebsiteAuthorityError):self.read()
            setattr(target,key,before)
        self.rows.filter.assert_not_called()

    def test_missing_ciphertext_reference_and_wrong_private_rows_never_use_a_fallback(self):
        for value in (None,'other',''):
            self.connection.capabilities['_cms_credential_operation_id']=value
            with self.assertRaises(WebsiteAuthorityError):self.read()
        self.connection.capabilities['_cms_credential_operation_id']=str(self.private.pk)
        for changes in ({'state':'cancelled'},{'generation':2},{'action':'workflow'},
                        {'payload':{'schema_version':1,'binding':self.binding,'sealed_credential':'plaintext-must-not-load'}}):
            before={key:getattr(self.private,key) for key in changes}
            for key,value in changes.items():setattr(self.private,key,value)
            with self.subTest(changes=tuple(changes)),self.assertRaises(WebsiteAuthorityError):self.read()
            for key,value in before.items():setattr(self.private,key,value)

    def test_all_git_actions_refuse_even_malformed_cms_discriminators(self):
        from .website_contract import REPO_ACTIONS
        for marker in (self.binding,{},None,'malformed'):
            self.connection.capabilities={'_cms_binding':marker,'publishingReady':True}
            for action in REPO_ACTIONS-{'portable'}:
                with self.subTest(action=action),self.assertRaises(WebsiteAuthorityError):
                    validate_authority(self.connection,contract_for(self.connection),action=action)
        self.assertEqual(validate_authority(self.connection,contract_for(self.connection),action='portable')['connection_generation'],1)

    def test_git_guard_blocks_before_provider_reads_even_for_cleanup_actions(self):
        manager=Mock();manager.filter.return_value.first.return_value=self.connection
        with patch.object(WebsiteConnection.objects,'select_related',return_value=manager), \
                patch('content_factory.website_connections.verify_repository_head',side_effect=AssertionError('No GitHub call')):
            for action in ('read','scan','setup','publish','worker_cleanup','restoration','cancel_operation'):
                with self.subTest(action=action),self.assertRaises(WebsiteAuthorityError):
                    with authority_guard(contract_for(self.connection),action=action):pass

    def test_private_operation_is_not_available_through_owner_receipt_endpoint(self):
        request=SimpleNamespace(query_params={},data={},user=self.user)
        with patch('content_factory.website_views._context',return_value=(self.context,self.config,None)), \
                patch.object(WebsiteConnectionOperation.objects,'filter') as rows:
            rows.return_value.first.return_value=self.private
            response=WebsiteConnectionOperationView().get(request,self.private.pk)
        self.assertEqual(response.status_code,404)
        self.assertNotIn('sealed_credential',str(response.data))
