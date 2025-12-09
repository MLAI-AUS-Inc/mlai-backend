from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.utils import timezone
from django.db.models import Sum
from .models import Minter, Task, Ledger
from .serializers import MinterSerializer, TaskSerializer, LedgerSerializer, PointsBalanceSerializer

class MinterViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only view for Minters. 
    Use Django Admin to add/manage minters.
    """
    queryset = Minter.objects.all()
    serializer_class = MinterSerializer
    lookup_field = 'slack_user_id'
    permission_classes = [AllowAny] # Bot interacts without user auth token usually, or use API key. 
    # For now AllowAny to ease bot integration, but ideally use custom auth or IP whitelist.

class TaskViewSet(viewsets.ModelViewSet):
    """
    Main ViewSet for Tasks.
    """
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [AllowAny] # Open for Bot interactivity

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get('status')
        portfolio_param = self.request.query_params.get('portfolio')
        
        if status_param:
            qs = qs.filter(status=status_param)
        if portfolio_param:
            qs = qs.filter(portfolio=portfolio_param)
        
        return qs

    @action(detail=True, methods=['post'])
    def claim(self, request, pk=None):
        task = self.get_object()
        volunteer_id = request.data.get('slack_user_id')

        if not volunteer_id:
             return Response({'error': 'slack_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if task.status != 'open':
            return Response({'error': f'Task is not open (status: {task.status})'}, status=status.HTTP_400_BAD_REQUEST)

        task.status = 'claimed'
        task.assigned_to_user_id = volunteer_id
        task.save()
        
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=['post'], url_path='request-complete')
    def request_complete(self, request, pk=None):
        task = self.get_object()
        requester_id = request.data.get('slack_user_id') # Who is saying it's done?

        if task.status != 'claimed':
            return Response({'error': 'Task is not claimed'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Optional: check if requester is the assignee
        if task.assigned_to_user_id and requester_id and task.assigned_to_user_id != requester_id:
             return Response({'error': 'Only the assignee can complete this task'}, status=status.HTTP_403_FORBIDDEN)

        task.status = 'pending_approval'
        task.save()
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        task = self.get_object()
        approver_id = request.data.get('slack_user_id')

        # Check permissions (is minter?) - skipped for now, relying on bot to check or add check here
        
        if task.status not in ['pending_approval', 'claimed']: # Allow approving claimed tasks directly too
            return Response({'error': 'Task is not in a state to be approved'}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Update Task
        task.status = 'closed'
        task.closed_by_user_id = approver_id
        task.closed_at = timezone.now()
        task.save()

        # 2. Create Ledger Entry
        Ledger.objects.create(
            slack_user_id=task.assigned_to_user_id,
            task=task,
            points_delta=task.points,
            reason=task.title,
            created_by_user_id=approver_id
        )

        return Response(TaskSerializer(task).data)
    
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        task = self.get_object()
        # Return to claimed
        task.status = 'claimed'
        task.save()
        return Response(TaskSerializer(task).data)
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        task = self.get_object()
        task.status = 'cancelled'
        task.save()
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=['post'])
    def award(self, request, pk=None):
        """
        Directly award a task to a user (Claim + Approve in one go).
        """
        task = self.get_object()
        assignee_id = request.data.get('assigned_to_user_id')
        approver_id = request.data.get('created_by_user_id')

        if not assignee_id:
            return Response({'error': 'assigned_to_user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        if task.status != 'open':
             return Response({'error': 'Task must be open to direct award'}, status=status.HTTP_400_BAD_REQUEST)

        # Update Task
        task.assigned_to_user_id = assignee_id
        task.status = 'closed'
        task.closed_by_user_id = approver_id
        task.closed_at = timezone.now()
        task.save()

        # Ledger
        Ledger.objects.create(
            slack_user_id=assignee_id,
            task=task,
            points_delta=task.points,
            reason=task.title,
            created_by_user_id=approver_id
        )

        return Response(TaskSerializer(task).data)

class UserBalanceViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]

    def retrieve(self, request, pk=None):
        """
        pk is the slack_user_id
        """
        slack_user_id = pk
        
        # Aggregate all ledger entries
        total = Ledger.objects.filter(slack_user_id=slack_user_id).aggregate(Sum('points_delta'))['points_delta__sum'] or 0
        
        # Annual (optional, just example using current year)
        current_year = timezone.now().year
        annual = Ledger.objects.filter(
            slack_user_id=slack_user_id, 
            created_at__year=current_year
        ).aggregate(Sum('points_delta'))['points_delta__sum'] or 0

        data = {
            'slack_user_id': slack_user_id,
            'annual_balance': annual,
            'lifetime_balance': total
        }
        return Response(data)
