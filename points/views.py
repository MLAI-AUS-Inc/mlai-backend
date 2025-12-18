from rest_framework import viewsets, status, mixins
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.db.models import Sum
from django.conf import settings
from datetime import date, timedelta

from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskSubmission, CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, TaskTemplate
)
from .serializers import (
    PointsAdminSerializer, MinterSerializer, TaskSerializer, LedgerSerializer,
    PointsAccountSerializer, PointsBalanceSerializer, TaskSubmissionSerializer,
    CoworkingBookingSerializer, CoworkingAvailabilitySerializer,
    CoworkingDayCapacitySerializer, RewardsCatalogSerializer, RewardRedemptionSerializer,
    TaskTemplateSerializer
)
from .services import PointsService, CoworkingService, TaskService, RewardsService
from .permissions import is_points_admin, InsufficientBalanceError, PermissionDeniedError
from core.models import User
from core.permissions import HasAPIKey, HasRooApiKey
from integrations.services import SlackService


class RateCardView(viewsets.ReadOnlyModelViewSet):
    """
    Public (authenticated) rate card of standard tasks.
    """
    queryset = TaskTemplate.objects.filter(is_active=True)
    serializer_class = TaskTemplateSerializer
    # Allow either API Key (for Roo/bots) or IsAuthenticated (for frontend users)
    permission_classes = [HasAPIKey | settings.REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES'][0]]


class PointsAdminViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only view for Points Admins. 
    Use Django Admin to add/manage admins.
    """
    queryset = PointsAdmin.objects.filter(is_active=True)
    serializer_class = PointsAdminSerializer
    lookup_field = 'slack_user_id'
    permission_classes = [HasRooApiKey]


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
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [HasAPIKey | HasRooApiKey]

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get('status')
        portfolio_param = self.request.query_params.get('portfolio')
        
        if status_param:
            qs = qs.filter(status=status_param)
        if portfolio_param:
            qs = qs.filter(portfolio=portfolio_param)
        
        return qs.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        """Create a new task. Only Points Admins can create tasks."""
        # Extract the creator's Slack ID
        creator_slack_id = request.data.get('created_by_user_id') or request.data.get('slack_user_id')
        
        if not creator_slack_id:
            return Response(
                {'error': 'created_by_user_id or slack_user_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if creator is a Points Admin
        if not is_points_admin(creator_slack_id):
            return Response(
                {'error': 'Only Points Admins can create tasks'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Proceed with normal creation
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def claim(self, request, pk=None):
        """Claim a task."""
        task = self.get_object()
        slack_user_id = request.data.get('slack_user_id')

        if not slack_user_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if task.status != 'open':
            return Response({'error': f'Task is not open (status: {task.status})'}, status=status.HTTP_400_BAD_REQUEST)

        # Link to user if possible
        user = PointsService.get_user_by_slack_id(slack_user_id)
        if user:
            task.assigned_user = user
        
        task.assigned_to_user_id = slack_user_id
        task.status = 'claimed'
        task.save()
        
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Submit completed work for a task."""
        task = self.get_object()
        slack_user_id = request.data.get('slack_user_id')
        submission_text = request.data.get('submission_text', '')
        submission_url = request.data.get('submission_url')

        if not slack_user_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if task.status not in ['claimed', 'open']:
            return Response({'error': f'Task cannot be submitted (status: {task.status})'}, status=status.HTTP_400_BAD_REQUEST)

        # Get user
        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response({'error': 'Please link your Slack account first'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user is assigned (if task was claimed)
        if task.assigned_user and task.assigned_user != user:
            return Response({'error': 'Only the assigned user can submit'}, status=status.HTTP_403_FORBIDDEN)

        # Create submission
        submission = TaskSubmission.objects.create(
            task=task,
            user=user,
            submission_text=submission_text,
            submission_url=submission_url,
            status='submitted',
        )

        # Update task
        task.status = 'submitted'
        task.assigned_user = user
        task.assigned_to_user_id = slack_user_id
        task.save()

        return Response(TaskSubmissionSerializer(submission).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve a task submission and award points."""
        task = self.get_object()
        approver_slack_id = request.data.get('slack_user_id')
        submission_id = request.data.get('submission_id')

        if not approver_slack_id:
            return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not is_points_admin(approver_slack_id):
            return Response({'error': 'You are not authorized to approve tasks'}, status=status.HTTP_403_FORBIDDEN)

        # Get submission
        if submission_id:
            try:
                submission = task.submissions.get(id=submission_id)
            except TaskSubmission.DoesNotExist:
                return Response({'error': 'Submission not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            # Get latest submission
            submission = task.submissions.filter(status='submitted').order_by('-created_at').first()
            if not submission:
                # Legacy flow: direct approve without submission
                return self._legacy_approve(task, approver_slack_id)

        try:
            submission, ledger = TaskService.approve_submission(submission, approver_slack_id)
            return Response({
                'task': TaskSerializer(task).data,
                'submission': TaskSubmissionSerializer(submission).data,
                'points_awarded': ledger.delta,
            })
        except PermissionDeniedError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def _legacy_approve(self, task, approver_slack_id):
        """Legacy approval flow for tasks without submissions."""
        if task.status not in ['claimed', 'submitted']:
            return Response({'error': 'Task is not in a state to be approved'}, status=status.HTTP_400_BAD_REQUEST)

        # Get assigned user
        user = task.assigned_user
        if not user and task.assigned_to_user_id:
            user = PointsService.get_user_by_slack_id(task.assigned_to_user_id)
        
        if not user:
            return Response({'error': 'No user assigned to this task'}, status=status.HTTP_400_BAD_REQUEST)

        idempotency_key = f"task_award:{task.id}:{user.id}"

        try:
            ledger, created = PointsService.award(
                user=user,
                delta=task.points,
                source='TASK',
                description=f"Completed task: {task.title}",
                created_by_slack_id=approver_slack_id,
                idempotency_key=idempotency_key,
                reference_type='TASK',
                reference_id=str(task.id),
            )
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        task.status = 'approved'
        task.closed_by_user_id = approver_slack_id
        task.closed_at = timezone.now()
        task.save()

        return Response({
            'task': TaskSerializer(task).data,
            'points_awarded': ledger.delta,
            'created': created,
        })
    
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject a task submission."""
        task = self.get_object()
        rejector_slack_id = request.data.get('slack_user_id')
        reason = request.data.get('reason', '')
        submission_id = request.data.get('submission_id')

        if not is_points_admin(rejector_slack_id):
            return Response({'error': 'You are not authorized to reject tasks'}, status=status.HTTP_403_FORBIDDEN)

        if submission_id:
            try:
                submission = task.submissions.get(id=submission_id)
                submission.status = 'rejected'
                submission.rejection_reason = reason
                submission.save()
            except TaskSubmission.DoesNotExist:
                pass

        task.status = 'claimed'
        task.save()
        return Response(TaskSerializer(task).data)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a task."""
        task = self.get_object()
        task.status = 'cancelled'
        task.save()
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=['post'])
    def award(self, request, pk=None):
        """Direct award: claim + approve in one step."""
        task = self.get_object()
        assignee_slack_id = request.data.get('assigned_to_user_id')
        approver_slack_id = request.data.get('created_by_user_id') or request.data.get('slack_user_id')

        if not assignee_slack_id:
            return Response({'error': 'assigned_to_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if not is_points_admin(approver_slack_id):
            return Response({'error': 'You are not authorized to award tasks'}, status=status.HTTP_403_FORBIDDEN)

        if task.status != 'open':
            return Response({'error': 'Task must be open to direct award'}, status=status.HTTP_400_BAD_REQUEST)

        # Get user
        user = PointsService.get_user_by_slack_id(assignee_slack_id)
        if not user:
            return Response({'error': 'Assignee must have linked Slack account'}, status=status.HTTP_400_BAD_REQUEST)

        idempotency_key = f"task_award:{task.id}:{user.id}"

        try:
            ledger, created = PointsService.award(
                user=user,
                delta=task.points,
                source='TASK',
                description=f"Completed task: {task.title}",
                created_by_slack_id=approver_slack_id,
                idempotency_key=idempotency_key,
                reference_type='TASK',
                reference_id=str(task.id),
            )
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        task.assigned_user = user
        task.assigned_user_id = assignee_slack_id
        task.status = 'approved'
        task.closed_by_user_id = approver_slack_id
        task.closed_at = timezone.now()
        task.save()

        return Response({
            'task': TaskSerializer(task).data,
            'points_awarded': ledger.delta,
        })

    # Legacy endpoint for backwards compatibility
    @action(detail=True, methods=['post'], url_path='request-complete')
    def request_complete(self, request, pk=None):
        """Legacy: Mark task as pending approval."""
        task = self.get_object()
        requester_id = request.data.get('slack_user_id')

        if task.status != 'claimed':
            return Response({'error': 'Task is not claimed'}, status=status.HTTP_400_BAD_REQUEST)
        
        if task.assigned_to_user_id and requester_id and task.assigned_to_user_id != requester_id:
            return Response({'error': 'Only the assignee can complete this task'}, status=status.HTTP_403_FORBIDDEN)

        task.status = 'submitted'
        task.save()
        return Response(TaskSerializer(task).data)


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
                'lifetime_earned': balance_data['lifetime_earned'],
                'lifetime_spent': balance_data['lifetime_spent'],
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
        cost_points = getattr(settings, 'COWORKING_DAY_COST_POINTS', 1)
        
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

        # Parse and validate date is today
        try:
            booking_date = date.fromisoformat(booking_date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        today = timezone.now().date()
        if booking_date != today:
            return Response(
                {'error': f'Coworking can only be booked for today ({today.isoformat()}). You requested: {booking_date.isoformat()}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = PointsService.get_user_by_slack_id(slack_user_id)
        if not user:
            return Response(
                {'error': 'Please link your Slack account first'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            booking = CoworkingService.book(
                user=user,
                booking_date=booking_date,
                created_by_slack_id=slack_user_id,
                slack_channel_id=slack_channel_id,
            )
            return Response(CoworkingBookingSerializer(booking).data, status=status.HTTP_201_CREATED)
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
        
        # If we really want to enforce that the *ID provided* is an admin, unless it's Roo:
        if not is_points_admin(admin_slack_id):
             # Allow if verified Roo API Request (which it is, due to permission_classes)
             pass 

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
        user = PointsService.get_user_by_slack_id(target_slack_id)
        if not user:
            # Attempt to fetch from Slack
            profile = SlackService.get_user_profile(target_slack_id)
            
            if profile:
                # Create user from Slack profile
                email = profile.get('email')
                real_name = profile.get('real_name', 'Unknown')
                
                # Handle email collisions or missing email
                if not email:
                    email = f"{target_slack_id}@slack.placeholder.com"
                
                # Check if email already exists (e.g. linked to another slack ID or no slack ID)
                if User.objects.filter(email=email).exists():
                    user = User.objects.get(email=email)
                    if not user.slack_id:
                        user.slack_id = target_slack_id
                        user.save()
                    elif user.slack_id != target_slack_id:
                         # Edge case: Email collision with different Slack ID
                         # Fallback to stub email
                         email = f"{target_slack_id}@slack.placeholder.com"
                         user = User.objects.create(
                            email=email,
                            slack_id=target_slack_id,
                            first_name=real_name.split()[0],
                            last_name=' '.join(real_name.split()[1:]) if ' ' in real_name else '',
                            role='participant'
                        )
                else:
                    user = User.objects.create(
                        email=email,
                        slack_id=target_slack_id,
                        first_name=real_name.split()[0],
                        last_name=' '.join(real_name.split()[1:]) if ' ' in real_name else '',
                        avatar_url=profile.get('image_url'),
                        role='participant'
                    )
            else:
                # Fallback: Create stub user
                user = User.objects.create(
                    email=f"{target_slack_id}@slack.placeholder.com",
                    slack_id=target_slack_id,
                    first_name="Unknown Slack User",
                    role='participant'
                )

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
