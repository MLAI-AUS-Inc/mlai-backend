from rest_framework import serializers
from .models import (
    PointsAdmin, Minter, Task, Ledger, PointsAccount,
    TaskAssignment, TaskSubmission, TaskActivity,
    CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, TaskTemplate, QuestProgress,
    PointsRequest,
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
        fields = [
            'user_id',
            'email',
            'slack_id',
            'balance',
            'earned_balance',
            'purchased_topup_balance',
            'lifetime_earned',
            'lifetime_purchased_topup',
            'lifetime_spent',
            'expired_or_reversed_points',
            'updated_at',
        ]


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


class TaskAssignmentSerializer(serializers.ModelSerializer):
    assigned_user_email = serializers.EmailField(source='assigned_user.email', read_only=True, allow_null=True)

    class Meta:
        model = TaskAssignment
        fields = [
            'id',
            'task',
            'assigned_user',
            'assigned_user_email',
            'assigned_to_slack_id',
            'claimed_points_snapshot',
            'status',
            'claimed_at',
            'released_at',
            'submitted_at',
            'approved_at',
            'approved_by_slack_id',
            'awarded_points',
            'closed_reason',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'claimed_at',
            'released_at',
            'submitted_at',
            'approved_at',
            'created_at',
            'updated_at',
        ]


class TaskSerializer(serializers.ModelSerializer):
    assigned_to_email = serializers.EmailField(source='assigned_user.email', read_only=True, allow_null=True)
    current_assignment = serializers.SerializerMethodField()
    group_staffed_count = serializers.SerializerMethodField()
    group_open_count = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            'id',
            'task_code',
            'title',
            'description',
            'portfolio',
            'work_domain',
            'review_flow',
            'points',
            'points_estimate',
            'points_min',
            'points_max',
            'status',
            'visibility',
            'volunteer_ready',
            'difficulty',
            'estimate_minutes',
            'outcome',
            'definition_of_done',
            'acceptance_criteria',
            'how_to_test',
            'repo',
            'reviewer_slack_id',
            'fallback_reviewer_slack_id',
            'source_system',
            'source_ref',
            'source_url',
            'group_key',
            'slot_label',
            'group_capacity',
            'blocked_reason',
            'metadata',
            'created_by_user_id',
            'assigned_to_user_id',
            'closed_by_user_id',
            'assigned_user',
            'assigned_to_email',
            'slack_channel_id',
            'slack_thread_ts',
            'due_date',
            'current_assignment',
            'group_staffed_count',
            'group_open_count',
            'created_at',
            'updated_at',
            'closed_at',
        ]
        read_only_fields = ('id', 'created_at', 'updated_at', 'closed_at')

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = getattr(self, 'instance', None)

        if instance:
            base_points = attrs.get('points_estimate', attrs.get('points', instance.points_estimate))
        else:
            base_points = attrs.get('points_estimate', attrs.get('points', 1))

        attrs.setdefault('points', base_points)
        attrs.setdefault('points_estimate', base_points)
        attrs.setdefault('points_min', attrs['points_estimate'])
        attrs.setdefault('points_max', attrs['points_estimate'])

        if attrs['points_min'] > attrs['points_estimate']:
            raise serializers.ValidationError({'points_min': 'points_min cannot exceed points_estimate'})
        if attrs['points_max'] < attrs['points_estimate']:
            raise serializers.ValidationError({'points_max': 'points_max cannot be below points_estimate'})

        volunteer_ready = attrs.get(
            'volunteer_ready',
            instance.volunteer_ready if instance else False,
        )
        review_flow = attrs.get(
            'review_flow',
            instance.review_flow if instance else Task.REVIEW_FLOW_CHOICES[1][0],
        )
        reviewer_slack_id = attrs.get(
            'reviewer_slack_id',
            instance.reviewer_slack_id if instance else None,
        )
        fallback_reviewer_slack_id = attrs.get(
            'fallback_reviewer_slack_id',
            instance.fallback_reviewer_slack_id if instance else None,
        )
        repo = attrs.get('repo', instance.repo if instance else '')
        acceptance_criteria = attrs.get(
            'acceptance_criteria',
            instance.acceptance_criteria if instance else '',
        )
        how_to_test = attrs.get(
            'how_to_test',
            instance.how_to_test if instance else '',
        )

        if volunteer_ready and review_flow == 'pr_review':
            errors = {}
            if not repo:
                errors['repo'] = 'repo is required for volunteer-ready PR review tasks'
            if not (reviewer_slack_id or fallback_reviewer_slack_id):
                errors['reviewer_slack_id'] = 'a reviewer or fallback reviewer is required'
            if not acceptance_criteria:
                errors['acceptance_criteria'] = 'acceptance_criteria is required'
            if not how_to_test:
                errors['how_to_test'] = 'how_to_test is required'
            if errors:
                raise serializers.ValidationError(errors)

        return attrs

    def create(self, validated_data):
        self._sync_points_fields(validated_data)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        self._sync_points_fields(validated_data, instance=instance)
        return super().update(instance, validated_data)

    def _sync_points_fields(self, validated_data, instance=None):
        if instance:
            base_points = validated_data.get('points_estimate', validated_data.get('points', instance.points_estimate))
        else:
            base_points = validated_data.get('points_estimate', validated_data.get('points', 1))

        validated_data['points_estimate'] = base_points
        validated_data['points'] = validated_data.get('points', base_points)
        validated_data['points_min'] = validated_data.get('points_min', validated_data['points_estimate'])
        validated_data['points_max'] = validated_data.get('points_max', validated_data['points_estimate'])

    def get_current_assignment(self, obj):
        assignment = obj.get_current_assignment()
        if not assignment:
            return None
        return TaskAssignmentSerializer(assignment).data

    def get_group_staffed_count(self, obj):
        if not obj.group_key:
            return None
        return Task.objects.filter(group_key=obj.group_key).filter(
            status__in=['claimed', 'submitted', 'approved']
        ).count()

    def get_group_open_count(self, obj):
        if not obj.group_key:
            return None
        return Task.objects.filter(group_key=obj.group_key, status='open').count()


class TaskTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskTemplate
        fields = [
            'name', 'alias', 'points', 'description', 'is_active'
        ]


class TaskSubmissionSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    task_title = serializers.CharField(source='task.title', read_only=True)
    task_points = serializers.IntegerField(source='task.points_estimate', read_only=True)

    class Meta:
        model = TaskSubmission
        fields = [
            'id', 'task', 'assignment', 'task_title', 'task_points', 'user', 'user_email',
            'submission_text', 'submission_url', 'status', 'evidence_kind',
            'evidence_payload', 'review_notes', 'reviewed_by_slack_id', 'reviewed_at',
            'approved_by_slack_id', 'approved_at', 'rejection_reason',
            'ledger_entry', 'created_at'
        ]
        read_only_fields = [
            'id',
            'status',
            'reviewed_by_slack_id',
            'reviewed_at',
            'approved_by_slack_id',
            'approved_at',
            'ledger_entry',
            'created_at',
        ]


class TaskActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskActivity
        fields = [
            'id',
            'task',
            'assignment',
            'submission',
            'event_type',
            'actor_slack_id',
            'summary',
            'metadata',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


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
            'fulfillment', 'is_active', 'max_per_user', 'can_afford',
            'stock_remaining'
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


class PointsRequestSerializer(serializers.ModelSerializer):
    ledger_entry_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = PointsRequest
        fields = [
            'id',
            'requester_slack_id',
            'target_slack_id',
            'points',
            'reason',
            'status',
            'approved_by_slack_id',
            'approved_at',
            'ledger_entry_id',
            'slack_channel_id',
            'slack_thread_ts',
            'slack_summary_message_ts',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'status',
            'approved_by_slack_id',
            'approved_at',
            'ledger_entry_id',
            'created_at',
            'updated_at',
        ]


class PointsBalanceSerializer(serializers.Serializer):
    """Legacy serializer for backwards compatibility."""
    slack_user_id = serializers.CharField()
    annual_balance = serializers.IntegerField()
    lifetime_balance = serializers.IntegerField()


class QuestProgressSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuestProgress
        fields = [
            'slack_user_id', 'quest_id', 'current_count', 
            'completed', 'first_progress_at', 'completed_at'
        ]
        read_only_fields = ['first_progress_at', 'created_at', 'updated_at']


class QuestProgressInputSerializer(serializers.Serializer):
    slack_user_id = serializers.CharField(max_length=50)
    quest_id = serializers.CharField(max_length=50)
    increment_by = serializers.IntegerField(min_value=1, default=1, required=False)


class QuestCompleteInputSerializer(serializers.Serializer):
    slack_user_id = serializers.CharField(max_length=50)
    quest_id = serializers.CharField(max_length=50)
