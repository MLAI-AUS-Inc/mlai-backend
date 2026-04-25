from rest_framework import serializers


class ContentFactoryRunAttemptSerializer(serializers.Serializer):
    attempt = serializers.IntegerField()
    status = serializers.CharField()
    message = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    completed_at = serializers.DateTimeField(required=False, allow_null=True)
    artifacts = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    input_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    output_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    notes_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    status_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ContentFactoryRunStepSerializer(serializers.Serializer):
    name = serializers.CharField()
    required = serializers.BooleanField(required=False, default=True)
    status = serializers.CharField(required=False, default="pending")
    attempts = serializers.IntegerField(required=False, default=0)
    message = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    completed_at = serializers.DateTimeField(required=False, allow_null=True)
    artifacts = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    latest_attempt_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    attempt_history = ContentFactoryRunAttemptSerializer(many=True, required=False, default=list)


class ContentFactoryRunSyncSerializer(serializers.Serializer):
    run_id = serializers.CharField()
    workflow = serializers.CharField()
    domain = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    github_repo = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    slack_user_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    status = serializers.CharField()
    current_step = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    artifact_root = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    step_order = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    started_at = serializers.DateTimeField(required=False, allow_null=True)
    updated_at = serializers.DateTimeField(required=False, allow_null=True)
    acceptance_summary = serializers.DictField(required=False, default=dict)
    verification_summary = serializers.DictField(required=False, default=dict)
    approval_state = serializers.CharField(required=False, default="not_required")
    resume_available = serializers.BooleanField(required=False, default=False)
    error = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    result = serializers.DictField(required=False, default=dict, allow_null=True)
    run_request = serializers.DictField(required=False, default=dict, allow_null=True)
    run_request_path = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    step_states = serializers.DictField(required=False, default=dict)


class ContentFactoryRunValleyJobSerializer(serializers.Serializer):
    job_id = serializers.CharField()
    transition = serializers.ChoiceField(choices=[
        ("queued", "Queued"),
        ("started", "Started"),
        ("finished", "Finished"),
    ])
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ContentFactoryRunControlSerializer(serializers.Serializer):
    actor = serializers.CharField(required=False, allow_blank=True, default="content-factory")
