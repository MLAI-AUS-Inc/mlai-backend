"""CMS secrets stay encrypted and cannot move between sites, owners or generations."""

import base64
from copy import deepcopy
import json
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from integrations.fields import encrypt_credential_value
from . import cms_connection_contract as cms

TEST_KEY = base64.urlsafe_b64encode(b'x' * 32).decode()
ROTATED_KEY = base64.urlsafe_b64encode(b'y' * 32).decode()


@override_settings(CONNECTOR_CREDENTIAL_KEYS=json.dumps({'test': TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID='test')
class CmsCredentialContractTests(SimpleTestCase):
    def setUp(self):
        self.binding={'schema_version':1,'channel':'cms','platform':'wordpress',
            'company_id':str(uuid4()),'organization_id':'7','owner_user_id':'4',
            'connection_id':str(uuid4()),'generation':1,
            'public_origin':'https://fixture.test/','api_origin':'https://fixture.test/wordpress/'}
        self.secret='synthetic-application-password'

    def seal(self, **kwargs):
        return cms.seal_credential(self.binding,username='fixture-owner',secret=self.secret,**kwargs)

    def test_context_bound_encryption_and_private_plaintext_repr(self):
        before=deepcopy(self.binding)
        sealed=self.seal()
        self.assertTrue(sealed.startswith('mlai-enc:v1:test:'))
        self.assertNotIn(self.secret,sealed);self.assertNotIn('fixture-owner',sealed)
        credential=cms.open_credential(sealed,binding=self.binding)
        self.assertEqual(credential.secret,self.secret);self.assertEqual(credential.username,'fixture-owner')
        self.assertNotIn(self.secret,repr(credential));self.assertNotIn('fixture-owner',repr(credential))
        self.assertEqual(self.binding,before)

    def test_cross_company_owner_connection_generation_and_origin_replays_are_denied(self):
        sealed=self.seal()
        for changes in ({'company_id':str(uuid4())},{'organization_id':'8'},{'owner_user_id':'99'},
                        {'connection_id':str(uuid4())},{'generation':2},{'api_origin':'https://fixture.test/other/'},
                        {'public_origin':'https://fixture.test/blog/'}):
            with self.subTest(changes=changes),self.assertRaises(cms.CmsContractError):
                cms.open_credential(sealed,binding={**self.binding,**changes})

    def test_typed_closed_bindings_reject_coercions_and_worker_authority_fields(self):
        for changes in ({'schema_version':True},{'generation':True},{'generation':'1'},
                        {'channel':'git'},{'platform':'future'},{'owner_user_id':'4\n'},
                        {'company_id':'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'},{'repository_id':123},
                        {'public_origin':'https://FIXTURE.test/'},{'api_origin':'https://foreign.test/'}):
            with self.subTest(changes=changes),self.assertRaises(cms.CmsContractError):
                cms.validate_binding({**self.binding,**changes})

    def test_credential_urls_encoded_paths_and_unsafe_origins_are_not_accepted(self):
        for origin in ('http://fixture.test/','https://user:password@fixture.test/',
                       'https://@fixture.test/','https://fixture.test/?token=private',
                       'https://fixture.test/#secret','https://fixture.test/../other/',
                       'https://fixture.test/%2e%2e/','https://fixture.test/a//b/',
                       'https://fixture.test:8443/','https://fixture.test/\n','//fixture.test/'):
            with self.subTest(origin=origin),self.assertRaises(cms.CmsContractError):cms.canonical_origin(origin)
        self.assertEqual(cms.canonical_origin('https://fixture.test:443/wordpress'),'https://fixture.test/wordpress/')

    def test_plaintext_legacy_corrupt_envelopes_and_inner_type_confusion_are_rejected(self):
        sealed=self.seal()
        wrong_inner={'schema_version':1,'binding':{**self.binding,'generation':True},'username':'fixture-owner','secret':self.secret}
        for candidate in (self.secret,json.dumps(wrong_inner),'mlai-enc:v1:missing:private',sealed[:-6]+'broken',
                          encrypt_credential_value(json.dumps(wrong_inner))):
            with self.subTest(candidate_kind=type(candidate).__name__),self.assertRaises(cms.CmsContractError) as error:
                cms.open_credential(candidate,binding=self.binding)
            self.assertNotIn(self.secret,str(error.exception))

    def test_rotation_reads_old_key_but_removed_key_never_falls_back_to_plaintext(self):
        sealed=self.seal()
        with override_settings(CONNECTOR_CREDENTIAL_KEYS=json.dumps({'test':TEST_KEY,'rotated':ROTATED_KEY}),
                CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID='rotated'):
            self.assertEqual(cms.open_credential(sealed,binding=self.binding).secret,self.secret)
            self.assertTrue(self.seal().startswith('mlai-enc:v1:rotated:'))
        with override_settings(CONNECTOR_CREDENTIAL_KEYS=json.dumps({'rotated':ROTATED_KEY}),
                CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID='rotated'),self.assertRaises(cms.CmsContractError):
            cms.open_credential(sealed,binding=self.binding)

    def test_ghost_key_is_private_and_cannot_be_reused_as_wordpress_credential(self):
        binding={**self.binding,'platform':'ghost','api_origin':'https://admin.fixture.test/'}
        secret='a'*24+':'+'b'*64
        sealed=cms.seal_credential(binding,secret=secret)
        credential=cms.open_credential(sealed,binding=binding)
        self.assertEqual(credential.secret,secret);self.assertEqual(credential.platform,'ghost')
        self.assertNotIn(secret,repr(credential))
        with self.assertRaises(cms.CmsContractError):cms.open_credential(sealed,binding=self.binding)
        for invalid in ('a'*24+':'+'z'*64,'a'*23+':'+'b'*64,'private'):
            with self.assertRaises(cms.CmsContractError):cms.seal_credential(binding,secret=invalid)

    def test_basic_auth_username_and_secret_header_confusion_is_rejected(self):
        for username,secret in (('user:other',self.secret),('user\r\nother',self.secret),
                                ('user','private\r\nAuthorization: other'),('user','short')):
            with self.assertRaises(cms.CmsContractError):cms.seal_credential(self.binding,username=username,secret=secret)
