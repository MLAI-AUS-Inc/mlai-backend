"""Member adapters for contracts otherwise owned by trusted Roo service calls."""
import uuid
from datetime import timedelta
from django.core import signing
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from roo.meeting_rooms import MeetingRoomService,MeetingRoomError
from roo.meeting_room_views import _parse_timestamp
from roo.models import MeetingRoomBooking,Ledger,RewardRedemption
from roo.services import PointsService,RewardsService
from roo.permissions import InsufficientBalanceError
from roo.staging_fixtures import ROOMS
from roo.staging_payments import create_staging_purchase,resolve_staging_purchase
SALT='staging-member-room-preview-v1'

class MemberView(APIView):
    permission_classes=(IsAuthenticated,)
    def handle_exception(self,exc):
        if isinstance(exc,MeetingRoomError):return Response({'code':exc.code,'error':exc.message},status=exc.status_code)
        if isinstance(exc,InsufficientBalanceError):return Response({'code':'insufficient_balance','error':str(exc)},status=409)
        if isinstance(exc,(ValueError,TypeError)):return Response({'code':'invalid_request','error':str(exc)},status=400)
        return super().handle_exception(exc)
    def fields(self,request,allowed):
        if not isinstance(request.data,dict) or set(request.data)-set(allowed):raise MeetingRoomError('invalid_request','Unexpected request fields.')
    def interval(self,request):
        room=request.data.get('room_slug')
        if room not in ROOMS:raise MeetingRoomError('room_unavailable','Use a synthetic staging room.',404)
        return room,_parse_timestamp(request.data.get('starts_at'),'starts_at'),_parse_timestamp(request.data.get('ends_at'),'ends_at')
    def identity(self,request):
        session=getattr(request,'community_chat_account_session',None)
        if session is None:raise MeetingRoomError('session_required','Independent staging session required.',401)
        return str(session.pk)

class RoomsView(MemberView):
    def get(self,request):return Response({'rooms':[room for room in MeetingRoomService.list_rooms() if room['slug'] in ROOMS],'synthetic':True})
class AvailabilityView(MemberView):
    def post(self,request):
        self.fields(request,('room_slug','starts_at','ends_at','client_request_id'))
        room,start,end=self.interval(request);request_id=str(MeetingRoomService.validate_client_request_id(request.data.get('client_request_id')))
        result=MeetingRoomService.availability(user=request.user,room_slug=room,starts_at=start,ends_at=end)
        start,end,cost=MeetingRoomService.validate_interval(start,end)
        result['preview']=signing.dumps({'user':str(request.user.pk),'session':self.identity(request),'room':room,'start':start.isoformat(),'end':end.isoformat(),'cost':cost,'request':request_id},salt=SALT)
        result['confirmation_expires_at']=(timezone.now()+timedelta(minutes=10)).isoformat();result['synthetic']=True
        return Response(result)
class BookView(MemberView):
    @transaction.atomic
    def post(self,request):
        self.fields(request,('room_slug','starts_at','ends_at','client_request_id','preview'))
        room,start,end=self.interval(request);request_id=MeetingRoomService.validate_client_request_id(request.data.get('client_request_id'))
        session=self.identity(request)
        # A committed retry remains safe after preview expiry, but still verifies
        # the signed owner/session/payload (max_age=None), never an untrusted UUID.
        existing=MeetingRoomBooking.objects.filter(client_request_id=request_id,user=request.user).first()
        try:preview=signing.loads(str(request.data.get('preview','')),salt=SALT,max_age=None if existing else 600)
        except signing.SignatureExpired:raise MeetingRoomError('expired_confirmation','Booking preview expired.',410)
        except signing.BadSignature:raise MeetingRoomError('invalid_preview','Invalid booking preview.',400)
        expected={'user':str(request.user.pk),'session':session,'room':room,'start':start.isoformat(),'end':end.isoformat(),'request':str(request_id)}
        if any(preview.get(k)!=v for k,v in expected.items()):raise MeetingRoomError('idempotency_conflict','Preview does not match this account/session/interval/request.',409)
        booking,created=MeetingRoomService.book(user=request.user,requested_by_slack_id='STAGING_LOCAL',room_slug=room,starts_at=start,ends_at=end,client_request_id=str(request_id),confirmation_expires_at=timezone.now()+timedelta(seconds=30),expected_points_cost=preview['cost'])
        return Response({'booking':MeetingRoomService.serialize_booking(booking),'created':created,'balance':MeetingRoomService.current_balance(request.user),'synthetic':True},status=201 if created else 200)
class MyBookingsView(MemberView):
    def post(self,request):
        self.fields(request,())
        return Response({'bookings':[MeetingRoomService.serialize_booking(b) for b in MeetingRoomService.my_bookings(user=request.user)]})
class CancelView(MemberView):
    def post(self,request):
        self.fields(request,('booking_id',))
        booking,changed,refunded=MeetingRoomService.cancel(user=request.user,booking_id=request.data.get('booking_id'),requested_by_slack_id='STAGING_LOCAL')
        return Response({'booking':MeetingRoomService.serialize_booking(booking),'changed':changed,'refunded':refunded,'balance':MeetingRoomService.current_balance(request.user)})
class OwnLedgerView(MemberView):
    def get(self,request):
        rows=Ledger.objects.filter(user=request.user).order_by('-pk')[:100]
        return Response({'ledger':[{'id':r.pk,'kind':r.kind,'source':r.source,'delta_microroo':str(r.delta_microroo or 0),'digital_delta_microroo':str(r.digital_delta_microroo),'description':r.description,'created_at':r.created_at} for r in rows]})
class RewardsView(MemberView):
    def get(self,request):return Response({'rewards':RewardsService.list_available(request.user)})
    @transaction.atomic
    def post(self,request):
        self.fields(request,('reward_code','quantity','client_request_id'))
        from django.contrib.auth import get_user_model
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        key='staging-reward:'+str(uuid.UUID(str(request.data.get('client_request_id'))))
        code=request.data.get('reward_code');quantity=request.data.get('quantity',1)
        if code not in {'STAGING_AUTO','STAGING_MANUAL'} or type(quantity)!=int or quantity<1:raise ValueError('Positive quantity and synthetic reward required.')
        row=RewardRedemption.objects.filter(user=request.user,notes=key).first()
        if row:
            if row.reward_id!=code or row.quantity!=quantity:raise MeetingRoomError('idempotency_conflict','Reward request changed.',409)
        else:row=RewardsService.request_redemption(user=request.user,reward_code=code,quantity=quantity,notes=key)
        return Response({'id':str(row.pk),'status':row.status,'synthetic':True})
class PurchaseView(MemberView):
    def post(self,request):
        self.fields(request,('pack_id','client_request_id','consent'))
        row=create_staging_purchase(request.user,request.data.get('pack_id'),request.data.get('client_request_id'),request.data.get('consent'))
        return Response({'id':str(row.pk),'status':row.status,'points_amount':row.points_amount,'synthetic':True})
class PurchaseOutcomeView(MemberView):
    def post(self,request):
        self.fields(request,('purchase_id','outcome'))
        row=resolve_staging_purchase(request.user,request.data.get('purchase_id'),request.data.get('outcome'))
        return Response({'id':str(row.pk),'status':row.status,'synthetic':True})
