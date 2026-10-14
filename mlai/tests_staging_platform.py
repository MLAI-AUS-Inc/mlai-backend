"""Ordinary route composition and real staging account authentication boundaries."""
import sys
import types
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.urls import clear_url_caches, path, resolve
from rest_framework.test import APIClient
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from core.authentication import CustomJWTAuthentication
from community_chat.tests import test_staging_password as fixtures
PILOT,STAGE,NATIVE,ROOT=fixtures.PILOT,fixtures.STAGE,fixtures.NATIVE,fixtures.ROOT
from community_chat.models import CommunityChatAccountSession

class NormalMemberView(APIView):
    authentication_classes=[CustomJWTAuthentication]
    permission_classes=[IsAuthenticated]
    def get(self,request):return Response({'user_id':request.user.pk})
    def post(self,request):return Response({'user_id':request.user.pk})

class NormalAdminView(NormalMemberView):permission_classes=[IsAdminUser]

@override_settings(**PILOT)
class StagingPlatformTests(TestCase):
    login=fixtures.StagingPasswordTests.login
    def setUp(self):
        fixtures.StagingPasswordTests.setUp(self)
        import mlai.urls
        self.original=mlai.urls.urlpatterns[:]
        mlai.urls.urlpatterns.extend([path('api/v1/new-function/',NormalMemberView.as_view()),path('api/v1/new-admin-function/',NormalAdminView.as_view())])
        clear_url_caches();self.addCleanup(self.restore_routes)
    def restore_routes(self):
        import mlai.urls
        mlai.urls.urlpatterns[:]=self.original;clear_url_caches()

    def test_normal_root_domain_routes_resolve_without_stage_registry(self):
        for route in ('/api/v1/auth/me/','/api/v1/founder-tools/bootstrap/','/api/v1/points/rate-card/','/api/v1/new-function/'):
            with self.subTest(route=route):self.assertIsNotNone(resolve(route).func)

    def test_new_normal_domain_view_accepts_actual_staged_user(self):
        response=self.login('ios');self.assertEqual(response.status_code,200)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer '+response.data['session']['access_token'],HTTP_ORIGIN=NATIVE)
        result=self.client.get('/api/v1/new-function/')
        self.assertEqual(result.status_code,200);self.assertEqual(result.data,{'user_id':self.user.pk})
        self.assertEqual(self.client.get('/api/v1/new-admin-function/').status_code,403)

    def test_browser_cookie_origin_remains_bound_on_new_route(self):
        self.assertEqual(self.login().status_code,200)
        self.assertEqual(self.client.post('/api/v1/new-function/',{},format='json',HTTP_ORIGIN=STAGE).status_code,200)
        self.assertEqual(self.client.post('/api/v1/new-function/',{},format='json',HTTP_ORIGIN='https://chat.mlai.au').status_code,401)

    def test_revoked_rotated_and_mixed_credentials_never_fall_back_to_jwt(self):
        response=self.login('ios');raw=response.data['session']['access_token']
        self.client.credentials(HTTP_AUTHORIZATION='Bearer '+raw,HTTP_ORIGIN=NATIVE)
        CommunityChatAccountSession.objects.filter(user=self.user).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get('/api/v1/new-function/').status_code,401)
        self.client.cookies['__Host-mlai_staging_access']=raw
        self.assertEqual(self.client.get('/api/v1/new-function/').status_code,401)
        self.client.cookies.clear();self.client.credentials(HTTP_AUTHORIZATION='Bearer non-staging-jwt',HTTP_ORIGIN=NATIVE)
        with patch('rest_framework_simplejwt.authentication.JWTAuthentication.authenticate',side_effect=AssertionError('Hosted staging must never use production JWT fallback')):
            self.assertEqual(self.client.get('/api/v1/new-function/').status_code,401)

    def test_legacy_cookie_names_cannot_authenticate_general_api(self):
        response=self.login('ios');raw=response.data['session']['access_token']
        self.client.cookies.clear();self.client.cookies['access_token']=raw
        self.assertEqual(self.client.get('/api/v1/new-function/',HTTP_ORIGIN=STAGE).status_code,401)

    def test_existing_core_me_uses_same_session_and_domain_dto(self):
        response=self.login('android');self.client.credentials(HTTP_AUTHORIZATION='Bearer '+response.data['session']['access_token'],HTTP_ORIGIN=NATIVE)
        result=self.client.get('/api/v1/auth/me/')
        self.assertEqual(result.status_code,200);self.assertEqual(result.data['email'],self.user.email)

    def test_profile_patch_uses_real_versioned_contract(self):
        self.assertEqual(self.login().status_code,200)
        account=self.client.get(ROOT+'account/',HTTP_ORIGIN=STAGE)
        result=self.client.patch(ROOT+'account/',{'display_name':'Synthetic edited name','profile_version':account.data['profile']['profile_version']},format='json',HTTP_ORIGIN=STAGE)
        self.assertEqual(result.status_code,200);self.assertEqual(result.data['profile']['display_name'],'Synthetic edited name')
        self.assertEqual(self.client.patch(ROOT+'account/',{'is_staff':True,'profile_version':result.data['profile']['profile_version']},format='json',HTTP_ORIGIN=STAGE).status_code,400)

    def test_revocation_between_initial_read_and_mutation_is_rechecked(self):
        from community_chat.account_sessions import authenticate_access_token
        response=self.login('ios');raw=response.data['session']['access_token']
        self.client.credentials(HTTP_AUTHORIZATION='Bearer '+raw,HTTP_ORIGIN=NATIVE)
        def revoke_after_read(token):
            session=authenticate_access_token(token)
            CommunityChatAccountSession.objects.filter(pk=session.pk).update(revoked_at=timezone.now())
            return session
        with patch('community_chat.authentication.authenticate_access_token',side_effect=revoke_after_read):
            result=self.client.post('/api/v1/new-function/',{},format='json')
        self.assertEqual(result.status_code,401)

    def test_general_api_keeps_service_key_authority(self):
        response=self.login('ios');self.client.credentials(HTTP_AUTHORIZATION='Bearer '+response.data['session']['access_token'],HTTP_ORIGIN=NATIVE)
        result=self.client.post('/api/v1/points/meeting-rooms/book/',{'slack_user_id':'another-user'},format='json')
        self.assertIn(result.status_code,(401,403))
