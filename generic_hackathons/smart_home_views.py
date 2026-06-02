"""HTTP endpoints for the Watt *Smart Home (Beginner)* coding-blocks challenge.

Separate module from ``watt_views.py`` (the streamed-game challenge). It reuses only the
shared identity helpers (team -> class_id/household_id) so the device commands land on the
exact Firebase path the streamed Unity game already listens to.

    GET  /api/v1/hackathons/watt/smart-home/blocks/   -> palette catalog
    POST /api/v1/hackathons/watt/smart-home/deploy/   -> compile placed blocks -> write commands
"""
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from generic_hackathons import smart_home_blocks as blocks
from generic_hackathons import smart_home_firebase as shf
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

        raw_blocks = request.data.get("blocks", [])
        if not isinstance(raw_blocks, list):
            return Response(
                {"error": "blocks must be a list of block ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        block_ids = [str(b).strip() for b in raw_blocks if str(b).strip()]

        unknown = sorted({b for b in block_ids if b not in blocks.known_block_ids()})
        if unknown:
            return Response(
                {"error": f"Unknown block ids: {', '.join(unknown)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        class_id = _class_id()
        household_id = _household_id(team)

        # Need the live game tick to stamp commands, or Unity rejects them as stale.
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
                    "block_id": spec["block_id"],
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
            },
            status=status.HTTP_200_OK,
        )
