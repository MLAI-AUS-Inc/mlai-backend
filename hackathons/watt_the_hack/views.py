from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from generic_hackathons.models import GenericHackathonTeam

from .engine import ensure_engine_importable
from .serializers import InitRequestSerializer, RunRequestSerializer, StepRequestSerializer

ensure_engine_importable()

from watt_the_hack.api.sandbox import ControllerCompileError, compile_controller_source
from watt_the_hack.controllers.parametric import (
    ParametricControllerParams,
    make_parametric_controller,
)
from watt_the_hack.data_loaders.scenarios import (
    config_overrides as scenario_config_overrides,
    find_scenario_by_id,
    list_scenarios,
    load_scenario,
    public_metadata,
    scoring_config,
)
from watt_the_hack.engine.engine import Engine, SimulationConfig
from watt_the_hack.metrics.metrics import Metrics
from watt_the_hack.simulation.runner import run_strategy
from watt_the_hack.simulation.strategy import ResolvedStrategy


WATT_THE_HACK_SLUG = "watt-the-hack"

_engine = Engine()
_submission_counts: dict[str, int] = defaultdict(int)


def _settings_list(name: str, default: list[str]) -> list[str]:
    value = getattr(settings, name, None)
    if value is None:
        return default
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    return list(value)


def _unlocked_scenarios() -> set[str]:
    default = {"t1_welcome", "t2_first_code"}
    unlocked = set(_settings_list("WATT_THE_HACK_UNLOCKED_SCENARIOS", list(default)))
    if getattr(settings, "WATT_THE_HACK_AUTO_UNLOCK", True):
        unlocked.update(s["id"] for s in list_scenarios(include_judging=False))
    return unlocked


def _sim_access_allowed(user) -> bool:
    if not getattr(settings, "WATT_THE_HACK_REQUIRE_TEAM_FOR_SIM", False):
        return True
    return GenericHackathonTeam.objects.filter(
        hackathon__slug=WATT_THE_HACK_SLUG,
        members=user,
    ).exists()


