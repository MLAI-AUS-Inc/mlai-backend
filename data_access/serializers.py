from rest_framework import serializers


FILTER_OPERATORS = ("eq", "neq", "gt", "gte", "lt", "lte", "in", "icontains")
QUERY_OPERATIONS = ("list", "count", "aggregate")


class DataFilterSerializer(serializers.Serializer):
    field = serializers.CharField(max_length=120)
    operator = serializers.ChoiceField(choices=FILTER_OPERATORS)
    value = serializers.JSONField()

    def validate(self, attrs):
        operator = attrs["operator"]
        value = attrs["value"]
        if operator == "in" and (not isinstance(value, list) or not value):
            raise serializers.ValidationError({"value": "`in` filters require a non-empty list."})
        if operator == "icontains" and not isinstance(value, str):
            raise serializers.ValidationError({"value": "`icontains` filters require a string value."})
        return attrs


class DataOrderBySerializer(serializers.Serializer):
    field = serializers.CharField(max_length=120)
    direction = serializers.ChoiceField(choices=("asc", "desc"), default="asc")


class DataQuerySerializer(serializers.Serializer):
    requester_slack_id = serializers.CharField(max_length=80, trim_whitespace=True)
    resource = serializers.CharField(max_length=120)
    operation = serializers.ChoiceField(choices=QUERY_OPERATIONS, default="list")
    fields = serializers.ListField(
        child=serializers.CharField(max_length=120),
        required=False,
        allow_empty=False,
    )
    filters = DataFilterSerializer(many=True, required=False)
    group_by = serializers.ListField(
        child=serializers.CharField(max_length=120),
        required=False,
        allow_empty=True,
    )
    order_by = DataOrderBySerializer(many=True, required=False)
    limit = serializers.IntegerField(required=False, min_value=1)
    offset = serializers.IntegerField(required=False, min_value=0, default=0)

    def validate_requester_slack_id(self, value):
        value = str(value or "").strip()
        if not value:
            raise serializers.ValidationError("requester_slack_id is required.")
        return value
