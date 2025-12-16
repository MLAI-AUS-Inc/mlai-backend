from rest_framework import serializers
from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskSubmission, CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption
)


class PointsAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = PointsAdmin
        fields = '__all__'


# Backwards compatibility
MinterSerializer = PointsAdminSerializer


class PointsAccountSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    slack_id = serializers.CharField(source='user.slack_id', read_only=True)
    
    class Meta:
        model = PointsAccount
        fields = ['user_id', 'email', 'slack_id', 'balance', 'lifetime_earned', 'lifetime_spent', 'updated_at']


class LedgerSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    
    class Meta:
        model = Ledger
        fields = [
            'id', 'user', 'user_email', 'delta', 'kind', 'source',
            'reference_type', 'reference_id', 'description',
            'created_by_slack_id', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TaskSerializer(serializers.ModelSerializer):
    assigned_to_email = serializers.EmailField(source='assigned_to_user.email', read_only=True, allow_null=True)
    
    class Meta:
        model = Task
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'closed_at')


class TaskSubmissionSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    task_title = serializers.CharField(source='task.title', read_only=True)
    task_points = serializers.IntegerField(source='task.points', read_only=True)
    
    class Meta:
        model = TaskSubmission
        fields = [
            'id', 'task', 'task_title', 'task_points', 'user', 'user_email',
            'submission_text', 'submission_url', 'status',
            'approved_by_slack_id', 'approved_at', 'rejection_reason',
            'created_at'
        ]
        read_only_fields = ['id', 'status', 'approved_by_slack_id', 'approved_at', 'created_at']


class CoworkingAvailabilitySerializer(serializers.Serializer):
    date = serializers.DateField()
    available_slots = serializers.IntegerField()
    total_capacity = serializers.IntegerField()
    cost_points = serializers.IntegerField()
    is_bookable = serializers.BooleanField()


class CoworkingBookingSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    is_refundable = serializers.SerializerMethodField()
    
    class Meta:
        model = CoworkingBooking
        fields = [
            'id', 'user', 'user_email', 'date', 'status', 'points_cost',
            'is_refundable', 'created_at', 'cancelled_at'
        ]
        read_only_fields = ['id', 'status', 'points_cost', 'created_at', 'cancelled_at']
    
    def get_is_refundable(self, obj):
        from .services import CoworkingService
        if obj.status != 'booked':
            return False
        return CoworkingService.is_refundable(obj.date)


class CoworkingDayCapacitySerializer(serializers.ModelSerializer):
    class Meta:
        model = CoworkingDayCapacity
        fields = ['date', 'capacity', 'notes']


class RewardsCatalogSerializer(serializers.ModelSerializer):
    can_afford = serializers.SerializerMethodField()
    
    class Meta:
        model = RewardsCatalog
        fields = [
            'code', 'name', 'description', 'cost_points',
            'fulfillment', 'is_active', 'max_per_user', 'can_afford'
        ]
    
    def get_can_afford(self, obj):
        request = self.context.get('request')
        if not request or not hasattr(request, 'user_balance'):
            return None
        return request.user_balance >= obj.cost_points


class RewardRedemptionSerializer(serializers.ModelSerializer):
    reward_name = serializers.CharField(source='reward.name', read_only=True)
    reward_cost = serializers.IntegerField(source='reward.cost_points', read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    total_cost = serializers.SerializerMethodField()
    
    class Meta:
        model = RewardRedemption
        fields = [
            'id', 'user', 'user_email', 'reward', 'reward_name', 'reward_cost',
            'quantity', 'total_cost', 'status', 'notes',
            'requested_at', 'approved_at', 'fulfilled_at', 'approved_by_slack_id'
        ]
        read_only_fields = ['id', 'status', 'requested_at', 'approved_at', 'fulfilled_at', 'approved_by_slack_id']
    
    def get_total_cost(self, obj):
        return obj.reward.cost_points * obj.quantity


class PointsBalanceSerializer(serializers.Serializer):
    """Legacy serializer for backwards compatibility."""
    slack_user_id = serializers.CharField()
    annual_balance = serializers.IntegerField()
    lifetime_balance = serializers.IntegerField()
