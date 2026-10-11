"""Bounded input contracts; unknown fields are rejected."""
from rest_framework import serializers
from .sources import parse_resource


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object.")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({name: "Unknown field." for name in sorted(unknown)})
        return super().to_internal_value(data)


class ToolSerializer(StrictSerializer):
    tool = serializers.CharField(max_length=64)
    arguments = serializers.DictField(default=dict)


class ResourceSerializer(StrictSerializer):
    resource = serializers.CharField(max_length=255)

    def validate_resource(self, value):
        parse_resource(value)
        return value


class ReadSerializer(ResourceSerializer):
    cursor = serializers.CharField(max_length=2048, required=False, allow_blank=True, default="")
    limit = serializers.IntegerField(min_value=1, max_value=100, default=50)


class ThreadSerializer(ReadSerializer):
    thread_id = serializers.RegexField(r"^[0-9]{1,20}\.[0-9]{1,9}$", max_length=30)


class SearchSerializer(ReadSerializer):
    query = serializers.CharField(max_length=200)


class EmptySerializer(StrictSerializer):
    pass


class EventsSerializer(ResourceSerializer):
    cursor = serializers.CharField(max_length=2048, allow_blank=True, default="")
    limit = serializers.IntegerField(min_value=1, max_value=100, default=50)


class ConsentSerializer(StrictSerializer):
    name = serializers.CharField(max_length=100)
    runtime = serializers.ChoiceField(choices=("hermes", "external"), default="external")
    scopes = serializers.ListField(child=serializers.CharField(max_length=64), min_length=1, max_length=20)
    resources = serializers.ListField(child=serializers.CharField(max_length=255), min_length=1, max_length=20)
    expires_in_days = serializers.IntegerField(min_value=1, max_value=30, default=7)

    def validate_resources(self, value):
        for resource in value:
            parse_resource(resource)
        return sorted(set(value))


def validated(serializer_type, data):
    serializer = serializer_type(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class TaskCreateSerializer(ResourceSerializer):
    instruction = serializers.CharField(max_length=16000)
    idempotency_key = serializers.CharField(max_length=160)
    inputs = serializers.ListField(default=list, max_length=20)
    limits = serializers.DictField(default=dict)

    def validate_idempotency_key(self, value):
        if value.startswith(("slack-command:", "schedule:", "event:")):
            raise serializers.ValidationError("This idempotency namespace belongs to the scheduler.")
        return value


class LeaseSerializer(StrictSerializer):
    lease_token = serializers.UUIDField()
    lease_epoch = serializers.IntegerField(min_value=1)


class CompleteSerializer(LeaseSerializer):
    result = serializers.JSONField()
    actual_cost_microunits = serializers.IntegerField(min_value=0, default=0)


class FailSerializer(LeaseSerializer):
    error_code = serializers.RegexField(r"^[a-z0-9_]{1,64}$", default="worker_failed")
    retryable = serializers.BooleanField(default=False)
    details = serializers.DictField(required=False)


class ClaimSerializer(StrictSerializer):
    worker_id = serializers.RegexField(r"^[A-Za-z0-9_.:-]{1,120}$")


class ProposalSerializer(ResourceSerializer):
    kind = serializers.ChoiceField(choices=("message.send",))
    payload = serializers.DictField()
    idempotency_key = serializers.CharField(max_length=160)


class ResponsibilitySerializer(ResourceSerializer):
    name = serializers.CharField(max_length=120)
    instruction = serializers.CharField(max_length=16000)
    next_run_at = serializers.DateTimeField()
    interval_seconds = serializers.IntegerField(min_value=300, max_value=2678400, allow_null=True, default=None)
    timezone_name = serializers.CharField(max_length=64, default="Australia/Melbourne")
    quiet_start = serializers.TimeField(allow_null=True, default=None)
    quiet_end = serializers.TimeField(allow_null=True, default=None)
    limits = serializers.DictField(default=dict)


class SubscriptionSerializer(ResourceSerializer):
    event_types = serializers.ListField(child=serializers.ChoiceField(choices=(
        "message.created", "message.updated", "message.deleted", "membership.changed")), min_length=1, max_length=4)
    instruction = serializers.CharField(max_length=16000)
    limits = serializers.DictField(default=dict)


class TokenSerializer(StrictSerializer):
    grant_type = serializers.ChoiceField(choices=("authorization_code", "refresh_token"))
    client_id = serializers.CharField(max_length=255)
    resource = serializers.CharField(max_length=500)
    code = serializers.CharField(max_length=200, required=False)
    redirect_uri = serializers.CharField(max_length=2000, required=False)
    code_verifier = serializers.CharField(min_length=43, max_length=128, required=False)
    refresh_token = serializers.CharField(max_length=512, required=False)
    scope = serializers.CharField(max_length=2000, required=False)

    def validate(self, data):
        common = {"grant_type", "client_id", "resource"}
        if data["grant_type"] == "authorization_code":
            expected = common | {"code", "redirect_uri", "code_verifier"}
            if set(data) != expected:
                raise serializers.ValidationError("Authorization-code exchange fields are incomplete or unsupported.")
        elif not (common | {"refresh_token"}).issubset(data) or set(data) - (common | {"refresh_token", "scope"}):
            raise serializers.ValidationError("Refresh exchange fields are incomplete or unsupported.")
        return data


class SlackAdmissionSerializer(StrictSerializer):
    team_id = serializers.RegexField(r"^T[A-Z0-9]{1,29}$")
    user_id = serializers.RegexField(r"^[UW][A-Z0-9]{1,29}$")
    channel_id = serializers.RegexField(r"^C[A-Z0-9]{1,29}$")
    source_event_id = serializers.RegexField(r"^slack:T[A-Z0-9]+:[A-Za-z0-9_.:-]{1,100}$", max_length=140)
    instruction = serializers.CharField(min_length=1, max_length=16000)
