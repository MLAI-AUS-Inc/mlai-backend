import hashlib
import hmac
import json
import re
import time
from copy import deepcopy

from django.core.exceptions import ValidationError
from rest_framework import viewsets, status, mixins
from rest_framework.permissions import AllowAny, IsAuthenticated
# Removed mixins as they are not used in new code usually, but kept if needed for legacy.
# Also added logging

from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db.models import Q, Sum
from django.conf import settings
from django.shortcuts import get_object_or_404
from datetime import date, timedelta
from typing import Optional, Tuple
from uuid import UUID

from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount, BoostPostAdmission,
    TaskAssignment, TaskSubmission, CoworkingBooking, CoworkingBookingOperation,
    CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, TaskTemplate, PointsRequest, PointsPurchase,
)

from .services import (
    PointsService, PointsPurchaseService, CoworkingService, CoworkingBatchBookingError,
    BoostPostAdmissionService, BoostPostPayloadConflictError, InvalidBoostPostError,
    TaskService, RewardsService,
)
from .coding import roo_decimal_string
from .permissions import (
    can_list_committee_candidate_emails,
    can_generate_coworking_reports,
    is_points_admin,
    is_points_super_admin,
    IdempotencyConflictError,
    InsufficientBalanceError,
    PermissionDeniedError,
)
from .committee_candidates import CommitteeCandidateEmailService
from core.models import SlackFounderAccountLink, User
from core.slack_founder_links import (
    ConflictingSlackFounderLinkError,
    assign_direct_slack_identity,
    founder_tools_connection_type,
)
from core.permissions import HasAPIKey, HasRooApiKey, HasStrictRooApiKey
from core.slack_users import resolve_existing_user_from_profile
from integrations.services import SlackService
from community_chat.authentication import CommunityChatAccountAuthentication
from hospital.authentication import CustomJWTAuthentication

# Additional imports for Activity & Quests
import logging
from django.db import OperationalError, connection, transaction
from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskSubmission, CoworkingBooking, CoworkingBookingOperation, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, TaskTemplate, PointsRequest,
    # Activity & Quests
    ChannelFirstPost, QuestProgress
)
from .serializers import (
    PointsAdminSerializer, MinterSerializer, TaskSerializer, LedgerSerializer,
    PointsAccountSerializer, PointsBalanceSerializer, TaskAssignmentSerializer, TaskSubmissionSerializer,
    CoworkingBookingSerializer, CoworkingAvailabilitySerializer,
    CoworkingDayCapacitySerializer, RewardsCatalogSerializer, RewardRedemptionSerializer,
    TaskTemplateSerializer, PointsRequestSerializer,
    # Quests
    QuestProgressSerializer, QuestProgressInputSerializer, QuestCompleteInputSerializer
)

logger = logging.getLogger(__name__)
FIRST_CHANNEL_POST_POINTS = 4


def clean_slack_id(value: Optional[str]) -> str:
    """Normalize Slack IDs and mention strings to the raw Slack ID."""
    cleaned = str(value or '').strip()
    if cleaned.startswith('<@') and cleaned.endswith('>'):
        cleaned = cleaned[2:-1].split('|', 1)[0]
    if cleaned.startswith('@'):
        cleaned = cleaned[1:]
    return cleaned.strip()


def _coworking_operation_receipt(*, raw_id, kind: str, request_fields: dict):
    """Return a canonical operation id, fingerprint, and prior receipt."""
    if raw_id in (None, ''):
        return None, None, None
    try:
        operation_id = UUID(str(raw_id).strip())
    except (ValueError, TypeError, AttributeError):
        raise ValueError('operation_id must be a valid UUID')
    canonical_request = json.dumps(
        {'kind': kind, **request_fields},
        sort_keys=True,
        separators=(',', ':'),
    )
    fingerprint = hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()
    receipt = CoworkingBookingOperation.objects.filter(pk=operation_id).first()
    if receipt and (
        receipt.kind != kind or receipt.request_fingerprint != fingerprint
    ):
        raise ValueError('operation_id was already used for a different request')
    return operation_id, fingerprint, receipt


def _coworking_receipt_payload(response_data, *, kind):
    """Keep the durable result needed by Roo without serializer user PII."""
    payload = deepcopy(dict(response_data))
    if kind == 'single':
        payload.pop('user', None)
        payload.pop('user_email', None)
    else:
        rows = sorted(
            payload.get('results', []),
            key=lambda row: str(row.get('slack_user_id') or ''),
        )
        for row in rows:
            row.pop('slack_user_id', None)
            booking = row.get('booking') if isinstance(row, dict) else None
            if isinstance(booking, dict):
                booking.pop('user', None)
                booking.pop('user_email', None)
        payload['results'] = rows
        payload.pop('admin_slack_user_id', None)
    return payload


def _coworking_replay_response(
    receipt,
    *,
    admin_slack_user_id=None,
    target_slack_user_ids=None,
):
    payload = deepcopy(dict(receipt.response_payload))
    if receipt.kind == 'single':
        current_status = CoworkingBooking.objects.filter(
            pk=payload.get('id')
        ).values_list('status', flat=True).first()
        payload['operation_booking_current_status'] = current_status or 'deleted'
    else:
        targets = sorted(
            str(value).strip() for value in (target_slack_user_ids or [])
        )
        payload['admin_slack_user_id'] = str(admin_slack_user_id or '').strip()
        for row, slack_user_id in zip(payload.get('results', []), targets):
            row['slack_user_id'] = slack_user_id
        booking_rows = [
            row.get('booking')
            for row in payload.get('results', [])
            if isinstance(row, dict) and isinstance(row.get('booking'), dict)
        ]
        current_statuses = {
            str(booking_id): booking_status
            for booking_id, booking_status in CoworkingBooking.objects.filter(
                pk__in=[booking.get('id') for booking in booking_rows]
            ).values_list('id', 'status')
        }
        for booking in booking_rows:
            booking['operation_booking_current_status'] = current_statuses.get(
                str(booking.get('id')), 'deleted'
            )
    payload['idempotent'] = True
    payload['operation_replayed'] = True
    return Response(payload, status=status.HTTP_200_OK)


def split_slack_profile_name(profile: dict, slack_user_id: str) -> Tuple[str, str]:
    """Return safe first/last names from Slack profile fields."""
    candidates = (
        profile.get('real_name'),
        profile.get('display_name'),
        profile.get('name'),
        'Unknown Slack User',
    )
    display_name = ''
    for candidate in candidates:
        display_name = ' '.join(str(candidate or '').strip().split())
        if display_name:
            break

    first_name, separator, last_name = display_name.partition(' ')
    if not first_name:
        first_name = 'Unknown'
        last_name = 'Slack User'
    return first_name, last_name

STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300


def get_or_create_user_for_slack_id(slack_user_id: str) -> User:
    """Resolve a Slack user to a local user, creating a placeholder when needed."""
    slack_user_id = clean_slack_id(slack_user_id)
    if not slack_user_id:
        raise ValueError('target_slack_id is required')

    user = PointsService.get_user_by_slack_id(slack_user_id)
    if user:
        return user

    profile = SlackService.get_user_profile(slack_user_id)
    if profile:
        email = profile.get('email') or f"{slack_user_id}@slack.placeholder.com"
        first_name, last_name = split_slack_profile_name(profile, slack_user_id)

        existing_user = User.objects.filter(email=email).first()
        if existing_user:
            if not existing_user.slack_id:
                try:
                    existing_user = assign_direct_slack_identity(
                        existing_user,
                        slack_user_id,
                    )
                except ConflictingSlackFounderLinkError:
                    email = f"{slack_user_id}@slack.placeholder.com"
                else:
                    return existing_user
            if existing_user.slack_id == slack_user_id:
                return existing_user
            else:
                email = f"{slack_user_id}@slack.placeholder.com"

        return User.objects.create(
            email=email,
            slack_id=slack_user_id,
            first_name=first_name,
            last_name=last_name,
            avatar_url=profile.get('image_url'),
        )

    return User.objects.create(
        email=f"{slack_user_id}@slack.placeholder.com",
        slack_id=slack_user_id,
        first_name="Unknown Slack User",
    )


def get_existing_user_for_slack_id(slack_user_id: Optional[str]) -> Optional[User]:
    """Resolve an already-linked Slack user without creating a placeholder."""
    if not slack_user_id:
        return None
    return PointsService.get_user_by_slack_id(slack_user_id.strip())


def require_linked_points_admin(slack_user_id: Optional[str], *, action_label: str) -> User:
    """Require a real linked user account plus an active Points Admin role."""
    cleaned_slack_id = (slack_user_id or '').strip()
    if not cleaned_slack_id:
        raise ValueError('created_by_user_id or slack_user_id is required')

    user = get_existing_user_for_slack_id(cleaned_slack_id)
    if not user:
        raise PermissionDeniedError(
            f'A linked user account is required to {action_label}'
        )
    if not is_points_admin(cleaned_slack_id):
        raise PermissionDeniedError(f'Only Points Admins can {action_label}')
    return user


def award_first_channel_post_bonus(slack_user_id: str, channel_id: str) -> Tuple[bool, Optional[int]]:
    """Create the first-post marker and award the intro bonus atomically."""
    with transaction.atomic():
        _, created = ChannelFirstPost.objects.get_or_create(
            slack_user_id=slack_user_id,
            channel_id=channel_id,
        )
        if not created:
            return False, None

        user = get_or_create_user_for_slack_id(slack_user_id)
        _, awarded = PointsService.award(
            user=user,
            delta=FIRST_CHANNEL_POST_POINTS,
            source='EVENT',
            description='Completed quest: First Contact',
            created_by_slack_id='SYSTEM',
            idempotency_key=f"first_post_award:{slack_user_id}:{channel_id}",
            reference_type='FIRST_CHANNEL_POST',
            reference_id=f"{slack_user_id}:{channel_id}",
        )
        balance = PointsService.get_balance(user)
        return awarded, balance['balance']


def is_retryable_sqlite_lock(exc: Exception) -> bool:
    """Return whether a DB error is a transient SQLite lock."""
    message = str(exc).lower()
    return connection.vendor == 'sqlite' and 'locked' in message


