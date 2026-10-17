"""Credential-bearing provider transport has no redirect or private-network path."""

import base64
import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase
import jwt

from . import cms_api_transport as api
from .cms_connection_contract import CmsCredential
from . import tests_cms_connection_contract_unit as contract_fixtures


class CmsApiTransportTests(SimpleTestCase):
    def test_plain_permalink_server_is_usable_without_replaying_or_weakening_route_input(self):
        # Default WordPress installations do not route /wp-json/. Model that
        # real server boundary instead of treating every URI as JSON success.
        def server():
            target = urlsplit(self.client.request.call_args.args[1])
            route = parse_qs(target.query).get('rest_route')
            self.response.status = 200 if target.path == '/wordpress/index.php' and route == ['/wp/v2/posts'] else 404
            return self.response
        self.client.getresponse.side_effect = server
        value, _ = self.request(query={'context': 'edit'})
        self.assertEqual(value, [{'id': 4, 'slug': 'article'}])
        self.client.request.assert_called_once()
        self.pin.reset_mock()
        with self.assertRaises(api.CmsApiError):
            self.request(query={'rest_route': 'https://foreign.example.test/'})
        self.pin.assert_not_called()

    def test_only_fixed_public_site_read_can_omit_credential(self):
        for platform, origin, expected in (("wordpress", "https://fixture.test/wordpress/", "/wordpress/index.php?rest_route=%2F"),
                                           ("ghost", "https://admin.fixture.test/", "/ghost/api/admin/site/")):
            binding = {**self.binding, "platform": platform, "api_origin": origin}
            self.response.read.return_value = b'{"site": {}}'
            api.request_json(binding, None, method="GET", resource="site")
            self.assertEqual(self.client.request.call_args.args, ("GET", expected))
            self.assertNotIn("Authorization", self.client.request.call_args.kwargs["headers"])
        with patch.object(api, "public_addresses") as dns:
            for options in ({"method": "GET", "resource": "posts"}, {"method": "POST", "resource": "site"},
                            {"method": "GET", "resource": "site", "identifier": 1}):
                with self.subTest(options=options), self.assertRaises(api.CmsApiError):
                    api.request_json(self.binding, None, **options)
            dns.assert_not_called()

    def setUp(self):
        fixture=contract_fixtures.CmsCredentialContractTests('runTest');fixture.setUp()
        self.binding=fixture.binding
        self.credential=CmsCredential(platform='wordpress',username='fixture-owner',secret='synthetic-application-password')
        self.response=Mock(status=200)
        self.headers={'Content-Type':'application/json','X-WP-TotalPages':'3'}
        self.response.getheader.side_effect=lambda name,default=None:self.headers.get(name,default)
        self.response.read.return_value=b'[{"id":4,"slug":"article"}]'
        self.client=Mock();self.client.getresponse.return_value=self.response
        self.addresses=patch.object(api,'public_addresses',side_effect=lambda url,domain:(urlsplit(url),['8.8.8.8']))
        self.connection=patch.object(api,'PinnedHTTPSConnection',return_value=self.client)
        self.pin=self.addresses.start();self.connect=self.connection.start()
        self.addCleanup(self.addresses.stop);self.addCleanup(self.connection.stop)

    def request(self, **kwargs):
        return api.request_json(self.binding,self.credential,method=kwargs.pop('method','GET'),
            resource=kwargs.pop('resource','posts'),**kwargs)

    def test_wordpress_resource_and_query_are_pinned_before_credentials_are_sent(self):
        value,pagination=self.request(query={'page':2,'per_page':100,'context':'edit'})
        self.assertEqual(value,[{'id':4,'slug':'article'}]);self.assertEqual(pagination,{'pages':3})
        self.pin.assert_called_once_with('https://fixture.test/wordpress/index.php','fixture.test')
        self.connect.assert_called_once_with('fixture.test','8.8.8.8')
        method,target=self.client.request.call_args.args
        self.assertEqual(method,'GET');self.assertEqual(target,'/wordpress/index.php?rest_route=%2Fwp%2Fv2%2Fposts&page=2&per_page=100&context=edit')
        headers=self.client.request.call_args.kwargs['headers']
        encoded=base64.b64encode(b'fixture-owner:synthetic-application-password').decode()
        self.assertEqual(headers['Authorization'],'Basic '+encoded)
        self.assertNotIn('Cookie',headers);self.assertNotIn('Referer',headers)
        self.response.read.assert_called_once_with(api.MAX_JSON_BYTES+1)
        self.response.close.assert_called_once();self.client.close.assert_called_once()

    def test_credential_bearing_redirects_are_never_followed_or_read(self):
        for status in (301,302,303,307,308):
            self.response.status=status
            self.headers['Location']='https://attacker.test/collect'
            with self.subTest(status=status),self.assertRaises(api.CmsApiError) as error:self.request()
            self.assertEqual(error.exception.category,'rejected')
        self.assertEqual(self.client.request.call_count,5)
        self.response.read.assert_not_called()

    def test_private_or_rebound_destinations_fail_before_any_authentication(self):
        from .website_contract import WebsiteAuthorityError
        self.pin.side_effect=WebsiteAuthorityError('deployment_origin_invalid','Private address')
        with self.assertRaises(api.CmsApiError) as error:self.request()
        self.assertEqual(error.exception.category,'origin_unavailable')
        self.connect.assert_not_called();self.client.request.assert_not_called()

    def test_fixed_resources_identifiers_and_query_keys_cannot_be_used_as_urls(self):
        for changes in ({'resource':'https://attacker.test/'},{'resource':'../users'},
                        {'identifier':'4?token=private'},{'identifier':True},{'method':'TRACE'},
                        {'query':{'url':'https://attacker.test/'}},{'query':{'page':True}},
                        {'query':{'filter':'x'*513}},{'query':{'slug':'bad\nheader'}}):
            with self.subTest(changes=changes),self.assertRaises(api.CmsApiError):self.request(**changes)
        self.connect.assert_not_called()

    def test_safe_error_classes_do_not_include_provider_body_headers_or_credentials(self):
        self.response.read.return_value=b'private-provider-body synthetic-application-password'
        for status,category in ((401,'credentials_required'),(403,'credentials_required'),
                               (429,'transient'),(503,'transient'),(400,'rejected')):
            self.response.status=status
            with self.subTest(status=status),self.assertRaises(api.CmsApiError) as error:self.request()
            self.assertEqual(error.exception.category,category)
            self.assertNotIn('synthetic-application-password',str(error.exception))
            self.assertNotIn('private-provider-body',str(error.exception))
        self.response.read.assert_not_called()

    def test_html_compressed_oversized_duplicate_or_nonfinite_responses_fail_closed(self):
        for headers,body in (({'Content-Type':'text/html'},b'<html>private</html>'),
                             ({'Content-Encoding':'gzip'},b'compressed'),({},b'x'*(api.MAX_JSON_BYTES+1)),
                             ({},b'{"id":1,"id":2}'),({},b'{"id":NaN}'),({},b'null'),({},b'\xff')):
            self.headers={'Content-Type':'application/json',**headers};self.response.read.return_value=body
            with self.subTest(headers=headers,body_length=len(body)),self.assertRaises(api.CmsApiError) as error:self.request()
            self.assertEqual(error.exception.category,'invalid_response')

    def test_invalid_payload_is_rejected_before_dns_or_credentials(self):
        for changes in ({'payload':{'title':'x'}},{'method':'POST','payload':{'title':float('nan')}},
                        {'method':'POST','payload':{'content':'x'*(api.MAX_JSON_BYTES+1)}}):
            with self.subTest(changes=changes),self.assertRaises(api.CmsApiError) as error:self.request(**changes)
            self.assertEqual(error.exception.category,'invalid_request')
        self.pin.assert_not_called();self.connect.assert_not_called()

    def test_timeout_keeps_a_safe_transient_category_and_closes_connection(self):
        self.client.request.side_effect=TimeoutError('private-provider-details')
        with self.assertRaises(api.CmsApiError) as error:self.request(method='POST',payload={'title':'Article'})
        self.assertEqual(error.exception.category,'transient')
        self.assertNotIn('private-provider-details',str(error.exception))
        self.client.close.assert_called_once()

    def test_ghost_uses_short_lived_signed_jwt_and_the_selected_admin_origin(self):
        binding={**self.binding,'platform':'ghost','api_origin':'https://admin.fixture.test/'}
        credential=CmsCredential(platform='ghost',secret='a'*24+':'+'b'*64)
        with patch.object(api.time,'time',return_value=1800000000):
            api.request_json(binding,credential,method='GET',resource='posts',query={'formats':'html','limit':100})
        self.pin.assert_called_once_with('https://admin.fixture.test/ghost/api/admin/posts/','admin.fixture.test')
        headers=self.client.request.call_args.kwargs['headers']
        self.assertEqual(headers['Accept-Version'],'v6.0')
        token=headers['Authorization'].removeprefix('Ghost ')
        decoded=jwt.decode(token,bytes.fromhex('b'*64),algorithms=['HS256'],audience='/admin/',
            options={'verify_exp':False,'verify_iat':False})
        self.assertEqual(decoded,{'iat':1800000000,'exp':1800000060,'aud':'/admin/'})
        self.assertEqual(jwt.get_unverified_header(token)['kid'],'a'*24)

    def test_cross_platform_or_malformed_plaintext_never_reaches_http(self):
        for credential in (CmsCredential(platform='ghost',secret='a'*24+':'+'b'*64),
                           CmsCredential(platform='wordpress',username='user:foreign',secret='synthetic-application-password')):
            with self.assertRaises(api.CmsApiError) as error:
                api.request_json(self.binding,credential,method='GET',resource='posts')
            self.assertEqual(error.exception.category,'credential_context_changed')
        self.pin.assert_not_called();self.connect.assert_not_called()
