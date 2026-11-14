"""Narrow Cybertest service operations and separately authenticated consent."""
import secrets
from urllib.parse import urlsplit
from uuid import UUID
from django.conf import settings
from rest_framework.parsers import JSONParser
from rest_framework.permissions import BasePermission, IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView
from community_chat.authentication import CommunityChatAccountAuthentication
from . import cybertest_checkout as checkout
from .cybertest_policy import CheckoutError, token_digest
from .cybertest_paths import base_path
from .permissions import InsufficientBalanceError, IdempotencyConflictError

COOKIE='cybertest_checkout'
COOKIE_PATH='/api/v1/my-startup/cybertest/'


class ServicePermission(BasePermission):
    def has_permission(self,request,view):
        expected=getattr(settings,'CYBERTEST_BILLING_API_KEY','')
        supplied=request.headers.get('X-Cybertest-Key','')
        return bool(isinstance(expected,str) and len(expected)>=32 and
                    supplied and secrets.compare_digest(expected,supplied))


class CheckoutThrottle(SimpleRateThrottle):
    rate='30/minute'
    scope='cybertest_checkout'
    def get_cache_key(self,request,view):
        subject=f'user:{request.user.pk}' if request.user and request.user.is_authenticated else f'ip:{self.get_ident(request)}'
        return self.cache_format%{'scope':self.scope,'ident':subject}


def _body(request,allowed):
    if not isinstance(request.data,dict) or set(request.data)-set(allowed):
        raise CheckoutError('INVALID_REQUEST','The request contains unsupported fields.',400)
    return request.data


def _handoff(value):
    try:
        quote_id,token=value.split('.',1)
        quote_id=UUID(quote_id);token_digest(token)
        return quote_id,token
    except (ValueError,AttributeError,TypeError) as exc:
        raise CheckoutError('INVALID_HANDOFF','Open the original checkout link again.',400) from exc


