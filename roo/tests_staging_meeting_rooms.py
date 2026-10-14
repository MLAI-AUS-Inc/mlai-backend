import uuid
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.utils import timezone
from django.core import signing
from rest_framework.test import APIClient
from community_chat.tests import test_staging_password as fixtures
from roo.models import PointsAccount,MeetingRoomBooking
from roo.services import PointsService
ROOT='/api/v1/points/meeting-rooms/'
@override_settings(**fixtures.PILOT,STAGING_FIXTURE_DATABASE='staging_points_tests',MEETING_ROOM_BOOKING_ENABLED=True)
class MemberRoomTests(TestCase):
    def setUp(self):
        fixtures.StagingPasswordTests.setUp(self)
        from community_chat.models import CommunityMemberProfile
        CommunityMemberProfile.objects.create(user=self.user,status='approved',review_reasons=['synthetic_staging_pilot'])
        from roo.staging_fixtures import seed_user,seed_catalogue
        seed_user(self.user);seed_catalogue()
        login=fixtures.StagingPasswordTests.login(self,'android')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer '+login.data['session']['access_token'],HTTP_ORIGIN=fixtures.NATIVE)
        start=(timezone.now()+timedelta(days=2)).replace(hour=2,minute=0,second=0,microsecond=0)
        self.body={'room_slug':'small-meeting-room','starts_at':start.isoformat(),'ends_at':(start+timedelta(hours=1)).isoformat(),'client_request_id':str(uuid.uuid4())}
    def post(self,path,data):return self.client.post(ROOT+path+'/',data,format='json')
    def test_booking_debit_replay_owner_refund(self):
        quote=self.post('availability',self.body);self.assertEqual(quote.status_code,200)
        body={**self.body,'preview':quote.data['preview']}
        first=self.post('book',body);self.assertEqual(first.status_code,201,first.data)
        self.assertEqual(self.post('book',body).status_code,200)
        self.assertEqual(PointsService.get_available_microroo(self.user),11_000_000)
        self.assertEqual(PointsAccount.objects.get(user=self.user).digital_balance_microroo,24_000_000)
        booking=first.data['booking']['id']
        with patch('django.core.signing.time.time',return_value=__import__('time').time()+3600):
            self.assertEqual(self.post('book',body).status_code,200)
        for _ in range(2):self.assertEqual(self.post('cancel',{'booking_id':booking}).status_code,200)
        self.assertEqual(PointsService.get_available_microroo(self.user),12_000_000)
    def test_payload_identity_and_expired_confirmation_rejected(self):
        quote=self.post('availability',self.body)
        body={**self.body,'preview':quote.data['preview']}
        self.assertEqual(self.post('book',{**body,'target_slack_user_id':'foreign'}).status_code,400)
        with patch('django.core.signing.TimestampSigner.unsign',side_effect=signing.SignatureExpired('expired')):
            self.assertEqual(self.post('book',body).status_code,410)
        changed={**body,'client_request_id':str(uuid.uuid4())}
        self.assertEqual(self.post('book',changed).status_code,409)
        self.assertFalse(MeetingRoomBooking.objects.exists())
    def test_two_hour_charge_conflict_stranger_and_started_cancel(self):
        self.body['ends_at']=(timezone.datetime.fromisoformat(self.body['starts_at'])+timedelta(hours=2)).isoformat()
        quote=self.post('availability',self.body);body={**self.body,'preview':quote.data['preview']}
        first=self.post('book',body);self.assertEqual(first.status_code,201)
        self.assertEqual(PointsService.get_available_microroo(self.user),10_000_000)
        booking_id=first.data['booking']['id']
        from django.contrib.auth import get_user_model
        from community_chat.models import CommunityMemberProfile
        from roo.staging_fixtures import seed_user
        original=self.user
        self.user=get_user_model().objects.create_user(email='volunteer01@staging.mlai.au',password='Synthetic-private-pilot-472!')
        CommunityMemberProfile.objects.create(user=self.user,status='approved',review_reasons=['synthetic_staging_pilot']);seed_user(self.user)
        self.installation=str(uuid.uuid4());self.client.credentials();login=fixtures.StagingPasswordTests.login(self,'android')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer '+login.data['session']['access_token'],HTTP_ORIGIN=fixtures.NATIVE)
        self.assertEqual(self.post('cancel',{'booking_id':booking_id}).status_code,403)
        self.body['client_request_id']=str(uuid.uuid4());quote=self.post('availability',self.body)
        self.assertEqual(self.post('book',{**self.body,'preview':quote.data['preview']}).status_code,409)
        self.assertEqual(self.post('my-bookings',{}).data['bookings'],[])
        from roo.meeting_rooms import MeetingRoomService,MeetingRoomError
        booking=MeetingRoomBooking.objects.get(pk=booking_id)
        with patch.object(MeetingRoomService,'_now',return_value=booking.starts_at),self.assertRaises(MeetingRoomError) as exc:
            MeetingRoomService.cancel(user=original,booking_id=booking_id,requested_by_slack_id='STAGING_LOCAL')
        self.assertEqual(exc.exception.code,'booking_started')
