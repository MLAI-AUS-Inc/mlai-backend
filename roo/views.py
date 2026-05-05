import time

from rest_framework import viewsets, status, mixins
from rest_framework.permissions import AllowAny
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

from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskAssignment, TaskSubmission, CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, TaskTemplate, PointsRequest
)

from .services import (
    PointsService, PointsPurchaseService, CoworkingService,
    TaskService, RewardsService,
)
from .permissions import (
    is_points_admin,
    is_points_super_admin,
    InsufficientBalanceError,
    PermissionDeniedError,
)
from core.models import User
from core.permissions import HasAPIKey, HasRooApiKey
from integrations.services import SlackService

# Additional imports for Activity & Quests
import logging
from django.db import OperationalError, connection, transaction
from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskSubmission, CoworkingBooking, CoworkingDayCapacity,
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


def get_or_create_user_for_slack_id(slack_user_id: str) -> User:
    """Resolve a Slack user to a local user, creating a placeholder when needed."""
    if not slack_user_id:
        raise ValueError('target_slack_id is required')

    slack_user_id = slack_user_id.strip()
    user = PointsService.get_user_by_slack_id(slack_user_id)
    if user:
        return user

    profile = SlackService.get_user_profile(slack_user_id)
    if profile:
        email = profile.get('email') or f"{slack_user_id}@slack.placeholder.com"
        real_name = profile.get('real_name', 'Unknown')

        existing_user = User.objects.filter(email=email).first()
        if existing_user:
            if not existing_user.slack_id:
                existing_user.slack_id = slack_user_id
                existing_user.save(update_fields=['slack_id'])
                return existing_user
            if existing_user.slack_id == slack_user_id:
                return existing_user
            email = f"{slack_user_id}@slack.placeholder.com"

        return User.objects.create(
            email=email,
            slack_id=slack_user_id,
            first_name=real_name.split()[0],
            last_name=' '.join(real_name.split()[1:]) if ' ' in real_name else '',
            avatar_url=profile.get('image_url'),
            role='participant',
        )

    return User.objects.create(
        email=f"{slack_user_id}@slack.placeholder.com",
        slack_id=slack_user_id,
        first_name="Unknown Slack User",
        role='participant',
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
            delta=2,
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
            account = PointsService.get_or_create_account(user)
            
            data = {
                'slack_user_id': slack_user_id,
                'email': user.email,
                'balance': balance_data['balance'],
                'earned_balance': balance_data['earned_balance'],
                'purchased_topup_balance': balance_data['purchased_topup_balance'],
                'lifetime_earned': balance_data['lifetime_earned'],
                'lifetime_purchased_topup': balance_data['lifetime_purchased_topup'],
                'lifetime_spent': balance_data['lifetime_spent'],
                'expired_or_reversed_points': balance_data['expired_or_reversed_points'],
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

    @action(detail=False, methods=['get'])
    def availability(self, request):
        """Check availability for dates."""
        date_param = request.query_params.get('date')
        days_ahead = int(request.query_params.get('days', 7))
        
        start_date = date.today()
        if date_param:
            start_date = date.fromisoformat(date_param)
        
        results = []
        cost_points = CoworkingService.get_coworking_cost()
        
        for i in range(days_ahead):
            check_date = start_date + timedelta(days=i)
            available, capacity = CoworkingService.check_availability(check_date)
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
        if not is_points_admin(slack_user_id):
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

        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response(
                {'error': 'Please link your Slack account first'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            booking, created = CoworkingService.book(
                user=user,
                booking_date=booking_date,
                created_by_slack_id=slack_user_id,
                slack_channel_id=slack_channel_id,
            )
            response_data = CoworkingBookingSerializer(booking).data
            if not created:
                response_data["already_booked"] = True
                response_data["idempotent"] = True
                return Response(response_data, status=status.HTTP_200_OK)
            return Response(response_data, status=status.HTTP_201_CREATED)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientBalanceError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

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
        if not slack_user_id:
            return Response({'error': 'slack_user_id required'}, status=status.HTTP_400_BAD_REQUEST)

        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        bookings = CoworkingBooking.objects.filter(user=user).order_by('-date')[:20]
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
            {
                'id': str(purchase.id),
                'status': purchase.status,
                'slack_user_id': purchase.slack_user_id,
                'pack_id': purchase.pack_id,
                'points_amount': purchase.points_amount,
                'amount_cents': purchase.amount_cents,
                'currency': purchase.currency,
                'expires_at': purchase.expires_at.isoformat(),
                'frontend_checkout_page_url': PointsPurchaseService.frontend_checkout_page_url(purchase),
            },
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

        # Bypass admin check if authenticated via API Key (Roo)
        # The permission class has already passed at this point.
        # But we still run the check for non-API-Key users (if any could get here)
        # Since usage is restricted to HasRooApiKey, we technically know it's allowed.
        # But if we want to support human users via session in future, we keep the check.
        # For now, if HasRooApiKey is the ONLY permission, then everyone here is Roo.
        # But let's be explicitly safe and check if it's NOT an admin AND NOT authorized via key (impossible here)
        
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