class WattTheHackSimulationMixin:
    permission_classes = [permissions.IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not _sim_access_allowed(request.user):
            self.permission_denied(
                request,
                message="Join or create a Watt The Hack team before opening the sandbox.",
            )


class ScenarioListView(WattTheHackSimulationMixin, APIView):
    def get(self, request):
        unlocked = _unlocked_scenarios()
        scenarios = [
            s for s in list_scenarios() if s["id"] in unlocked
        ]
        return Response(scenarios, status=status.HTTP_200_OK)


class InitView(WattTheHackSimulationMixin, APIView):
    def post(self, request):
        serializer = InitRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scenario_id = serializer.validated_data.get("scenario_id")
        if not scenario_id:
            return Response(
                {"detail": "scenario_id is required. The default profile has been removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        unlocked = _unlocked_scenarios()
        if scenario_id not in unlocked:
            return Response(
                {"detail": "This scenario is locked or has not been released yet."},
                status=status.HTTP_403_FORBIDDEN,
            )

        path = find_scenario_by_id(scenario_id)
        if path is None:
            return Response(
                {"detail": f"Unknown scenario_id: {scenario_id!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        spec, state = load_scenario(path)
        spec_meta = public_metadata(spec)
        steps = len(state["_profiles_full"]["demand"])
        _engine.add_forecast_to_state(state)

        if _is_judging_scenario(spec_meta.get("pool")):
            return Response(
                {
                    "detail": (
                        "Judging scenarios cannot be initialized step-by-step. "
                        "Use /sim/run for full evaluation."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        _prepare_state_for_response(state)
        return Response(
            {"state": state, "steps": steps, "scenario": spec_meta},
            status=status.HTTP_200_OK,
        )


class StepView(WattTheHackSimulationMixin, APIView):
    def post(self, request):
        serializer = StepRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        state = data["state"]

        controller_fn, controller_error = _resolve_controller(data["controller"])
        controller_state = _state_visible_to_controller(state)
        try:
            action = controller_fn(controller_state)
        except Exception as exc:  # noqa: BLE001
            action = _fallback_controller()(controller_state)
            controller_error = controller_error or f"Runtime error: {exc}"

        engine_state = _rehydrate_state_for_engine(state)
        new_state, outputs = _engine.step(engine_state, action)

        scenario_id = state.get("scenario_id")
        if scenario_id and _is_judging_scenario_by_id(scenario_id):
            return Response(
                {"detail": "Judging scenarios cannot be stepped. Use /sim/run for full evaluation."},
                status=status.HTTP_403_FORBIDDEN,
            )

        _prepare_state_for_response(new_state)
        return Response(
            {"state": new_state, "outputs": outputs, "controller_error": controller_error},
            status=status.HTTP_200_OK,
        )


class RunView(WattTheHackSimulationMixin, APIView):
    def post(self, request):
        serializer = RunRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        controller_fn, controller_error = _resolve_controller(data["controller"])
        fallback = _fallback_controller()

        is_judging = False
        scoring: dict[str, Any] = {}
        overrides: dict[str, Any] = {}
        path = None
        state = data["state"]
        scenario_id = data.get("scenario_id") or state.get("scenario_id")

        if scenario_id:
            unlocked = _unlocked_scenarios()
            if scenario_id not in unlocked:
                return Response(
                    {"detail": "This scenario is locked or has not been released yet."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            path = find_scenario_by_id(scenario_id)
            if path is not None:
                spec, _ = load_scenario(path)
                scoring = scoring_config(spec)
                overrides = scenario_config_overrides(spec)
                is_judging = spec.get("pool") == "judging"

        run_engine = Engine(config=SimulationConfig(**overrides)) if overrides else _engine
        dt_hours = getattr(run_engine, "dt_hours", run_engine.config.dt_hours)
        metrics = Metrics(
            dt_hours=dt_hours,
            baselines={**Metrics().baselines, **scoring.get("baselines", {})},
        )

        if is_judging:
            auth_error = _validate_judging_auth(data)
            if auth_error:
                return auth_error

        engine_state = _rehydrate_state_for_engine(state)
        if is_judging and path is not None:
            _, full_state = load_scenario(path)
            run_engine.add_forecast_to_state(full_state)
            engine_state = full_state

        states: list[dict[str, Any]] = []
        outputs_list: list[dict[str, Any]] = []

        def safe_step(view: dict) -> dict:
            nonlocal controller_error
            try:
                return controller_fn(view)
            except Exception as exc:  # noqa: BLE001
                controller_error = controller_error or f"Runtime error: {exc}"
                return fallback(view)

        def capture(_i: int, _view, _action, outputs: dict, post_state: dict) -> None:
            out_state = dict(post_state)
            out_outputs = dict(outputs)
            if is_judging:
                _strip_judging_data(out_state, out_outputs)
            _prepare_state_for_response(out_state)
            states.append(out_state)
            outputs_list.append(out_outputs)

        strategy = ResolvedStrategy(step=safe_step, kind="callable", name="browser")
        run_result = run_strategy(
            run_engine,
            engine_state,
            strategy,
            data.get("steps"),
            on_step=capture,
            metrics=metrics,
        )
        final_state = dict(run_result["final_state"])

        if is_judging:
            _strip_judging_data(final_state)
            states = []
            outputs_list = []

        _prepare_state_for_response(final_state)
        return Response(
            {
                "final_state": final_state,
                "states": states,
                "outputs": outputs_list,
                "metrics": metrics.summary(),
                "controller_error": controller_error,
            },
            status=status.HTTP_200_OK,
        )


def _validate_judging_auth(data: dict[str, Any]) -> Response | None:
    registered_teams = getattr(settings, "WATT_THE_HACK_REGISTERED_TEAMS", {})
    team = data.get("team_id")
    if not team:
        return Response(
            {"detail": "team_id is required for judging scenarios."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    expected_token = registered_teams.get(team)
    if not expected_token:
        return Response(
            {"detail": f"Unregistered team_id: '{team}'. Please contact the organizers."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if data.get("team_token") != expected_token:
        return Response(
            {"detail": "Invalid team_token. Authentication failed."},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    if _submission_counts[team] >= getattr(settings, "WATT_THE_HACK_MAX_JUDGING_RUNS", 3):
        return Response(
            {"detail": "Rate limit exceeded: max 3 judging submissions per team."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    _submission_counts[team] += 1
    return None


def _fallback_controller():
    return make_parametric_controller(ParametricControllerParams())


def _resolve_controller(spec: dict[str, Any]):
    if spec.get("kind") == "simple":
        params = spec.get("params") or {}
        return make_parametric_controller(
            ParametricControllerParams(
                battery_flow_mw=params.get("battery_flow_mw", 0.0),
                emergency_generator=params.get("emergency_generator", 0.0),
                curtail_solar=params.get("curtail_solar", 0.0),
                fcas_reserve_mw=params.get("fcas_reserve_mw", 0.0),
                subscribe_ids=params.get("subscribe_ids", False),
            )
        ), None

    try:
        return compile_controller_source(spec["source"]), None
    except ControllerCompileError as exc:
        return _fallback_controller(), str(exc)


def _is_judging_scenario(pool: str | None) -> bool:
    return pool == "judging"


def _is_judging_scenario_by_id(scenario_id: str) -> bool:
    path = find_scenario_by_id(scenario_id)
    if path:
        spec, _ = load_scenario(path)
        return _is_judging_scenario(spec.get("pool"))
    return False


def _prepare_state_for_response(state: dict[str, Any]) -> None:
    for key in [k for k in state.keys() if k.startswith("_")]:
        state.pop(key, None)


def _state_visible_to_controller(state: dict[str, Any]) -> dict[str, Any]:
    return Engine.controller_view(state)


_REHYDRATE_KEYS: tuple[str, ...] = (
    "_profiles_full",
    "_price_profile_full",
    "_events_full",
    "_forecast_config_full",
    "_attack_windows_full",
    "features",
)


def _rehydrate_state_for_engine(state: dict[str, Any]) -> dict[str, Any]:
    scenario_id = state.get("scenario_id")
    if not scenario_id:
        return state
    path = find_scenario_by_id(str(scenario_id))
    if path is None:
        return state
    _, scenario_state = load_scenario(path)
    engine_state = dict(state)
    for key in _REHYDRATE_KEYS:
        if key in scenario_state:
            engine_state[key] = scenario_state[key]
    return engine_state


def _strip_judging_data(state: dict[str, Any], outputs: dict[str, Any] | None = None) -> None:
    state.pop("forecast", None)
    if outputs is not None:
        outputs.pop("import_price", None)
        outputs.pop("export_price", None)