class RateCardView(viewsets.ReadOnlyModelViewSet):
    """
    Public (authenticated) rate card of standard tasks.
    """
    queryset = TaskTemplate.objects.filter(is_active=True)
    serializer_class = TaskTemplateSerializer
    # Allow either API Key (for Roo/bots) or IsAuthenticated (for frontend users)
    permission_classes = [HasAPIKey | settings.REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES'][0]]


class StripeWebhookView(APIView):
    permission_classes = [AllowAny]

    def _parse_stripe_signature(self, signature_header):
        parts = {}
        for item in signature_header.split(','):
            key, separator, value = item.partition('=')
            if separator and key and value:
                parts.setdefault(key, []).append(value)
        timestamp_values = parts.get('t') or []
        signatures = parts.get('v1') or []
        if not timestamp_values or not signatures:
            raise ValueError('Malformed Stripe signature header')
        try:
            timestamp = int(timestamp_values[0])
        except ValueError as exc:
            raise ValueError('Malformed Stripe signature timestamp') from exc
        return timestamp, signatures

    def _verify_stripe_signature(self, payload, signature_header):
        webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')
        if not webhook_secret:
            raise RuntimeError('Stripe webhook secret is not configured')
        if not signature_header:
            raise ValueError('Missing Stripe signature header')

        timestamp, signatures = self._parse_stripe_signature(signature_header)
        if abs(time.time() - timestamp) > STRIPE_WEBHOOK_TOLERANCE_SECONDS:
            raise ValueError('Stripe signature timestamp is outside the tolerance window')

        signed_payload = f"{timestamp}.".encode('utf-8') + payload
        expected_signature = hmac.new(
            webhook_secret.encode('utf-8'),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not any(hmac.compare_digest(expected_signature, signature) for signature in signatures):
            raise ValueError('Invalid Stripe signature')

    def _find_purchase_for_session(self, session):
        metadata = session.get('metadata') or {}
        purchase_id = metadata.get('points_purchase_id')
        queryset = PointsPurchase.objects.select_for_update()
        if purchase_id:
            return queryset.get(id=purchase_id)
        return queryset.get(stripe_checkout_session_id=session.get('id'))

    def _post_paid_slack_confirmation(self, purchase):
        purchase_from = purchase.purchase_from or {}
        if purchase_from.get('source') != 'slack':
            return False

        channel_id = str(purchase_from.get('slack_channel_id') or '').strip()
        thread_ts = str(purchase_from.get('slack_thread_ts') or '').strip()
        if not channel_id or not thread_ts:
            return False

        metadata = dict(purchase.metadata or {})
        if metadata.get('slack_paid_confirmation_message_ts'):
            return False

        text = (
            f"Top-up complete. {purchase.points_amount} Roo Points have been added. "
            "These points can be used for eligible MLAI rewards, but they do not count "
            "toward lifetime earned contribution."
        )
        sent, message_ts = SlackService.send_message(
            channel_id,
            text,
            thread_ts=thread_ts,
        )
        if not sent:
            logger.warning(
                "Failed to post Roo Points paid confirmation for purchase %s",
                purchase.id,
            )
            return False

        metadata['slack_paid_confirmation_message_ts'] = message_ts
        metadata['slack_paid_confirmation_sent_at'] = timezone.now().isoformat()
        purchase.metadata = metadata
        purchase.save(update_fields=['metadata', 'updated_at'])
        return True

    def _handle_checkout_completed(self, session):
        if session.get('status') != 'complete' or session.get('payment_status') != 'paid':
            return {'received': True, 'ignored': True, 'reason': 'checkout_session_not_paid'}

        session_id = session.get('id')
        if not session_id:
            raise ValueError('Stripe checkout session id is required')

        should_post_slack_confirmation = False
        slack_confirmation_sent = False
        with transaction.atomic():
            purchase = self._find_purchase_for_session(session)
            session_metadata = session.get('metadata') or {}
            checkout_consent_mode = session_metadata.get('checkout_consent_mode')
            if (
                checkout_consent_mode == 'stripe'
                and (session.get('consent') or {}).get('terms_of_service') != 'accepted'
            ):
                raise ValueError(
                    'Stripe terms consent is required for this Roo Points purchase'
                )

            if purchase.stripe_checkout_session_id and purchase.stripe_checkout_session_id != session_id:
                raise ValueError('Stripe checkout session does not match the purchase')

            if purchase.status == 'paid' and purchase.ledger_entry_id:
                return {
                    'received': True,
                    'purchase_id': str(purchase.id),
                    'credited': False,
                    'already_paid': True,
                }

            if purchase.status != 'pending':
                return {
                    'received': True,
                    'purchase_id': str(purchase.id),
                    'ignored': True,
                    'reason': f'purchase_{purchase.status}',
                }

            ledger, credited = PointsService.credit_purchased_topup(
                user=purchase.user,
                delta=purchase.points_amount,
                description=f"{purchase.points_amount} Top-up Roo Points purchase",
                idempotency_key=f"stripe_checkout_session:{session_id}",
                reference_type='POINTS_PURCHASE',
                reference_id=str(purchase.id),
                created_by_slack_id='STRIPE',
            )

            purchase.status = 'paid'
            purchase.paid_at = timezone.now()
            purchase.stripe_checkout_session_id = session_id
            purchase.ledger_entry = ledger
            purchase_update_fields = [
                'status',
                'paid_at',
                'stripe_checkout_session_id',
                'ledger_entry',
                'updated_at',
            ]
            if checkout_consent_mode == 'stripe':
                purchase.terms_version_accepted = str(
                    session_metadata.get('terms_version')
                    or getattr(settings, 'ROO_POINTS_TERMS_VERSION', '')
                ).strip()
                purchase.terms_accepted_at = purchase.paid_at
                purchase.privacy_version_accepted = str(
                    session_metadata.get('privacy_version')
                    or getattr(settings, 'ROO_POINTS_PRIVACY_VERSION', '')
                ).strip()
                purchase.privacy_accepted_at = purchase.paid_at
                purchase_metadata = dict(purchase.metadata or {})
                purchase_metadata.update(
                    {
                        'stripe_terms_consent': 'accepted',
                        'stripe_terms_consent_recorded_at': purchase.paid_at.isoformat(),
                    }
                )
                purchase.metadata = purchase_metadata
                purchase_update_fields.extend(
                    [
                        'terms_version_accepted',
                        'terms_accepted_at',
                        'privacy_version_accepted',
                        'privacy_accepted_at',
                        'metadata',
                    ]
                )
            purchase.save(update_fields=purchase_update_fields)
            should_post_slack_confirmation = True

        if should_post_slack_confirmation:
            slack_confirmation_sent = self._post_paid_slack_confirmation(purchase)

        return {
            'received': True,
            'purchase_id': str(purchase.id),
            'credited': credited,
            'slack_confirmation_sent': slack_confirmation_sent,
        }

    def post(self, request):
        payload = request.body
        signature_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

        try:
            self._verify_stripe_signature(payload, signature_header)
            event = json.loads(payload.decode('utf-8'))
        except RuntimeError as exc:
            logger.warning('Stripe webhook is not configured: %s', exc)
            return Response({'error': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        event_type = event.get('type')
        if event_type != 'checkout.session.completed':
            return Response({'received': True, 'ignored': True})

        session = (event.get('data') or {}).get('object') or {}
        try:
            result = self._handle_checkout_completed(session)
        except (PointsPurchase.DoesNotExist, ValidationError):
            return Response({'error': 'Points purchase not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class PointsAdminViewSet(mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.ReadOnlyModelViewSet):
    """
    View for Points Admins.
    Reads are available to Roo.
    Writes are restricted to the single points super-admin requester.
    """
    queryset = PointsAdmin.objects.filter(is_active=True)
    serializer_class = PointsAdminSerializer
    lookup_field = 'slack_user_id'
    permission_classes = [HasRooApiKey]

    def _clean_slack_id(self, value):
        if value is None:
            return ""
        return str(value).strip()

    def _require_super_admin_requester(self, request):
        requester_slack_id = self._clean_slack_id(
            request.data.get('requester_slack_id') or request.query_params.get('requester_slack_id')
        )
        if not requester_slack_id:
            return None, Response(
                {'error': 'requester_slack_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not is_points_super_admin(requester_slack_id):
            return None, Response(
                {'error': 'Only the Roo points super admin can manage points admins'},
                status=status.HTTP_403_FORBIDDEN,
            )

        return requester_slack_id, None

    def create(self, request, *args, **kwargs):
        """Promote a Slack user to Points Admin."""
        requester_slack_id, error_response = self._require_super_admin_requester(request)
        if error_response:
            return error_response

        target_slack_id = self._clean_slack_id(request.data.get('target_slack_id'))
        if not target_slack_id:
            return Response(
                {'error': 'target_slack_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        admin = PointsAdmin.objects.filter(slack_user_id=target_slack_id).first()
        linked_user = PointsService.get_user_by_slack_id(target_slack_id)

        if admin and admin.is_active:
            changed_fields = []
            if linked_user and admin.user_id != linked_user.id:
                admin.user = linked_user
                changed_fields.append('user')
            if changed_fields:
                admin.save(update_fields=changed_fields)

            data = PointsAdminSerializer(admin).data
            data.update({
                'target_slack_id': target_slack_id,
                'already_admin': True,
                'created': False,
            })
            return Response(data, status=status.HTTP_200_OK)

        if admin:
            admin.is_active = True
            admin.added_by_slack_id = requester_slack_id
            if linked_user:
                admin.user = linked_user
            admin.save()
            status_code = status.HTTP_200_OK
        else:
            admin = PointsAdmin.objects.create(
                slack_user_id=target_slack_id,
                user=linked_user,
                role='committee',
                is_active=True,
                added_by_slack_id=requester_slack_id,
            )
            status_code = status.HTTP_201_CREATED

        data = PointsAdminSerializer(admin).data
        data.update({
            'target_slack_id': target_slack_id,
            'already_admin': False,
            'created': status_code == status.HTTP_201_CREATED,
        })
        return Response(data, status=status_code)

    def update(self, request, *args, **kwargs):
        """Treat PUT the same as PATCH for the points admin allowance update contract."""
        kwargs['partial'] = True
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        """Update a specific Points Admin's weekly allowance."""
        requester_slack_id, error_response = self._require_super_admin_requester(request)
        if error_response:
            return error_response

        weekly_allowance = request.data.get('weekly_allowance')
        if weekly_allowance is None:
            return Response(
                {'error': 'weekly_allowance is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            weekly_allowance = int(weekly_allowance)
        except (TypeError, ValueError):
            return Response(
                {'error': 'weekly_allowance must be an integer'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if weekly_allowance <= 0:
            return Response(
                {'error': 'weekly_allowance must be positive'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_slack_id = self.kwargs.get(self.lookup_field)
        admin = PointsAdmin.objects.filter(
            slack_user_id=target_slack_id,
            is_active=True,
        ).first()
        if not admin:
            return Response({'error': 'Not a points admin'}, status=status.HTTP_404_NOT_FOUND)

        admin.weekly_allowance = weekly_allowance
        admin.save(update_fields=['weekly_allowance'])

        data = PointsAdminSerializer(admin).data
        data.update({
            'requester_slack_id': requester_slack_id,
            'target_slack_id': target_slack_id,
            'weekly_allowance': admin.weekly_allowance,
        })
        return Response(data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        """Soft-revoke a specific Points Admin."""
        requester_slack_id, error_response = self._require_super_admin_requester(request)
        if error_response:
            return error_response

        target_slack_id = self.kwargs.get(self.lookup_field)
        admin = PointsAdmin.objects.filter(slack_user_id=target_slack_id).first()
        if not admin:
            return Response({'error': 'Not a points admin'}, status=status.HTTP_404_NOT_FOUND)

        if not admin.is_active:
            return Response(
                {
                    'requester_slack_id': requester_slack_id,
                    'target_slack_id': target_slack_id,
                    'revoked': False,
                    'already_revoked': True,
                },
                status=status.HTTP_200_OK,
            )

        admin.is_active = False
        admin.save(update_fields=['is_active'])
        return Response(
            {
                'requester_slack_id': requester_slack_id,
                'target_slack_id': target_slack_id,
                'revoked': True,
            },
            status=status.HTTP_200_OK,
        )


class AdminAllowanceView(APIView):
    """
    Get admin's weekly point allowance status.
    Returns allowance, used, and remaining points.
    """
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request):
        slack_id = request.query_params.get('slack_id')
        if not slack_id:
            return Response({'error': 'slack_id required'}, status=status.HTTP_400_BAD_REQUEST)
        
        result = PointsService.get_admin_allowance_status(slack_id)
        if 'error' in result:
            return Response(result, status=status.HTTP_404_NOT_FOUND)
        return Response(result)

# Backwards compatibility
MinterViewSet = PointsAdminViewSet


class TaskViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Tasks with full workflow support.
    """
    TASK_EDITABLE_FIELDS = {
        'title',
        'description',
        'points',
        'portfolio',
        'work_domain',
        'review_flow',
        'reviewer_slack_id',
        'fallback_reviewer_slack_id',
        'repo',
        'estimate_minutes',
        'difficulty',
        'due_date',
        'volunteer_ready',
        'acceptance_criteria',
        'how_to_test',
        'definition_of_done',
        'blocked_reason',
    }
    TASK_EDIT_META_FIELDS = {
        'slack_user_id',
        'created_by_user_id',
        'expected_updated_at',
        'task_title',
    }
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get_queryset(self):
        qs = super().get_queryset().select_related('assigned_user').prefetch_related('assignments', 'submissions')
        params = self.request.query_params

        status_param = params.get('status')
        portfolio_param = params.get('portfolio')
        task_code = params.get('task_code')
        work_domain = params.get('work_domain')
        review_flow = params.get('review_flow')
        group_key = params.get('group_key')
        reviewer_slack_id = params.get('reviewer_slack_id')
        assigned_to_me = params.get('assigned_to_me')
        claimable = self._parse_bool_param(params.get('claimable'))
        volunteer_ready = self._parse_bool_param(params.get('volunteer_ready'))
        needs_review = self._parse_bool_param(params.get('needs_review'))

        if status_param:
            qs = qs.filter(status=status_param)
        if portfolio_param:
            qs = qs.filter(portfolio=portfolio_param)
        if task_code:
            qs = qs.filter(task_code__iexact=task_code.strip())
        if work_domain:
            qs = qs.filter(work_domain=work_domain)
        if review_flow:
            qs = qs.filter(review_flow=review_flow)
        if group_key:
            qs = qs.filter(group_key=group_key)
        if volunteer_ready is not None:
            qs = qs.filter(volunteer_ready=volunteer_ready)
        if reviewer_slack_id:
            qs = qs.filter(
                Q(reviewer_slack_id=reviewer_slack_id) |
                Q(fallback_reviewer_slack_id=reviewer_slack_id)
            )
        if assigned_to_me:
            qs = qs.filter(
                Q(assignments__assigned_to_slack_id=assigned_to_me, assignments__status__in=TaskAssignment.ACTIVE_STATUSES) |
                Q(assigned_to_user_id=assigned_to_me)
            )
        if claimable:
            qs = qs.filter(status='open', volunteer_ready=True).exclude(
                assignments__status__in=TaskAssignment.ACTIVE_STATUSES
            )
        if needs_review:
            qs = qs.filter(status='submitted')

        return qs.distinct().order_by('-created_at')

    def _parse_bool_param(self, value):
        if value is None:
            return None
        return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

    def _serialize_task(self, task):
        return TaskSerializer(task, context={'request': self.request}).data

    def _task_edit_help(self):
        editable_fields = ", ".join(sorted(self.TASK_EDITABLE_FIELDS))
        return f"You can edit: {editable_fields}."

    def _coerce_expected_updated_at(self, expected_updated_at: str):
        expected_dt = parse_datetime((expected_updated_at or '').strip())
        if expected_dt is None:
            raise ValueError('expected_updated_at must be a valid ISO-8601 datetime')
        return expected_dt

    def _validate_task_update_request(self, task: Task, data):
        actor_slack_id = data.get('created_by_user_id') or data.get('slack_user_id')
        require_linked_points_admin(actor_slack_id, action_label='edit tasks')

        expected_updated_at = data.get('expected_updated_at')
        if not expected_updated_at:
            raise ValueError('expected_updated_at is required')

        expected_dt = self._coerce_expected_updated_at(expected_updated_at)
        if expected_dt != task.updated_at:
            return actor_slack_id, Response(
                {
                    'error': 'Task changed since you last saw it. Refresh the task and try again.',
                    'task': self._serialize_task(task),
                },
                status=status.HTTP_409_CONFLICT,
            )

        requested_fields = {
            key
            for key in data.keys()
            if key not in self.TASK_EDIT_META_FIELDS
        }
        if not requested_fields:
            raise ValueError(self._task_edit_help())

        unsupported_fields = sorted(requested_fields - self.TASK_EDITABLE_FIELDS)
        if unsupported_fields:
            return actor_slack_id, Response(
                {
                    'error': self._task_edit_help(),
                    'unsupported_fields': unsupported_fields,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return actor_slack_id, None

    def create(self, request, *args, **kwargs):
        """Create a new task. Only Points Admins can create tasks."""
        
        # Make data mutable to handle bot inconsistencies
        data = request.data.copy()
        
        # 1. Map 'task_title' to 'title' if needed
        if 'task_title' in data and 'title' not in data:
            data['title'] = data['task_title']

        # 2. Map 'slack_user_id' to 'created_by_user_id' if needed
        # The bot sends 'slack_user_id' as the authenticated user/actor
        if 'created_by_user_id' not in data and 'slack_user_id' in data:
            data['created_by_user_id'] = data['slack_user_id']

        # Use the modified data
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        
        # Extract the creator's Slack ID for permission check
        creator_slack_id = data.get('created_by_user_id')
        
        if not creator_slack_id:
            return Response(
                {'error': 'created_by_user_id or slack_user_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            require_linked_points_admin(creator_slack_id, action_label='create tasks')
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        task = serializer.save()
        TaskService.ensure_task_code(task)
        TaskService.create_activity(
            task=task,
            event_type='created',
            actor_slack_id=creator_slack_id,
            summary='Task created',
        )
        if task.volunteer_ready:
            TaskService.create_activity(
                task=task,
                event_type='published',
                actor_slack_id=creator_slack_id,
                summary='Task marked volunteer-ready',
            )

        assigned_slack_id = data.get('assigned_to_user_id')
        if assigned_slack_id:
            user = get_or_create_user_for_slack_id(assigned_slack_id)
            try:
                TaskService.claim_task(task, user, assigned_slack_id)
            except ValueError:
                pass

        task.refresh_from_db()
        headers = self.get_success_headers(self._serialize_task(task))
        return Response(self._serialize_task(task), status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        """Treat PUT the same as PATCH for task edits."""
        return self.partial_update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        """Admin-only partial task edit with optimistic locking."""
        task = self.get_object()
        data = request.data.copy()

        if 'task_title' in data and 'title' not in data:
            data['title'] = data['task_title']

        try:
            actor_slack_id, error_response = self._validate_task_update_request(task, data)
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if error_response is not None:
            return error_response

        serializer_data = {
            key: value
            for key, value in data.items()
            if key in self.TASK_EDITABLE_FIELDS
        }
        serializer = self.get_serializer(task, data=serializer_data, partial=True)
        serializer.is_valid(raise_exception=True)
        task = serializer.save()
        TaskService.create_activity(
            task=task,
            event_type='updated',
            actor_slack_id=actor_slack_id,
            summary='Task details updated',
            metadata={'fields': sorted(serializer_data.keys())},
        )
        task.refresh_from_db()
        return Response(self._serialize_task(task))

    def destroy(self, request, *args, **kwargs):
        return Response(
            {'error': 'Hard delete is not supported for tasks. Use cancel instead.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    @action(detail=False, methods=['get'], url_path=r'by-code/(?P<task_code>[^/.]+)')
    def by_code(self, request, task_code=None):
        task = get_object_or_404(self.get_queryset(), task_code__iexact=task_code)
        return Response(self._serialize_task(task))

    @action(detail=True, methods=['post'])
    def claim(self, request, pk=None):
        """Claim a task."""
        task = self.get_object()
        slack_user_id = request.data.get('slack_user_id')

        if not slack_user_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        user = get_or_create_user_for_slack_id(slack_user_id)

        try:
            TaskService.claim_task(task, user, slack_user_id)
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        task.refresh_from_db()
        return Response(self._serialize_task(task))

    @action(detail=True, methods=['post'])
    def unclaim(self, request, pk=None):
        """Release a task back to the queue before any submission exists."""
        task = self.get_object()
        slack_user_id = request.data.get('slack_user_id')

        if not slack_user_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            assignment = TaskService.unclaim_task(task, slack_user_id)
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        task.refresh_from_db()
        return Response({
            'task': self._serialize_task(task),
            'assignment': TaskAssignmentSerializer(assignment).data,
        })

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Submit completed work for a task."""
        task = self.get_object()
        slack_user_id = request.data.get('slack_user_id')
        submission_text = request.data.get('submission_text', '')
        submission_url = request.data.get('submission_url')

        if not slack_user_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        user = get_or_create_user_for_slack_id(slack_user_id)
        try:
            _, submission = TaskService.submit_task(
                task=task,
                user=user,
                slack_user_id=slack_user_id,
                submission_text=submission_text or 'Submitted via API',
                submission_url=submission_url,
            )
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(TaskSubmissionSerializer(submission).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve a task submission and award points."""
        task = self.get_object()
        approver_slack_id = request.data.get('slack_user_id')
        submission_id = request.data.get('submission_id')
        awarded_points = request.data.get('awarded_points')
        review_notes = request.data.get('review_notes', '')

        if not approver_slack_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Get submission
        if submission_id:
            try:
                submission = task.submissions.get(id=submission_id)
            except TaskSubmission.DoesNotExist:
                return Response({'error': 'Submission not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            # Get latest submission
            submission = TaskService.get_latest_submitted_submission(task)
            if not submission:
                # Legacy flow: direct approve without submission
                return self._legacy_approve(task, approver_slack_id, awarded_points=awarded_points, review_notes=review_notes)

        try:
            if awarded_points is not None:
                awarded_points = int(awarded_points)
            submission, ledger, _ = TaskService.approve_submission(
                submission,
                approver_slack_id,
                awarded_points=awarded_points,
                review_notes=review_notes,
            )
            task.refresh_from_db()
            return Response({
                'task': self._serialize_task(task),
                'submission': TaskSubmissionSerializer(submission).data,
                'points_awarded': ledger.delta,
            })
        except PermissionDeniedError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def _legacy_approve(self, task, approver_slack_id, *, awarded_points=None, review_notes=''):
        """Legacy approval flow for tasks without submissions."""
        if task.status not in ['claimed', 'submitted']:
            return Response({'error': 'Task is not in a state to be approved'}, status=status.HTTP_400_BAD_REQUEST)

        # Get assigned user
        user = task.assigned_user
        if not user and task.assigned_to_user_id:
            user = get_or_create_user_for_slack_id(task.assigned_to_user_id)

        if not user:
            return Response({'error': 'No user assigned to this task'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if awarded_points is not None:
                awarded_points = int(awarded_points)
            _, ledger = TaskService.approve_assignment_without_submission(
                task,
                user,
                approver_slack_id,
                slack_user_id=user.slack_id or task.assigned_to_user_id,
                awarded_points=awarded_points,
                review_notes=review_notes,
            )
            task.refresh_from_db()
        except PermissionDeniedError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'task': self._serialize_task(task),
            'points_awarded': ledger.delta,
            'created': True,
        })
    
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject a task submission."""
        task = self.get_object()
        rejector_slack_id = request.data.get('slack_user_id')
        reason = request.data.get('reason', '')
        submission_id = request.data.get('submission_id')

        try:
            submission, assignment = TaskService.reject_submission(
                task,
                rejector_slack_id,
                reason=reason,
                submission_id=submission_id,
            )
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        task.refresh_from_db()
        return Response({
            'task': self._serialize_task(task),
            'submission': TaskSubmissionSerializer(submission).data,
            'assignment': TaskAssignmentSerializer(assignment).data if assignment else None,
        })
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a task."""
        task = self.get_object()
        actor_slack_id = request.data.get('slack_user_id') or request.data.get('created_by_user_id')
        reason = request.data.get('reason', '')
        try:
            require_linked_points_admin(actor_slack_id, action_label='cancel tasks')
            task, active_assignment = TaskService.cancel_task(task, actor_slack_id, reason=reason)
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        task.refresh_from_db()
        return Response(self._serialize_task(task))

    @action(detail=True, methods=['post'])
    def award(self, request, pk=None):
        """Direct award: claim + approve in one step."""
        task = self.get_object()
        assignee_slack_id = request.data.get('assigned_to_user_id')
        approver_slack_id = request.data.get('created_by_user_id') or request.data.get('slack_user_id')

        if not assignee_slack_id:
            return Response({'error': 'assigned_to_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            require_linked_points_admin(approver_slack_id, action_label='award task points')
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        user = get_or_create_user_for_slack_id(assignee_slack_id)
        try:
            if not task.get_active_assignment():
                TaskService.claim_task(task, user, assignee_slack_id)
            _, ledger = TaskService.approve_assignment_without_submission(
                task,
                user,
                approver_slack_id,
                slack_user_id=assignee_slack_id,
            )
            task.refresh_from_db()
        except PermissionDeniedError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'task': self._serialize_task(task),
            'points_awarded': ledger.delta,
        })

    # Legacy endpoint for backwards compatibility
    @action(detail=True, methods=['post'], url_path='request-complete')
    def request_complete(self, request, pk=None):
        """Legacy: Mark task as pending approval."""
        task = self.get_object()
        requester_id = request.data.get('slack_user_id')

        if not requester_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        user = get_or_create_user_for_slack_id(requester_id)
        try:
            TaskService.submit_task(
                task=task,
                user=user,
                slack_user_id=requester_id,
                submission_text='Completion requested via legacy endpoint',
                evidence_kind='legacy_request_complete',
                evidence_payload={'source': 'request-complete'},
            )
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        task.refresh_from_db()
        return Response(self._serialize_task(task))


class UserBalanceViewSet(viewsets.ViewSet):
    """Get points balance for a user by Slack ID."""
    permission_classes = [HasAPIKey | HasRooApiKey]

    def retrieve(self, request, pk=None):
        slack_user_id = pk
        
        user = PointsService.get_user_by_slack_id(slack_user_id)
        if user:
            balance_data = PointsService.get_balance(user)
            available_microroo = PointsService.get_available_microroo(user)
            total_microroo = balance_data['balance_microroo']
            reserved_microroo = max(total_microroo - available_microroo, 0)
            
            data = {
                'slack_user_id': slack_user_id,
                'email': user.email,
                # Legacy callers have always treated ``balance`` as spendable.
                # Coding reservations must therefore be deducted even though
                # the underlying account total is not debited until settlement.
                'balance': PointsService.microroo_to_legacy_whole(available_microroo),
                'earned_balance': balance_data['earned_balance'],
                'purchased_topup_balance': balance_data['purchased_topup_balance'],
                'lifetime_earned': balance_data['lifetime_earned'],
                'lifetime_purchased_topup': balance_data['lifetime_purchased_topup'],
                'lifetime_spent': balance_data['lifetime_spent'],
                'expired_or_reversed_points': balance_data['expired_or_reversed_points'],
                'balance_microroo': str(available_microroo),
                'balance_roo': roo_decimal_string(available_microroo),
                'reserved_microroo': str(reserved_microroo),
                'reserved_roo': roo_decimal_string(reserved_microroo),
                'total_balance_microroo': str(total_microroo),
                'total_balance_roo': roo_decimal_string(total_microroo),
                'annual_balance': balance_data['lifetime_earned'],  # For backwards compat
                'lifetime_balance': balance_data['lifetime_earned'],  # For backwards compat
            }
        else:
            # Legacy: calculate from ledger by slack_user_id field
            total = Ledger.objects.filter(slack_user_id=slack_user_id).aggregate(Sum('points_delta'))['points_delta__sum'] or 0
            current_year = timezone.now().year
            annual = Ledger.objects.filter(
                slack_user_id=slack_user_id, 
                created_at__year=current_year
            ).aggregate(Sum('points_delta'))['points_delta__sum'] or 0

            data = {
                'slack_user_id': slack_user_id,
                'annual_balance': annual,
                'lifetime_balance': total,
                'balance': total,
            }
        
        return Response(data)


class CurrentUserBalanceView(APIView):
    """Get the authenticated user's current Roo Points balance."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        balance_data = PointsService.get_balance(user)
        available_microroo = PointsService.get_available_microroo(user)
        total_microroo = balance_data['balance_microroo']
        reserved_microroo = max(total_microroo - available_microroo, 0)
        return Response(
            {
                'user_id': user.id,
                'email': user.email,
                'slack_user_id': user.slack_id,
                'balance': PointsService.microroo_to_legacy_whole(available_microroo),
                'earned_balance': balance_data['earned_balance'],
                'purchased_topup_balance': balance_data['purchased_topup_balance'],
                'lifetime_earned': balance_data['lifetime_earned'],
                'lifetime_purchased_topup': balance_data['lifetime_purchased_topup'],
                'lifetime_spent': balance_data['lifetime_spent'],
                'expired_or_reversed_points': balance_data['expired_or_reversed_points'],
                'balance_microroo': str(available_microroo),
                'balance_roo': roo_decimal_string(available_microroo),
                'reserved_microroo': str(reserved_microroo),
                'reserved_roo': roo_decimal_string(reserved_microroo),
                'total_balance_microroo': str(total_microroo),
                'total_balance_roo': roo_decimal_string(total_microroo),
            },
            status=status.HTTP_200_OK,
        )


class KimiPromptUsageView(APIView):
    """Atomically debit an MLAI account for one Kimi Code prompt.

    Cloudflare Access supplies the developer email to the Kimi gateway. The
    gateway calls this service endpoint with Roo's private API credential. The
    caller controls only the identity and idempotency key; pricing remains a
    backend setting so it cannot be reduced by a modified browser request.
    """

    permission_classes = [HasStrictRooApiKey]
    IDEMPOTENCY_RE = re.compile(r'^[A-Za-z0-9._:-]{16,180}$')

    @staticmethod
    def _error(code: str, message: str, http_status: int) -> Response:
        return Response(
            {'code': code, 'message': message},
            status=http_status,
        )

    @staticmethod
    def _active_user(email_value):
        email = User.objects.normalize_email(email_value)
        if not email or len(email) > 254 or '@' not in email:
            return email, None
        return email, User.objects.filter(email__iexact=email, is_active=True).first()

    def get(self, request):
        email, user = self._active_user(request.query_params.get('email'))
        if not email or user is None:
            code = 'invalid_email' if not email or '@' not in email else 'account_not_found'
            message = (
                'A valid MLAI account email is required.'
                if code == 'invalid_email'
                else 'No active MLAI account is linked to this email.'
            )
            return self._error(
                code,
                message,
                status.HTTP_400_BAD_REQUEST
                if code == 'invalid_email'
                else status.HTTP_404_NOT_FOUND,
            )

        balance = PointsService.get_balance(user)['balance']
        return Response(
            {
                'balance': balance,
                'prompt_cost_points': settings.KIMI_ROO_POINTS_PER_PROMPT,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        email, user = self._active_user(request.data.get('email'))
        request_key = str(request.data.get('idempotency_key') or '').strip()
        session_id = str(request.data.get('session_id') or '').strip()

        if not email or len(email) > 254 or '@' not in email:
            return self._error(
                'invalid_email',
                'A valid MLAI account email is required.',
                status.HTTP_400_BAD_REQUEST,
            )
        if not self.IDEMPOTENCY_RE.fullmatch(request_key):
            return self._error(
                'invalid_idempotency_key',
                'idempotency_key must contain 16-180 safe characters.',
                status.HTTP_400_BAD_REQUEST,
            )
        if len(session_id) > 100:
            return self._error(
                'invalid_session_id',
                'session_id must be 100 characters or fewer.',
                status.HTTP_400_BAD_REQUEST,
            )

        if user is None:
            return self._error(
                'account_not_found',
                'No active MLAI account is linked to this email.',
                status.HTTP_404_NOT_FOUND,
            )

        points = settings.KIMI_ROO_POINTS_PER_PROMPT
        ledger_key = f'kimi_prompt:{request_key}'
        try:
            ledger, created = PointsService.spend(
                user=user,
                delta=points,
                source='TOOLS',
                description='Kimi Code prompt',
                created_by_slack_id='KIMI_CODE',
                idempotency_key=ledger_key,
                reference_type='KIMI_PROMPT',
                reference_id=session_id or None,
            )
        except InsufficientBalanceError:
            balance = PointsService.get_balance(user)['balance']
            return Response(
                {
                    'code': 'insufficient_points',
                    'message': f'{points} Roo Point is required for this prompt.'
                    if points == 1
                    else f'{points} Roo Points are required for this prompt.',
                    'required_points': points,
                    'balance': balance,
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        except IdempotencyConflictError:
            return self._error(
                'idempotency_conflict',
                'That idempotency key was already used for a different operation.',
                status.HTTP_409_CONFLICT,
            )

        # PointsService is globally idempotent. Reject a key collision instead
        # of treating another user's or another operation's ledger row as this
        # request. This also covers the IntegrityError race path in spend().
        if (
            ledger.user_id != user.id
            or ledger.kind != 'SPEND'
            or ledger.source != 'TOOLS'
            or ledger.reference_type != 'KIMI_PROMPT'
            or ledger.delta != -points
        ):
            return self._error(
                'idempotency_conflict',
                'That idempotency key was already used for a different operation.',
                status.HTTP_409_CONFLICT,
            )

        balance = PointsService.get_balance(user)['balance']
        return Response(
            {
                'status': 'charged' if created else 'already_charged',
                'charged': created,
                'charged_points': points if created else 0,
                'prompt_cost_points': points,
                'balance': balance,
                'ledger_entry_id': ledger.id,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CommitteeCandidateEmailsView(APIView):
    """Return a private, copy-ready list of eligible member emails."""

    permission_classes = [HasStrictRooApiKey]

    def post(self, request):
        requester_slack_id = clean_slack_id(request.data.get('requester_slack_id'))
        if not requester_slack_id:
            return Response(
                {'code': 'requester_required', 'error': 'requester_slack_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not can_list_committee_candidate_emails(requester_slack_id):
            return Response(
                {
                    'code': 'committee_admin_only',
                    'error': 'Only active admin or committee Points Admins can list candidate emails',
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(CommitteeCandidateEmailService.list_emails())


class BoostPostAdmissionView(APIView):
    """Price and atomically debit one direct #boost-my-startup root post."""

    permission_classes = [HasStrictRooApiKey]
    REQUIRED_FIELDS = (
        'submission_key',
        'workspace_id',
        'channel_id',
        'root_message_ts',
        'poster_slack_id',
    )

    @staticmethod
    def _response(admission: BoostPostAdmission) -> dict:
        return {
            'admission_id': admission.id,
            'status': admission.status,
            'submission_key': admission.submission_key,
            'base_cost_points': admission.base_cost_points,
            'charged_points': admission.charged_points,
            'discount_applied': admission.discount_applied,
            'balance_before': admission.balance_before,
            'new_balance': admission.new_balance,
            'ledger_entry_id': admission.ledger_entry_id,
            'message': admission.rejection_message,
        }

    def post(self, request):
        values = {
            key: str(request.data.get(key) or '').strip()
            for key in self.REQUIRED_FIELDS
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            return Response(
                {
                    'status': 'invalid_post',
                    'code': 'invalid_post',
                    'message': 'Missing required fields: ' + ', '.join(missing),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        values['social_post_url'] = str(
            request.data.get('social_post_url') or ''
        ).strip()[:2048]
        root_text = str(request.data.get('root_text') or '')[:10000]
        recheck_insufficient_points = request.data.get(
            'recheck_insufficient_points',
            False,
        )
        if not isinstance(recheck_insufficient_points, bool):
            return Response(
                {
                    'status': 'invalid_post',
                    'code': 'invalid_post',
                    'message': 'recheck_insufficient_points must be a boolean',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            admission, created = BoostPostAdmissionService.admit(
                **values,
                root_text=root_text,
                recheck_insufficient_points=recheck_insufficient_points,
            )
        except InvalidBoostPostError as exc:
            return Response(
                {
                    'status': 'invalid_post',
                    'code': 'invalid_post',
                    'message': str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except BoostPostPayloadConflictError as exc:
            return Response(
                {
                    'status': 'invalid_post',
                    'code': 'idempotency_conflict',
                    'message': str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )

        data = self._response(admission)
        data['idempotent_replay'] = not created
        data['recheck_requested'] = recheck_insufficient_points
        if admission.status == 'approved':
            return Response(
                data,
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        if admission.status == 'insufficient_points':
            data['code'] = 'insufficient_points'
            return Response(data, status=status.HTTP_402_PAYMENT_REQUIRED)
        if admission.status == 'member_unlinked':
            data['code'] = 'member_unlinked'
            return Response(data, status=status.HTTP_404_NOT_FOUND)
        logger.error('Boost post admission %s remained processing', admission.id)
        return Response(
            {
                **data,
                'code': 'admission_incomplete',
                'message': 'Boost post admission did not reach a terminal state',
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class LedgerViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only view of ledger entries."""
    queryset = Ledger.objects.all()
    serializer_class = LedgerSerializer
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get_queryset(self):
        qs = super().get_queryset()
        slack_user_id = self.request.query_params.get('slack_user_id')
        user_id = self.request.query_params.get('user_id')
        source = self.request.query_params.get('source')
        kind = self.request.query_params.get('kind')

        if slack_user_id:
            user = PointsService.get_user_by_slack_id(slack_user_id)
            if user:
                qs = qs.filter(user=user)
            else:
                qs = qs.filter(slack_user_id=slack_user_id)
        
        if user_id:
            qs = qs.filter(user_id=user_id)
        if source:
            qs = qs.filter(source=source)
        if kind:
            qs = qs.filter(kind=kind)

        return qs.order_by('-created_at')[:100]


class CoworkingViewSet(viewsets.ViewSet):
    """Coworking booking management."""
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get_permissions(self):
        # This action is also wired through an explicit URL, so enforce its
        # narrower service credential here rather than relying on router-only
        # @action metadata.
        if self.action == 'book_many':
            return [HasStrictRooApiKey()]
        return super().get_permissions()

    @action(detail=False, methods=['get'])
    def availability(self, request):
        """Check availability for dates."""
        date_param = request.query_params.get('date')
        days_ahead = int(request.query_params.get('days', 7))
        slack_user_id = (request.query_params.get('slack_user_id') or '').strip()

        start_date = date.today()
        if date_param:
            start_date = date.fromisoformat(date_param)

        # When the caller identifies the user we can quote the price they would
        # actually be charged (which can drop with a 'ready' monthly update, and
        # can differ per date across a month boundary). Without a user we quote
        # the standard price.
        user = PointsService.get_user_by_slack_id(slack_user_id) if slack_user_id else None

        results = []
        standard_cost = CoworkingService.get_standard_coworking_cost()

        for i in range(days_ahead):
            check_date = start_date + timedelta(days=i)
            available, capacity = CoworkingService.check_availability(check_date)
            if user is not None:
                cost_points = CoworkingService.get_coworking_cost(
                    user=user, booking_date=check_date
                )
            else:
                cost_points = standard_cost
            results.append({
                'date': check_date.isoformat(),
                'available_slots': available,
                'total_capacity': capacity,
                'cost_points': cost_points,
                'is_bookable': available > 0,
            })

        return Response(results)

    @action(detail=False, methods=['get'])
    def report(self, request):
        """Points-admin-only active coworking booking report."""
        slack_user_id = (request.query_params.get('slack_user_id') or '').strip()
        start_date_param = request.query_params.get('start_date')
        end_date_param = request.query_params.get('end_date')

        if not slack_user_id:
            return Response(
                {'error': 'slack_user_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not can_generate_coworking_reports(slack_user_id):
            return Response(
                {'error': 'Only Roo Points Admins can generate coworking reports'},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not start_date_param or not end_date_param:
            return Response(
                {'error': 'start_date and end_date are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            start_date = date.fromisoformat(start_date_param)
            end_date = date.fromisoformat(end_date_param)
            report = CoworkingService.build_report(start_date, end_date)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(report)

    @action(detail=False, methods=['post'])
    def book(self, request):
        """Book a coworking day. Users can only book for today."""
        slack_user_id = request.data.get('slack_user_id')
        booking_date_str = request.data.get('date')
        slack_channel_id = request.data.get('slack_channel_id')
        raw_operation_id = request.data.get('operation_id')

        if not slack_user_id or not booking_date_str:
            return Response(
                {'error': 'slack_user_id and date are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Parse and validate date
        try:
            booking_date = date.fromisoformat(booking_date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )

        slack_user_id = clean_slack_id(slack_user_id)
        try:
            operation_id, operation_fingerprint, operation_receipt = (
                _coworking_operation_receipt(
                    raw_id=raw_operation_id,
                    kind='single',
                    request_fields={
                        'slack_user_id': slack_user_id,
                        'date': booking_date.isoformat(),
                    },
                )
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        if operation_receipt:
            return _coworking_replay_response(operation_receipt)
        
        today = timezone.now().date()
        max_date = today + timedelta(days=7)

        if booking_date < today:
             return Response(
                {'error': f'Cannot book dates in the past. Today is {today.isoformat()}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if booking_date > max_date:
            return Response(
                {'error': f'Cannot book for more than 7 days in advance. Max date is {max_date.isoformat()}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Points Admin awards create a Slack-scoped points owner immediately,
        # even when the member has never run the account-link flow. Resolve
        # that owner first so their granted balance remains directly usable.
        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            profile = SlackService.get_user_profile(slack_user_id)
            if profile is None:
                return Response(
                    {
                        'code': 'slack_identity_unavailable',
                        'error': 'Could not verify your Slack account right now. Please try again.',
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            user = resolve_existing_user_from_profile(
                slack_user_id=slack_user_id,
                profile=profile,
            )
        if not user:
            return Response(
                {
                    'code': 'slack_account_not_linked',
                    'error': 'Please link your Slack account first',
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                booking, created = CoworkingService.book(
                    user=user,
                    booking_date=booking_date,
                    created_by_slack_id=slack_user_id,
                    slack_channel_id=slack_channel_id,
                )
                response_data = dict(CoworkingBookingSerializer(booking).data)
                # Surface whether the monthly-update discount applied so callers
                # (e.g. Roo) don't have to hardcode the price. The standard cost is
                # the single source of truth in get_standard_coworking_cost().
                standard_cost = CoworkingService.get_standard_coworking_cost()
                response_data["standard_points_cost"] = standard_cost
                response_data["monthly_update_discount_applied"] = booking.points_cost < standard_cost
                connection_type = founder_tools_connection_type(booking.user)
                response_data["founder_tools_connection_type"] = connection_type
                response_data["founder_tools_account_linked"] = (
                    connection_type is not None
                )
                response_data["founder_tools_explicitly_linked"] = (
                    connection_type == "explicit"
                )
                response_status = (
                    status.HTTP_201_CREATED if created else status.HTTP_200_OK
                )
                if not created:
                    response_data["already_booked"] = True
                    response_data["idempotent"] = True
                if operation_id:
                    receipt, receipt_created = (
                        CoworkingBookingOperation.objects.get_or_create(
                            id=operation_id,
                            defaults={
                                'kind': 'single',
                                'request_fingerprint': operation_fingerprint,
                                'response_payload': _coworking_receipt_payload(
                                    response_data, kind='single'
                                ),
                                'http_status': response_status,
                            },
                        )
                    )
                    if receipt_created:
                        receipt.subjects.set([user])
                    if not receipt_created:
                        if (
                            receipt.kind != 'single'
                            or receipt.request_fingerprint != operation_fingerprint
                        ):
                            raise ValueError(
                                'operation_id was already used for a different request'
                            )
                        return _coworking_replay_response(receipt)
            return Response(response_data, status=response_status)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientBalanceError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='book-many')
    def book_many(self, request):
        """Admin-only atomic coworking booking for one or more tagged users."""
        admin_slack_user_id = clean_slack_id(
            request.data.get('admin_slack_user_id') or request.data.get('slack_user_id')
        )
        raw_target_ids = request.data.get('target_slack_user_ids')
        booking_date_str = request.data.get('date')
        slack_channel_id = request.data.get('slack_channel_id')
        raw_operation_id = request.data.get('operation_id')

        if not admin_slack_user_id or not booking_date_str:
            return Response(
                {'error': 'admin_slack_user_id and date are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if raw_target_ids is None:
            return Response(
                {'error': 'target_slack_user_ids is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if isinstance(raw_target_ids, str):
            raw_target_ids = [raw_target_ids]
        if not isinstance(raw_target_ids, list):
            return Response(
                {'error': 'target_slack_user_ids must be a list of Slack user IDs'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_slack_ids = []
        for raw_target_id in raw_target_ids:
            cleaned = clean_slack_id(raw_target_id)
            if cleaned and cleaned not in target_slack_ids:
                target_slack_ids.append(cleaned)

        if not target_slack_ids:
            return Response(
                {'error': 'At least one target Slack user ID is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            booking_date = date.fromisoformat(booking_date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            operation_id, operation_fingerprint, operation_receipt = (
                _coworking_operation_receipt(
                    raw_id=raw_operation_id,
                    kind='batch',
                    request_fields={
                        'admin_slack_user_id': admin_slack_user_id,
                        'target_slack_user_ids': sorted(target_slack_ids),
                        'date': booking_date.isoformat(),
                    },
                )
            )
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_409_CONFLICT)
        if operation_receipt:
            return _coworking_replay_response(
                operation_receipt,
                admin_slack_user_id=admin_slack_user_id,
                target_slack_user_ids=target_slack_ids,
            )

        if not is_points_admin(admin_slack_user_id):
            return Response(
                {'error': 'Only Roo Points Admins can book coworking for other users'},
                status=status.HTTP_403_FORBIDDEN,
            )

        today = timezone.now().date()
        max_date = today + timedelta(days=7)

        if booking_date < today:
            return Response(
                {'error': f'Cannot book dates in the past. Today is {today.isoformat()}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if booking_date > max_date:
            return Response(
                {'error': f'Cannot book for more than 7 days in advance. Max date is {max_date.isoformat()}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        users_by_slack_id = {
            user.slack_id: user
            for user in User.objects.filter(
                slack_id__in=target_slack_ids,
                is_active=True,
            )
        }
        missing_target_ids = [
            slack_id for slack_id in target_slack_ids if slack_id not in users_by_slack_id
        ]
        if missing_target_ids:
            return Response(
                {
                    'error': 'One or more target users need to link their Slack account first',
                    'errors': [
                        {
                            'slack_user_id': slack_id,
                            'error': 'Please link your Slack account first',
                        }
                        for slack_id in missing_target_ids
                    ],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_users = [users_by_slack_id[slack_id] for slack_id in target_slack_ids]

        try:
            with transaction.atomic():
                booking_results = CoworkingService.book_many(
                    target_users=target_users,
                    booking_date=booking_date,
                    created_by_slack_id=admin_slack_user_id,
                    slack_channel_id=slack_channel_id,
                )

                standard_cost = CoworkingService.get_standard_coworking_cost()
                explicitly_linked_user_ids = set(
                    SlackFounderAccountLink.objects.filter(
                        slack_user_id__in=[
                            booking.user_id for booking, _created in booking_results
                        ],
                        slack_user__is_active=True,
                        founder_user__is_active=True,
                    ).values_list('slack_user_id', flat=True)
                )
                results = []
                created_count = 0
                for booking, created in booking_results:
                    created_count += 1 if created else 0
                    connection_type = founder_tools_connection_type(
                        booking.user,
                        explicitly_linked=(
                            booking.user_id in explicitly_linked_user_ids
                        ),
                    )
                    results.append({
                        'slack_user_id': booking.user.slack_id,
                        'created': created,
                        'already_booked': not created,
                        'booking': dict(CoworkingBookingSerializer(booking).data),
                        'points_cost': booking.points_cost,
                        'standard_points_cost': standard_cost,
                        'monthly_update_discount_applied': booking.points_cost < standard_cost,
                        'founder_tools_connection_type': connection_type,
                        'founder_tools_account_linked': connection_type is not None,
                        'founder_tools_explicitly_linked': connection_type == 'explicit',
                    })

                response_data = {
                    'date': booking_date.isoformat(),
                    'admin_slack_user_id': admin_slack_user_id,
                    'target_count': len(results),
                    'created_count': created_count,
                    'already_booked_count': len(results) - created_count,
                    'standard_points_cost': standard_cost,
                    'results': results,
                }
                response_status = (
                    status.HTTP_201_CREATED
                    if created_count
                    else status.HTTP_200_OK
                )
                if operation_id:
                    receipt, receipt_created = (
                        CoworkingBookingOperation.objects.get_or_create(
                            id=operation_id,
                            defaults={
                                'kind': 'batch',
                                'request_fingerprint': operation_fingerprint,
                                'response_payload': _coworking_receipt_payload(
                                    response_data, kind='batch'
                                ),
                                'http_status': response_status,
                            },
                        )
                    )
                    if receipt_created:
                        receipt_subjects = list(target_users)
                        admin_subject = User.objects.filter(
                            slack_id=admin_slack_user_id
                        ).first()
                        if admin_subject is not None:
                            receipt_subjects.append(admin_subject)
                        receipt.subjects.set(receipt_subjects)
                    if not receipt_created:
                        if (
                            receipt.kind != 'batch'
                            or receipt.request_fingerprint != operation_fingerprint
                        ):
                            raise ValueError(
                                'operation_id was already used for a different request'
                            )
                        return _coworking_replay_response(
                            receipt,
                            admin_slack_user_id=admin_slack_user_id,
                            target_slack_user_ids=target_slack_ids,
                        )
        except CoworkingBatchBookingError as e:
            return Response(
                {'error': str(e), 'errors': e.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientBalanceError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(response_data, status=response_status)

    @action(detail=False, methods=['post'])
    def cancel(self, request):
        """Cancel a booking."""
        slack_user_id = request.data.get('slack_user_id')
        booking_id = request.data.get('booking_id')
        booking_date = request.data.get('date')

        if not slack_user_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        user = PointsService.get_user_by_slack_id(slack_user_id)

        # Find booking by ID or date
        if booking_id:
            try:
                booking = CoworkingBooking.objects.get(id=booking_id)
            except CoworkingBooking.DoesNotExist:
                return Response({'error': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)
        elif booking_date and user:
            try:
                booking = CoworkingBooking.objects.get(
                    user=user,
                    date=date.fromisoformat(booking_date),
                    status='booked'
                )
            except CoworkingBooking.DoesNotExist:
                return Response({'error': 'Booking not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            return Response(
                {'error': 'booking_id or date required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check ownership (unless admin)
        if booking.user != user and not is_points_admin(slack_user_id):
            return Response({'error': 'Not authorized to cancel this booking'}, status=status.HTTP_403_FORBIDDEN)

        try:
            booking, refunded = CoworkingService.cancel(str(booking.id), slack_user_id)
            return Response({
                'booking': CoworkingBookingSerializer(booking).data,
                'refunded': refunded,
                'refund_amount': booking.points_cost if refunded else 0,
            })
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def my_bookings(self, request):
        """Get user's bookings."""
        slack_user_id = request.query_params.get('slack_user_id')
        booking_id = request.query_params.get('booking_id')
        if not slack_user_id:
            return Response({'error': 'slack_user_id required'}, status=status.HTTP_400_BAD_REQUEST)

        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        bookings = CoworkingBooking.objects.filter(user=user)
        if booking_id:
            try:
                booking_uuid = UUID(str(booking_id).strip())
            except (ValueError, TypeError, AttributeError):
                return Response(
                    {'error': 'booking_id must be a valid UUID'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            bookings = bookings.filter(pk=booking_uuid)
        else:
            bookings = bookings.order_by('-date')[:20]
        return Response(CoworkingBookingSerializer(bookings, many=True).data)

    @action(detail=False, methods=['post'], url_path='set-capacity')
    def set_capacity(self, request):
        """Admin: Set capacity for a date."""
        slack_user_id = request.data.get('slack_user_id')
        capacity_date = request.data.get('date')
        capacity = request.data.get('capacity')
        notes = request.data.get('notes')

        if not is_points_admin(slack_user_id):
            return Response({'error': 'Admin only'}, status=status.HTTP_403_FORBIDDEN)

        try:
            day_capacity = CoworkingService.set_capacity(
                capacity_date=date.fromisoformat(capacity_date),
                capacity=int(capacity),
                requester_slack_id=slack_user_id,
                notes=notes,
            )
            return Response(CoworkingDayCapacitySerializer(day_capacity).data)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'], url_path='booking-help')
    def booking_help(self, request):
        """Send a Slack booking reminder without exposing Slack APIs to clients."""
        slack_user_id = (request.data.get('slack_user_id') or '').strip()
        reason_code = (request.data.get('reason_code') or '').strip()
        access_event_id = (request.data.get('access_event_id') or '').strip()

        if not slack_user_id or not reason_code or not access_event_id:
            return Response(
                {'error': 'slack_user_id, reason_code, and access_event_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        text = (
            "Reachy could not open the office door because there is no active coworking booking for today. "
            "Please create a booking in Slack, then try again."
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Office booking required*\n"
                        "Reachy could not open the office door because there is no active coworking booking for today."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Reason: `{reason_code}` · Access event: `{access_event_id}`",
                    }
                ],
            },
        ]
        sent, message_ts = SlackService.send_dm(slack_user_id, text, blocks=blocks)
        if not sent:
            return Response({'error': 'Failed to send Slack booking help message'}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({'queued': True, 'message_ts': message_ts, 'access_event_id': access_event_id})


class RewardsViewSet(viewsets.ViewSet):
    """Rewards catalog and redemption management."""
    permission_classes = [HasAPIKey | HasRooApiKey]

    def list(self, request):
        """List available rewards."""
        slack_user_id = request.query_params.get('slack_user_id')
        user = None
        if slack_user_id:
            user = PointsService.get_user_by_slack_id(slack_user_id)

        rewards = RewardsService.list_available(user)
        return Response(rewards)

    @action(detail=False, methods=['post'])
    def request(self, request):
        """Request a reward redemption."""
        slack_user_id = request.data.get('slack_user_id')
        reward_code = request.data.get('reward_code')
        quantity = int(request.data.get('quantity', 1))
        notes = request.data.get('notes')
        slack_channel_id = request.data.get('slack_channel_id')
        slack_thread_ts = request.data.get('slack_thread_ts')

        if not slack_user_id or not reward_code:
            return Response(
                {'error': 'slack_user_id and reward_code required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response({'error': 'Please link your Slack account first'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            redemption = RewardsService.request_redemption(
                user=user,
                reward_code=reward_code,
                quantity=quantity,
                notes=notes,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
            )
            return Response(RewardRedemptionSerializer(redemption).data, status=status.HTTP_201_CREATED)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientBalanceError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'])
    def approve(self, request):
        """Admin: Approve a redemption request."""
        slack_user_id = request.data.get('slack_user_id')
        redemption_id = request.data.get('redemption_id')

        if not is_points_admin(slack_user_id):
            return Response({'error': 'Admin only'}, status=status.HTTP_403_FORBIDDEN)

        try:
            redemption = RewardRedemption.objects.get(id=redemption_id)
        except RewardRedemption.DoesNotExist:
            return Response({'error': 'Redemption not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            redemption = RewardsService.approve_redemption(redemption, slack_user_id)
            return Response(RewardRedemptionSerializer(redemption).data)
        except (ValueError, InsufficientBalanceError, PermissionDeniedError) as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Admin: List pending redemption requests."""
        slack_user_id = request.query_params.get('slack_user_id')
        
        if not is_points_admin(slack_user_id):
            return Response({'error': 'Admin only'}, status=status.HTTP_403_FORBIDDEN)

        redemptions = RewardRedemption.objects.filter(status='requested').order_by('requested_at')
        return Response(RewardRedemptionSerializer(redemptions, many=True).data)

    @action(detail=False, methods=['get'])
    def my_redemptions(self, request):
        """Get user's redemption history."""
        slack_user_id = request.query_params.get('slack_user_id')
        if not slack_user_id:
            return Response({'error': 'slack_user_id required'}, status=status.HTTP_400_BAD_REQUEST)

        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        redemptions = RewardRedemption.objects.filter(user=user).order_by('-requested_at')[:20]
        return Response(RewardRedemptionSerializer(redemptions, many=True).data)


class PointsRequestViewSet(viewsets.ViewSet):
    """Slack-driven pending points requests that admins can approve later."""
    permission_classes = [HasAPIKey | HasRooApiKey]

    def create(self, request):
        requester_slack_id = (request.data.get('requester_slack_id') or '').strip()
        target_slack_id = (request.data.get('target_slack_id') or '').strip()
        reason = (request.data.get('reason') or '').strip()
        points = request.data.get('points')

        if not requester_slack_id or not target_slack_id or points is None or not reason:
            return Response(
                {'error': 'requester_slack_id, target_slack_id, points and reason are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            points = int(points)
        except (TypeError, ValueError):
            return Response({'error': 'Points must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        if points <= 0:
            return Response({'error': 'Points must be a positive integer'}, status=status.HTTP_400_BAD_REQUEST)

        points_request = PointsRequest.objects.create(
            requester_slack_id=requester_slack_id,
            target_slack_id=target_slack_id,
            points=points,
            reason=reason,
            slack_channel_id=request.data.get('slack_channel_id'),
            slack_thread_ts=request.data.get('slack_thread_ts'),
        )
        return Response(PointsRequestSerializer(points_request).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='slack-summary')
    def attach_slack_summary(self, request, pk=None):
        try:
            points_request = PointsRequest.objects.get(pk=pk)
        except PointsRequest.DoesNotExist:
            return Response({'error': 'Points request not found'}, status=status.HTTP_404_NOT_FOUND)

        slack_channel_id = (request.data.get('slack_channel_id') or '').strip()
        slack_summary_message_ts = (request.data.get('slack_summary_message_ts') or '').strip()

        if not slack_channel_id or not slack_summary_message_ts:
            return Response(
                {'error': 'slack_channel_id and slack_summary_message_ts are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        points_request.slack_channel_id = slack_channel_id
        points_request.slack_thread_ts = request.data.get('slack_thread_ts') or points_request.slack_thread_ts
        points_request.slack_summary_message_ts = slack_summary_message_ts
        points_request.save(
            update_fields=[
                'slack_channel_id',
                'slack_thread_ts',
                'slack_summary_message_ts',
                'updated_at',
            ]
        )
        return Response(PointsRequestSerializer(points_request).data)

    @action(detail=False, methods=['get'], url_path='by-slack-message')
    def by_slack_message(self, request):
        slack_channel_id = (request.query_params.get('slack_channel_id') or '').strip()
        slack_message_ts = (request.query_params.get('slack_message_ts') or '').strip()

        if not slack_channel_id or not slack_message_ts:
            return Response(
                {'error': 'slack_channel_id and slack_message_ts are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        points_request = PointsRequest.objects.filter(
            slack_channel_id=slack_channel_id,
            slack_summary_message_ts=slack_message_ts,
        ).first()
        if not points_request:
            return Response({'error': 'Points request not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response(PointsRequestSerializer(points_request).data)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        admin_slack_id = (request.data.get('admin_slack_id') or '').strip()
        if not admin_slack_id:
            return Response({'error': 'admin_slack_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not is_points_admin(admin_slack_id):
            return Response({'error': 'Not a points admin'}, status=status.HTTP_403_FORBIDDEN)

        try:
            with transaction.atomic():
                points_request = PointsRequest.objects.select_for_update().get(pk=pk)
                if points_request.status != 'pending':
                    return Response(
                        {'error': f'Points request is already {points_request.status}'},
                        status=status.HTTP_409_CONFLICT,
                    )

                user = get_or_create_user_for_slack_id(points_request.target_slack_id)
                ledger, _ = PointsService.award(
                    user=user,
                    delta=points_request.points,
                    source='MANUAL',
                    description=points_request.reason,
                    created_by_slack_id=admin_slack_id,
                    idempotency_key=f"points_request:{points_request.id}:approve",
                    reference_type='POINTS_REQUEST',
                    reference_id=str(points_request.id),
                )

                points_request.status = 'approved'
                points_request.approved_by_slack_id = admin_slack_id
                points_request.approved_at = timezone.now()
                points_request.ledger_entry = ledger
                points_request.save(
                    update_fields=[
                        'status',
                        'approved_by_slack_id',
                        'approved_at',
                        'ledger_entry',
                        'updated_at',
                    ]
                )
        except PointsRequest.DoesNotExist:
            return Response({'error': 'Points request not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Failed to approve points request %s", pk)
            return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        balance = PointsService.get_balance(user)
        return Response(
            {
                **PointsRequestSerializer(points_request).data,
                'points_awarded': ledger.delta,
                'new_balance': balance['balance'],
            }
        )


class PointsPurchaseViewSet(viewsets.ViewSet):
    """Create pending Top-up Roo Points purchases for Roo."""
    permission_classes = [HasRooApiKey]

    def get_permissions(self):
        if self.action in ('retrieve', 'checkout'):
            return [AllowAny()]
        return [permission() for permission in self.permission_classes]

    @staticmethod
    def _response_data(purchase):
        return {
            'id': str(purchase.id),
            'status': purchase.status,
            'pack_id': purchase.pack_id,
            'points_amount': purchase.points_amount,
            'amount_cents': purchase.amount_cents,
            'currency': purchase.currency,
            'expires_at': purchase.expires_at.isoformat(),
            'paid_at': purchase.paid_at.isoformat() if purchase.paid_at else None,
            'created_at': purchase.created_at.isoformat(),
            'frontend_checkout_page_url': PointsPurchaseService.frontend_checkout_page_url(purchase),
        }

    def create(self, request):
        slack_user_id = (request.data.get('slack_user_id') or '').strip()
        pack_id = (request.data.get('pack_id') or '').strip()
        purchase_from = request.data.get('purchase_from') or {}

        if not slack_user_id or not pack_id:
            return Response(
                {'error': 'slack_user_id and pack_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(purchase_from, dict):
            return Response(
                {'error': 'purchase_from must be an object when provided'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            purchase = PointsPurchaseService.create_purchase(
                slack_user_id=slack_user_id,
                pack_id=pack_id,
                purchase_from=purchase_from,
            )
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            self._response_data(purchase),
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['post'], url_path='checkout-options')
    def checkout_options(self, request):
        """Create idempotent, Stripe-hosted checkout buttons for Roo in Slack."""
        slack_user_id = (request.data.get('slack_user_id') or '').strip()
        checkout_request_id = (request.data.get('checkout_request_id') or '').strip()
        purchase_from = request.data.get('purchase_from') or {}
        requested_pack_ids = request.data.get('pack_ids')

        if not slack_user_id or not checkout_request_id:
            return Response(
                {'error': 'slack_user_id and checkout_request_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(checkout_request_id) > 255:
            return Response(
                {'error': 'checkout_request_id must be 255 characters or fewer'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(purchase_from, dict):
            return Response(
                {'error': 'purchase_from must be an object when provided'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if requested_pack_ids is None:
            pack_ids = list(PointsPurchaseService.ROO_TOPUP_PACKS)
        elif isinstance(requested_pack_ids, list):
            pack_ids = list(dict.fromkeys(str(value).strip() for value in requested_pack_ids))
        else:
            return Response(
                {'error': 'pack_ids must be an array when provided'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not pack_ids:
            return Response(
                {'error': 'At least one pack_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            pack_configs = {
                pack_id: PointsPurchaseService.get_pack_config(pack_id)
                for pack_id in pack_ids
            }
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        origin = {
            **purchase_from,
            'source': 'slack',
            'surface': 'roo_topup_buttons',
            'checkout_request_id': checkout_request_id,
        }
        options = []
        errors = []
        for pack_id in pack_ids:
            try:
                purchase = PointsPurchaseService.create_purchase(
                    slack_user_id=slack_user_id,
                    pack_id=pack_id,
                    purchase_from=origin,
                    checkout_request_id=checkout_request_id,
                )
                checkout_result = PointsPurchaseService.create_checkout_session(
                    purchase=purchase,
                    collect_terms_in_checkout=True,
                )
            except PermissionDeniedError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
            except ValueError as exc:
                errors.append({'pack_id': pack_id, 'error': str(exc)})
                continue
            except RuntimeError as exc:
                logger.warning(
                    "Failed to create direct Stripe Checkout for PointsPurchase pack %s: %s",
                    pack_id,
                    exc,
                )
                errors.append({'pack_id': pack_id, 'error': str(exc)})
                continue

            pack = pack_configs[pack_id]
            options.append(
                {
                    **self._response_data(checkout_result['purchase']),
                    'label': pack['label'],
                    'checkout_session_url': checkout_result['checkout_session_url'],
                }
            )

        if not options:
            return Response(
                {
                    'error': 'Stripe Checkout is temporarily unavailable for Roo Points top-ups',
                    'errors': errors,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                'checkout_request_id': checkout_request_id,
                'options': options,
                'errors': errors,
            },
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None):
        try:
            purchase = PointsPurchase.objects.get(pk=pk)
        except (PointsPurchase.DoesNotExist, ValueError, ValidationError):
            return Response({'error': 'Points purchase not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response(self._response_data(purchase))

    @action(detail=True, methods=['post'], url_path='checkout')
    def checkout(self, request, pk=None):
        try:
            purchase = PointsPurchase.objects.get(pk=pk)
        except (PointsPurchase.DoesNotExist, ValueError, ValidationError):
            return Response({'error': 'Points purchase not found'}, status=status.HTTP_404_NOT_FOUND)

        terms_version_accepted = request.data.get('terms_version_accepted')
        privacy_version_accepted = request.data.get('privacy_version_accepted')
        try:
            checkout_result = PointsPurchaseService.create_checkout_session(
                purchase=purchase,
                terms_version_accepted=terms_version_accepted,
                privacy_version_accepted=privacy_version_accepted,
            )
        except ValueError as exc:
            message = str(exc)
            response_status = status.HTTP_409_CONFLICT if 'cannot start Checkout' in message or 'expired' in message else status.HTTP_400_BAD_REQUEST
            return Response({'error': message}, status=response_status)
        except RuntimeError as exc:
            logger.warning("Failed to create Stripe Checkout Session for PointsPurchase %s: %s", pk, exc)
            return Response({'error': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        purchase = checkout_result['purchase']
        return Response(
            {
                **self._response_data(purchase),
                'stripe_checkout_session_id': checkout_result['checkout_session_id'],
                'checkout_session_url': checkout_result['checkout_session_url'],
            }
        )


class PointsPacksView(APIView):
    """Public list of available Top-up Roo Points packs.

    Lets the frontend render the upgrade page from a single source of truth
    instead of duplicating prices, keeping it in sync with ``ROO_TOPUP_PACKS``.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        packs = [
            {
                'pack_id': pack_id,
                'points': cfg['points'],
                'amount_cents': cfg['amount_cents'],
                'currency': cfg['currency'],
                'label': cfg['label'],
            }
            for pack_id, cfg in PointsPurchaseService.ROO_TOPUP_PACKS.items()
        ]
        return Response({'packs': packs})


class CurrentUserPurchaseView(APIView):
    """Create a pending Top-up Roo Points purchase for the authenticated user.

    This is the dashboard/web entry point (e.g. /founder-tools/upgrade). It
    mirrors ``CurrentUserBalanceView`` auth: any logged-in user can buy, no Roo
    API key or linked Slack account required. The response carries the
    ``frontend_checkout_page_url`` so the caller can hand off to the existing
    /roo/topup/{id} review + Stripe Checkout flow.
    """
    permission_classes = [IsAuthenticated]
    # Accept the Desktop/Community Chat account session as well as the existing
    # website JWT.  In both cases identity comes only from request.user.
    authentication_classes = (
        CommunityChatAccountAuthentication,
        CustomJWTAuthentication,
    )

    def post(self, request):
        pack_id = (request.data.get('pack_id') or '').strip()
        if not pack_id:
            return Response(
                {'error': 'pack_id is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        purchase_from = request.data.get('purchase_from') or {}
        if not isinstance(purchase_from, dict):
            return Response(
                {'error': 'purchase_from must be an object when provided'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        origin = dict(purchase_from)
        origin.setdefault('source', 'web')

        try:
            purchase = PointsPurchaseService.create_purchase_for_user(
                user=request.user,
                pack_id=pack_id,
                purchase_from=origin,
            )
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            PointsPurchaseViewSet._response_data(purchase),
            status=status.HTTP_201_CREATED,
        )


class ManualAwardView(APIView):
    """
    Admin: Manual points award/deduct.
    Secured by X-API-Key for Roo agent usage.
    """
    permission_classes = [HasRooApiKey]

    def post(self, request):
        admin_slack_id = request.data.get('slack_user_id') or request.data.get('admin_slack_id')
        target_slack_id = request.data.get('target_slack_id')
        
        points = request.data.get('points')
        reason = request.data.get('reason', 'Manual adjustment')

        if admin_slack_id:
            admin_slack_id = admin_slack_id.strip()
        if target_slack_id:
            target_slack_id = target_slack_id.strip()

        # 1. Validation & Admin Check
        if not admin_slack_id or not target_slack_id or points is None:
            return Response(
                {'error': 'slack_user_id (or admin_slack_id), target_slack_id and points are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            require_linked_points_admin(admin_slack_id, action_label='award points manually')
        except PermissionDeniedError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            points = int(points)
        except (ValueError, TypeError):
            return Response({'error': 'Points must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        # New Rule: Admins cannot award points to themselves
        if points > 0 and admin_slack_id == target_slack_id:
            return Response(
                {'error': 'Admins cannot award points to themselves'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. User Lookup & Auto-Creation
        user = get_or_create_user_for_slack_id(target_slack_id)

        # 3. Transaction Execution
        idempotency_key = f"manual:{admin_slack_id}:{target_slack_id}:{timezone.now().isoformat()}"

        try:
            if points > 0:
                ledger, created = PointsService.award(
                    user=user,
                    delta=points,
                    source='MANUAL',
                    description=reason,
                    created_by_slack_id=admin_slack_id,
                    idempotency_key=idempotency_key,
                )
            elif points < 0:
                ledger, created = PointsService.spend(
                    user=user,
                    delta=abs(points),
                    source='MANUAL',
                    description=reason,
                    created_by_slack_id=admin_slack_id,
                    idempotency_key=idempotency_key,
                )
            else:
                return Response({'error': 'Points cannot be zero'}, status=status.HTTP_400_BAD_REQUEST)

            balance = PointsService.get_balance(user)
            
            # 4. Response
            return Response({
                "success": True,
                "new_balance": balance['balance'],
                "ledger_id": ledger.id,
                "message": f"{'Awarded' if points > 0 else 'Deducted'} {abs(points)} points {'to' if points > 0 else 'from'} {user.full_name or user.email}"
            })


        except InsufficientBalanceError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
             return Response({'error': f"Internal error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SystemAwardView(APIView):
    """
    Roo-internal positive point award path for non-human system automations.
    """
    permission_classes = [HasRooApiKey]

    def post(self, request):
        created_by_slack_id = clean_slack_id(
            request.data.get('created_by_slack_id') or request.data.get('admin_slack_id') or 'SYSTEM'
        )
        target_slack_id = clean_slack_id(request.data.get('target_slack_id') or '')
        points = request.data.get('points')
        reason = str(request.data.get('reason') or 'System award')
        idempotency_key = str(request.data.get('idempotency_key') or '').strip()

        if not target_slack_id or points is None:
            return Response(
                {'error': 'target_slack_id and points are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            points = int(points)
        except (ValueError, TypeError):
            return Response({'error': 'Points must be an integer'}, status=status.HTTP_400_BAD_REQUEST)

        if points <= 0:
            return Response({'error': 'System awards must be positive'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = get_or_create_user_for_slack_id(target_slack_id)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception(
                "system_award_user_resolution_failed target_slack_id=%s",
                target_slack_id,
            )
            return Response(
                {'error': 'Internal error resolving target Slack user'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if not idempotency_key:
            idempotency_key = f"system:{created_by_slack_id}:{target_slack_id}:{timezone.now().isoformat()}"

        try:
            ledger, _ = PointsService.award(
                user=user,
                delta=points,
                source='EVENT',
                description=reason,
                created_by_slack_id=created_by_slack_id,
                idempotency_key=idempotency_key,
            )
            balance = PointsService.get_balance(user)
            return Response({
                'success': True,
                'points_awarded': ledger.delta,
                'new_balance': balance['balance'],
                'ledger_id': ledger.id,
            })
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception(
                "system_award_failed created_by_slack_id=%s target_slack_id=%s idempotency_key=%s",
                created_by_slack_id,
                target_slack_id,
                idempotency_key,
            )
            return Response({'error': f"Internal error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================
# Activity Tracking Views
# ============================================================

class ChannelActivityView(APIView):
    """
    Track first posts in channels.
    """
    # Override global authentication to allow API key access
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request, slack_user_id=None, channel_id=None):
        """
        Check if posted.
        Path: GET /api/v1/activity/first-post/{slack_user_id}/{channel_id}/
        """
        if not slack_user_id or not channel_id:
            return Response({"error": "slack_user_id and channel_id are required"}, status=status.HTTP_400_BAD_REQUEST)
        
        has_posted = ChannelFirstPost.objects.filter(
            slack_user_id=slack_user_id, 
            channel_id=channel_id
        ).exists()
        
        return Response({"has_posted": has_posted})

    def post(self, request):
        """
        Record first post.
        Path: POST /api/v1/activity/first-post/
        Body: {"slack_user_id": "...", "channel_id": "..."}
        """
        slack_user_id = request.data.get('slack_user_id')
        channel_id = request.data.get('channel_id')

        if not slack_user_id or not channel_id:
            return Response({"error": "slack_user_id and channel_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        if ChannelFirstPost.objects.filter(slack_user_id=slack_user_id, channel_id=channel_id).exists():
            return Response(
                {"error": "Activity already recorded", "has_posted": True}, 
                status=status.HTTP_409_CONFLICT
            )
        
        # Create record
        ChannelFirstPost.objects.create(slack_user_id=slack_user_id, channel_id=channel_id)

        # Award points if user is linked
        # Note: PointsService is imported at top of file, so we can use it directly
        # However, to avoid circular imports if any, we use the local import pattern from the original file if needed.
        # But PointsService is already imported at module level in views.py, so it should be fine.
        user = PointsService.get_user_by_slack_id(slack_user_id)
        
        points_awarded = False
        if user:
            try:
                idempotency_key = f"first_post_award:{slack_user_id}:{channel_id}"
                PointsService.award(
                    user=user,
                    delta=1,
                    source='COMMUNITY',
                    description=f"First post in channel {channel_id}",
                    created_by_slack_id="SYSTEM",
                    idempotency_key=idempotency_key
                )
                points_awarded = True
            except Exception as e:
                logger.error(f"Failed to award points for first post: {e}")

        return Response({
            "status": "recorded", 
            "points_awarded": points_awarded
        }, status=status.HTTP_201_CREATED)


class FirstChannelPostAwardView(APIView):
    """Idempotently award the intro bonus for a first top-level channel post."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request):
        slack_user_id = (request.data.get('slack_user_id') or '').strip()
        channel_id = (request.data.get('channel_id') or '').strip()

        if not slack_user_id or not channel_id:
            return Response(
                {'error': 'slack_user_id and channel_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_attempts = 3 if connection.vendor == 'sqlite' else 1
        for attempt in range(max_attempts):
            try:
                awarded, new_balance = award_first_channel_post_bonus(slack_user_id, channel_id)
                break
            except ValueError as exc:
                return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            except OperationalError as exc:
                if not is_retryable_sqlite_lock(exc) or attempt == max_attempts - 1:
                    logger.exception(
                        "Failed to award first channel post bonus for %s in %s",
                        slack_user_id,
                        channel_id,
                    )
                    return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                time.sleep(0.05 * (attempt + 1))
            except Exception as exc:
                logger.exception(
                    "Failed to award first channel post bonus for %s in %s",
                    slack_user_id,
                    channel_id,
                )
                return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response_data = {'awarded': awarded}
        if awarded and new_balance is not None:
            response_data['new_balance'] = new_balance
            response_data['points_awarded'] = FIRST_CHANNEL_POST_POINTS
        return Response(response_data, status=status.HTTP_200_OK)


# ============================================================
# Quests Views
# ============================================================

class QuestProgressView(APIView):
    """Get quest progress for a user."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request, slack_user_id, quest_id):
        """GET /api/v1/quests/{slack_user_id}/{quest_id}/"""
        try:
            progress = QuestProgress.objects.get(
                slack_user_id=slack_user_id, 
                quest_id=quest_id
            )
            return Response(QuestProgressSerializer(progress).data)
        except QuestProgress.DoesNotExist:
            return Response(
                {"detail": "No progress found for this quest"},
                status=status.HTTP_404_NOT_FOUND
            )


class UserQuestProgressView(APIView):
    """Get all quest progress for a user."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request, slack_user_id):
        """GET /api/v1/quests/{slack_user_id}/"""
        queryset = QuestProgress.objects.filter(slack_user_id=slack_user_id)
        
        # Optional filter by completion status
        completed_param = request.query_params.get('completed')
        if completed_param is not None:
            completed = completed_param.lower() == 'true'
            queryset = queryset.filter(completed=completed)
        
        quests_data = QuestProgressSerializer(queryset, many=True).data
        return Response({
            "slack_user_id": slack_user_id,
            "quests": quests_data
        })


class QuestIncrementView(APIView):
    """Increment quest progress."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request):
        """POST /api/v1/quests/progress/"""
        serializer = QuestProgressInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        slack_user_id = serializer.validated_data['slack_user_id']
        quest_id = serializer.validated_data['quest_id']
        increment_by = serializer.validated_data.get('increment_by', 1)
        
        with transaction.atomic():
            progress, created = QuestProgress.objects.select_for_update().get_or_create(
                slack_user_id=slack_user_id,
                quest_id=quest_id,
                defaults={'current_count': 0}
            )
            
            # Check if already completed
            if progress.completed:
                return Response({
                    "detail": "Quest already completed",
                    "completed_at": progress.completed_at.isoformat() if progress.completed_at else None
                }, status=status.HTTP_409_CONFLICT)
            
            # Increment count
            progress.current_count += increment_by
            progress.save()
        
        return Response({
            "slack_user_id": slack_user_id,
            "quest_id": quest_id,
            "current_count": progress.current_count,
            "completed": progress.completed,
            "message": "Progress incremented"
        })


class QuestCompleteView(APIView):
    """Mark a quest as completed."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def post(self, request):
        """POST /api/v1/quests/complete/"""
        serializer = QuestCompleteInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        slack_user_id = serializer.validated_data['slack_user_id']
        quest_id = serializer.validated_data['quest_id']
        
        with transaction.atomic():
            progress, created = QuestProgress.objects.select_for_update().get_or_create(
                slack_user_id=slack_user_id,
                quest_id=quest_id,
                defaults={'current_count': 0}
            )
            
            # Check if already completed
            if progress.completed:
                return Response({
                    "detail": "Quest already completed",
                    "completed_at": progress.completed_at.isoformat() if progress.completed_at else None
                }, status=status.HTTP_409_CONFLICT)
            
            # Mark as completed
            progress.completed = True
            progress.completed_at = timezone.now()
            progress.save()
        
        return Response({
            "slack_user_id": slack_user_id,
            "quest_id": quest_id,
            "current_count": progress.current_count,
            "completed": True,
            "completed_at": progress.completed_at.isoformat(),
            "message": "Quest marked as completed"
        })


class QuestCompletionStatusView(APIView):
    """Quick check if a quest is completed."""
    authentication_classes = []
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get(self, request, slack_user_id, quest_id):
        """GET /api/v1/quests/{slack_user_id}/{quest_id}/completed/"""
        try:
            progress = QuestProgress.objects.get(
                slack_user_id=slack_user_id,
                quest_id=quest_id
            )
            if progress.completed:
                return Response({
                    "completed": True,
                    "completed_at": progress.completed_at.isoformat() if progress.completed_at else None
                })
            else:
                return Response({
                    "completed": False,
                    "current_count": progress.current_count
                })
        except QuestProgress.DoesNotExist:
            return Response({
                "completed": False,
                "current_count": 0
            })