def _origin(setting):
    value=getattr(settings,setting,'').rstrip('/')
    parsed=urlsplit(value)
    allowed=parsed.scheme=='https' or (settings.DEBUG and parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1'))
    if not allowed or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise CheckoutError('BILLING_UNAVAILABLE','The checkout destination is not configured.',503)
    return value


class PrivateView(APIView):
    parser_classes=[JSONParser]
    throttle_classes=[CheckoutThrottle]

    def handle_exception(self,exc):
        if isinstance(exc,CheckoutError):return Response({'code':exc.code,'message':str(exc)},status=exc.status)
        if isinstance(exc,InsufficientBalanceError):return Response({'code':'INSUFFICIENT_ROO_POINTS','message':'There are not enough Roo points.'},status=402)
        if isinstance(exc,IdempotencyConflictError):return Response({'code':'CHECKOUT_CONFLICT','message':'This request has already been used.'},status=409)
        return super().handle_exception(exc)

    def finalize_response(self,request,response,*args,**kwargs):
        response=super().finalize_response(request,response,*args,**kwargs)
        response['Cache-Control']='private, no-store';response['Referrer-Policy']='no-referrer'
        return response


class ServiceView(PrivateView):
    authentication_classes=[]
    permission_classes=[ServicePermission]


class CreateQuoteView(ServiceView):
    def get(self,request):
        return Response(checkout.pending_refunds(request.query_params.get('after')))
    def post(self,request):
        data=_body(request,['checkout_id','token','binding','execution_plan'])
        frontend=_origin('COMMUNITY_CHAT_FRONTEND_URL')
        result=checkout.create_quote(data.get('checkout_id'),data.get('token'),data.get('binding') or {},**({'execution_plan':data['execution_plan']} if 'execution_plan' in data else {}))
        return Response({**result,'checkout_url':f"{frontend}/my-startup/cybertest#{result['checkout_id']}.{data['token']}"})


class QuoteStatusView(ServiceView):
    def get(self,request,checkout_id):return Response(checkout.status(checkout_id))


class RecoverQuoteView(ServiceView):
    def post(self,request):
        data=_body(request,['binding','token','parent_checkout_id'])
        result=checkout.recover_quote(data.get('binding'),data.get('token'),
            **({'parent_checkout_id':data['parent_checkout_id']} if 'parent_checkout_id' in data else {}))
        if result and result['status']=='quoted':
            frontend=_origin('COMMUNITY_CHAT_FRONTEND_URL')
            result={**result,'checkout_url':f"{frontend}/my-startup/cybertest#{result['checkout_id']}.{data['token']}"}
        return Response({'receipt':result})


class ChargeView(ServiceView):
    def post(self,request,checkout_id):
        data=_body(request,['audit_id','binding'])
        return Response(checkout.charge(checkout_id,data.get('audit_id'),data.get('binding') or {}))


class DispatchView(ServiceView):
    def post(self,request,checkout_id):
        data=_body(request,['audit_id'])
        return Response(checkout.start_dispatch(checkout_id,data.get('audit_id')))


class RefundView(ServiceView):
    def post(self,request,checkout_id):
        data=_body(request,['audit_id','failure_code'])
        return Response(checkout.refund_start_failure(checkout_id,data.get('audit_id'),data.get('failure_code')))


class CaptureView(PrivateView):
    authentication_classes=[]
    permission_classes=[AllowAny]
    def post(self,request):
        origin=str(request.headers.get('Origin') or '').rstrip('/')
        if not origin or origin not in getattr(settings,'COMMUNITY_CHAT_ALLOWED_ORIGINS',[]):
            raise CheckoutError('INVALID_ORIGIN','Open checkout from MLAI Chat.',403)
        data=_body(request,['handoff']);quote_id,token=_handoff(data.get('handoff'))
        checkout.configured_mode()
        # Authenticate the opaque handoff without disclosing a payer or balance.
        row=checkout._get(quote_id);checkout._handoff(row,token);checkout._current_mode(row);checkout._unexpired(row)
        response=Response({'captured':True})
        response.set_cookie(COOKIE,f'{quote_id}.{token}',max_age=1800,httponly=True,
            secure=not settings.DEBUG,samesite='Lax',path=COOKIE_PATH)
        return response


class AccountView(PrivateView):
    authentication_classes=[CommunityChatAccountAuthentication]
    permission_classes=[IsAuthenticated]


class PreviewView(AccountView):
    def get(self,request):
        quote_id,token=_handoff(request.COOKIES.get(COOKIE))
        return Response(checkout.preview(quote_id,token,request.user))


class ConfirmView(AccountView):
    def post(self,request):
        data=_body(request,['expected_points','checkout_id','binding_sha256'])
        quote_id,token=_handoff(request.COOKIES.get(COOKIE))
        # Cookies are shared by tabs. Consent must match the quote displayed in
        # the submitting page, not a newer handoff captured in a different tab.
        if str(quote_id)!=data.get('checkout_id') or checkout.status(quote_id)['binding_sha256']!=data.get('binding_sha256'):
            raise CheckoutError('STALE_CHECKOUT','Another checkout was opened. Reload and review the scope before confirming.')
        destination=_origin('CYBERTEST_APP_ORIGIN')
        try:
            prefix=base_path(getattr(settings,'CYBERTEST_BASE_PATH',''))
        except ValueError as exc:
            raise CheckoutError('BILLING_UNAVAILABLE','The checkout destination is not configured.',503) from exc
        result=checkout.confirm(quote_id,token,request.user,expected_points=data.get('expected_points'))
        return Response({**result,'return_url':f'{destination}{prefix}/?billing_return={quote_id}'})

class FailedSettlementView(ServiceView):
    def post(self, request, checkout_id):
        data = _body(request, ['audit_id', 'failure_code', 'costs'])
        return Response(checkout.settle_failed_review(checkout_id, data.get('audit_id'), data.get('failure_code'), data.get('costs')))


class DeliverySettlementView(ServiceView):
    def post(self, request, checkout_id):
        data = _body(request, ['audit_id', 'evidence', 'costs'])
        return Response(checkout.settle_delivery(checkout_id, data.get('audit_id'), data.get('evidence'), data.get('costs')))
