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
from generic_hackathons.watt_views import (
    _class_id,
    _current_team,
    _get_watt_hackathon,
    _household_id,
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

        # Path B-lite: prefer a structured `pipeline` (Inputs/Schedule/Brain/Actions/Outputs/Safety)
        # which the server compiles against live game state. Fall back to a flat `blocks` list.
        pipeline = request.data.get("pipeline")
        use_pipeline = isinstance(pipeline, dict)
        block_ids = []
        if not use_pipeline:
            raw_blocks = request.data.get("blocks", [])
            if not isinstance(raw_blocks, list):
                return Response(
                    {"error": "Provide a pipeline object or a blocks list."},
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
        if not isinstance(observation, dict) or not shf.is_observation_live(observation, shf.now_ms()):
            return Response(
                {"error": "Your smart home isn't live yet. Start the stream, then deploy."},
                status=status.HTTP_409_CONFLICT,
            )
        tick = shf.read_current_tick(observation)

        decisions = []
        brain_label = None
        if use_pipeline:
            compiled = policy.compile_policy(pipeline, observation)
            specs = compiled["commands"]
            decisions = compiled["decisions"]
            brain_label = compiled.get("brain")
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

        return Response(
            {
                "household_id": household_id,
                "tick_seen": tick,
                "deployed_count": len(written),
                "commands": rows,
                "decisions": decisions,
                "brain": brain_label,
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
        live = isinstance(observation, dict) and shf.is_observation_live(observation, shf.now_ms())

        return Response(
            {
                "live": live,
                "household_id": household_id,
                "day": sc.get("day") if sc.get("day") is not None else obs.get("day"),
                "tick": obs.get("tick"),
                "game_time": obs.get("game_time"),
                "wallet": sc.get("money"),
                "cost": sc.get("energy_cost"),
                "energy_kwh": sc.get("energy_kwh"),
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
        if not isinstance(observation, dict) or not shf.is_observation_live(observation, shf.now_ms()):
            return Response(
                {"error": "Your smart home isn't live yet. Start the stream, then buy."},
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
