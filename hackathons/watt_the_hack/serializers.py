from __future__ import annotations

from rest_framework import serializers

from .engine import ensure_engine_importable

ensure_engine_importable()

from watt_the_hack.constants import DEFAULT_STEPS

# Hard upper bound on a sandbox /sim/run or /sim/init request. The longest
# real scenario is 288 steps (3 days @ 15 min); this leaves generous headroom
# while preventing a participant from passing steps=100_000_000 and pinning a
# gunicorn worker (the loop is synchronous and accumulates per-step state in
# memory). Sandbox-only — judging runs on the GKE admin server with its own
# fixed step count.
MAX_SIM_STEPS = 2016


class ParametricControllerParamsSerializer(serializers.Serializer):
    battery_flow_mw = serializers.FloatField(default=0.0)
    emergency_generator = serializers.FloatField(default=0.0)
    curtail_solar = serializers.FloatField(default=0.0)
    fcas_reserve_mw = serializers.FloatField(default=0.0)
    subscribe_ids = serializers.BooleanField(default=False)


class ControllerSpecField(serializers.Field):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Controller must be an object.")

        kind = data.get("kind", "simple")
        if kind == "simple":
            params_serializer = ParametricControllerParamsSerializer(
                data=data.get("params") or {}
            )
            params_serializer.is_valid(raise_exception=True)
            return {"kind": "simple", "params": params_serializer.validated_data}

        if kind == "code":
            source = data.get("source")
            if not isinstance(source, str) or not source.strip():
                raise serializers.ValidationError("Code controllers require source.")
            return {"kind": "code", "source": source}

        raise serializers.ValidationError("Controller kind must be simple or code.")

    def to_representation(self, value):
        return value


class InitRequestSerializer(serializers.Serializer):
    steps = serializers.IntegerField(required=False, default=DEFAULT_STEPS, min_value=1, max_value=MAX_SIM_STEPS)
    scenario_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class StepRequestSerializer(serializers.Serializer):
    state = serializers.JSONField()
    controller = ControllerSpecField(required=False)

    def validate(self, attrs):
        attrs.setdefault("controller", {"kind": "simple", "params": {}})
        return attrs


class RunRequestSerializer(serializers.Serializer):
    state = serializers.JSONField()
    controller = ControllerSpecField(required=False)
    steps = serializers.IntegerField(required=False, default=DEFAULT_STEPS, min_value=1, max_value=MAX_SIM_STEPS)
    scenario_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    team_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    team_token = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        attrs.setdefault("controller", {"kind": "simple", "params": {}})
        return attrs

