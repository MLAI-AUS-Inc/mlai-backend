"""HTTP endpoints for the Watt *Smart Home (Beginner)* coding-blocks challenge.

Separate module from ``watt_views.py`` (the streamed-game challenge). It reuses only the
shared identity helpers (team -> class_id/household_id) so the device commands land on the
exact Firebase path the streamed Unity game already listens to.

    GET  /api/v1/hackathons/watt/smart-home/blocks/   -> palette catalog
    POST /api/v1/hackathons/watt/smart-home/deploy/   -> compile placed blocks -> write commands
"""
import time

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from generic_hackathons import smart_home_blocks as blocks
from generic_hackathons import smart_home_firebase as shf
from generic_hackathons import smart_home_policy as policy
from generic_hackathons import smart_home_progression as progression
from generic_hackathons.watt_views import (
    _class_id,
    _current_team,
    _get_watt_hackathon,
    _household_id,
    _team_size_gate,
)


class SmartHomeBlocksView(APIView):
    """Return the (server-owned) block catalog for the palette UI."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(
            {"groups": list(blocks.GROUPS), "blocks": blocks.public_catalog()},
            status=status.HTTP_200_OK,
        )


class SmartHomeDeployView(APIView):
    """Compile the placed blocks into device commands and write them to Firebase."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        hackathon = _get_watt_hackathon()
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {"error": "Join or create a Watt team before deploying your smart home."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = _team_size_gate(team)
        if gate is not None:
            return gate

        # Path B-lite: prefer a structured `pipeline` (Inputs/Schedule/Brain/Actions/Outputs/Safety)
        # which the server compiles against live game state. Fall back to a flat `blocks` list.
        # Stage-1 switchboard sends {switches: {bathroom: false, ...}} -> direct on/off.
        # Otherwise a structured `pipeline` (preferred) or a flat `blocks` list.
        switches = request.data.get("switches")
        use_switches = isinstance(switches, dict)

        pipeline = request.data.get("pipeline")
        use_pipeline = (not use_switches) and isinstance(pipeline, dict)
        block_ids = []
        if use_switches:
            unknown_dev = sorted(
                {str(d).strip() for d in switches if str(d).strip() not in progression.SWITCH_DEVICE_ROOM}
            )
            if unknown_dev:
                return Response(
                    {"error": f"Unknown switch devices: {', '.join(unknown_dev)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif not use_pipeline:
            raw_blocks = request.data.get("blocks", [])
            if not isinstance(raw_blocks, list):
                return Response(
                    {"error": "Provide a switches map, a pipeline object, or a blocks list."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            block_ids = [str(b).strip() for b in raw_blocks if str(b).strip()]
            unknown = sorted({b for b in block_ids if b not in blocks.known_block_ids()})
            if unknown:
                return Response(
                    {"error": f"Unknown block ids: {', '.join(unknown)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            all_ids = [
                str(b).strip()
                for slot in pipeline.values() if isinstance(slot, list)
                for b in slot if str(b).strip()
            ]
            unknown = sorted({b for b in all_ids if b not in policy.KNOWN_PIPELINE_IDS})
            if unknown:
                return Response(
                    {"error": f"Unknown pipeline block ids: {', '.join(unknown)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        class_id = _class_id()
        household_id = _household_id(team)

        # Need the live game tick (and live state for the policy), or Unity rejects commands as stale.
        try:
            observation = shf.read_observation(class_id, household_id)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"error": f"Could not reach the smart-home game state: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        now = shf.now_ms()
        liveness = shf.observation_liveness(observation, now)
        if not liveness["live"]:
            return Response(
                {
                    "error": "Your smart home isn't live yet. Start the stream, then deploy.",
                    "reason": liveness["reason"],
                    "observed_household": household_id,
                    "published_age_ms": liveness["age_ms"],
                    "server_now_ms": now,
                },
                status=status.HTTP_409_CONFLICT,
            )
        tick = shf.read_current_tick(observation)

        decisions = []
        brain_label = None
        brain_effect = None
        if use_switches:
            specs = []
            for dev, on in switches.items():
                room = progression.SWITCH_DEVICE_ROOM.get(str(dev).strip())
                if room is None:
                    continue
                specs.append(
                    {
                        "action": "set_lights",
                        "target_type": "lights",
                        "target_id": room,
                        "params": {"on": bool(on)},
                    }
                )
                decisions.append(f"Turned the {room} light {'on' if on else 'off'}.")
            if not specs:
                return Response(
                    {"error": "No valid switches to deploy."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif use_pipeline:
            # Stage-gate: reject pipeline blocks the team hasn't unlocked yet at the current
            # campaign day (the frontend hides them; this keeps a raw POST honest). Fail-open
            # when the day is unknown so a missing observation never blocks a deploy.
            day = observation.get("day") if isinstance(observation, dict) else None
            locked = progression.locked_block_ids_in(pipeline, day)
            if locked:
                return Response(
                    {"error": f"These blocks aren't unlocked yet (campaign day {day}): {', '.join(locked)}."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            compiled = policy.compile_policy(pipeline, observation)
            specs = compiled["commands"]
            decisions = compiled["decisions"]
            brain_label = compiled.get("brain")
            brain_effect = compiled.get("brain_effect")
        else:
            specs = blocks.compile_blocks(block_ids)

        commands = []
        rows = []
        for spec in specs:
            command = shf.build_command(
                action=spec["action"],
                target_type=spec["target_type"],
                target_id=spec["target_id"],
                params=spec["params"],
                tick_seen=tick,
            )
            commands.append(command)
            rows.append(
                {
                    "command_id": command["command_id"],
                    "block_id": spec.get("block_id"),
                    "action": spec["action"],
                    "target_id": spec["target_id"],
                }
            )

        try:
            written = shf.write_commands(class_id, household_id, commands)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"error": f"Failed to write commands to the game: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Publish the active brain so the streamed game can feature the AI bot in cutscenes
        # (which brain is running + its effect line). Best-effort: never fail a deploy over it.
        if use_pipeline:
            brain_id = next(
                (str(b).strip() for b in (pipeline.get("brain") or []) if str(b).strip()),
                None,
            )
            try:
                shf.write_policy(
                    class_id,
                    household_id,
                    {
                        "schema_version": "watt_hackathon_v2",
                        "brain_id": brain_id,
                        "brain_label": brain_label,
                        "brain_effect": brain_effect,
                        "deployed_at_ms": now,
                        "tick": tick,
                    },
                )
            except Exception:  # noqa: BLE001
                pass

        return Response(
            {
                "household_id": household_id,
                "tick_seen": tick,
                "deployed_count": len(written),
                "commands": rows,
                "decisions": decisions,
                "brain": brain_label,
                "brain_effect": brain_effect,
            },
            status=status.HTTP_200_OK,
        )


class SmartHomeStateView(APIView):
    """Live status for the web meta-layer: goal/day/wallet/cost/comfort, read from the
    score summary + observation the streamed game already publishes each tick."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        hackathon = _get_watt_hackathon()
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {"error": "Join or create a Watt team to view your smart home."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = _team_size_gate(team)
        if gate is not None:
            return gate

        class_id = _class_id()
        household_id = _household_id(team)
        try:
            observation = shf.read_observation(class_id, household_id)
            score = shf.read_score(class_id, household_id)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"error": f"Could not reach the smart-home game state: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        obs = observation if isinstance(observation, dict) else {}
        sc = score if isinstance(score, dict) else {}
        tariff = obs.get("tariff") or {}
        weather = obs.get("weather") or {}
        liveness = shf.observation_liveness(observation, shf.now_ms())

        return Response(
            {
                "live": liveness["live"],
                "live_reason": liveness["reason"],
                "published_age_ms": liveness["age_ms"],
                "household_id": household_id,
                "day": sc.get("day") if sc.get("day") is not None else obs.get("day"),
                "tick": obs.get("tick"),
                "game_time": obs.get("game_time"),
                "wallet": sc.get("money"),
                "cost": sc.get("energy_cost"),
                "energy_kwh": sc.get("energy_kwh"),
                "carbon": sc.get("carbon_kg"),
                "comfort": sc.get("mood"),
                "score": sc.get("score"),
                "tariff_period": tariff.get("period"),
                "weather_condition": weather.get("condition"),
            },
            status=status.HTTP_200_OK,
        )


class SmartHomeShopView(APIView):
    """Return the upgrades shop the streamed game publishes (visible catalog items + wallet)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        hackathon = _get_watt_hackathon()
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {"error": "Join or create a Watt team to view the shop."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = _team_size_gate(team)
        if gate is not None:
            return gate

        class_id = _class_id()
        household_id = _household_id(team)
        try:
            shop = shf.read_shop(class_id, household_id)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"error": f"Could not reach the shop: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        shop = shop if isinstance(shop, dict) else {}
        items = shop.get("items") if isinstance(shop.get("items"), list) else []
        return Response(
            {
                "available": bool(shop),
                "day": shop.get("day"),
                "wallet": shop.get("wallet"),
                "items": items,
            },
            status=status.HTTP_200_OK,
        )


class SmartHomeBuyView(APIView):
    """Buy a catalog upgrade by writing a purchase_upgrade command to the streamed game."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        hackathon = _get_watt_hackathon()
        team = _current_team(request.user, hackathon)
        if team is None:
            return Response(
                {"error": "Join or create a Watt team before buying upgrades."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate = _team_size_gate(team)
        if gate is not None:
            return gate

        item_id = str(request.data.get("item_id") or "").strip()
        if not item_id:
            return Response({"error": "item_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        class_id = _class_id()
        household_id = _household_id(team)
        try:
            observation = shf.read_observation(class_id, household_id)
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"error": f"Could not reach the smart-home game state: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        now = shf.now_ms()
        liveness = shf.observation_liveness(observation, now)
        if not liveness["live"]:
            return Response(
                {
                    "error": "Your smart home isn't live yet. Start the stream, then buy.",
                    "reason": liveness["reason"],
                    "observed_household": household_id,
                    "published_age_ms": liveness["age_ms"],
                    "server_now_ms": now,
                },
                status=status.HTTP_409_CONFLICT,
            )
        tick = shf.read_current_tick(observation)

        command = shf.build_command(
            action="purchase_upgrade", target_type="upgrade", target_id=item_id, params={}, tick_seen=tick,
        )
        try:
            shf.write_commands(class_id, household_id, [command])
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"error": f"Failed to send the purchase: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Briefly poll for the game's verdict so the UI can show "Purchased" or the failure reason.
        result = None
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                result = shf.read_command_result(class_id, household_id, command["command_id"])
            except Exception:  # noqa: BLE001
                result = None
            if isinstance(result, dict):
                break
            time.sleep(0.4)

        if isinstance(result, dict):
            return Response(
                {
                    "ok": bool(result.get("accepted")),
                    "item_id": item_id,
                    "reason": result.get("reason"),
                    "message": result.get("message"),
                },
                status=status.HTTP_200_OK,
            )
        return Response(
            {"ok": True, "pending": True, "item_id": item_id, "command_id": command["command_id"]},
            status=status.HTTP_200_OK,
        )
