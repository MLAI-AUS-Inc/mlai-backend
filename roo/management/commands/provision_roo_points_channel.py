"""Provision the fixed Roo Points room through normal relay creation."""

import os

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from agent_runtime.native_crypto import sign_event
from roo.native_points_requests import RooRelay, tags
from roo.points_request_workflow import ROO_POINTS_CHANNEL_ID

EXPECTED = [
    ["h", str(ROO_POINTS_CHANNEL_ID)],
    ["name", "roo-points"],
    ["visibility", "open"],
    ["channel_type", "stream"],
    ["about", "Ask Roo for points and celebrate level-ups with the MLAI community."],
]


class Command(BaseCommand):
    help = "Create the fixed Roo Points stream using a relay channel administrator"

    def handle(self, *args, **options):
        key = os.getenv("ROO_POINTS_PROVISION_SIGNING_KEY", "")
        if not key:
            raise CommandError("Set ROO_POINTS_PROVISION_SIGNING_KEY for a channel administrator")
        relay = RooRelay(key)
        existing = relay.query(ROO_POINTS_CHANNEL_ID, [9007], limit=2)
        if not existing:
            event = sign_event(relay.key, kind=9007, created_at=timezone.now().timestamp(),
                               tags=EXPECTED, content="")
            try:
                relay.publish(ROO_POINTS_CHANNEL_ID, event)
            except ValueError:
                # A concurrent provisioner may have created the same room.
                pass
            existing = relay.query(ROO_POINTS_CHANNEL_ID, [9007], limit=2)
        if len(existing) != 1 or any(tags(existing[0], tag[0]) != [tag] for tag in EXPECTED):
            raise CommandError("Roo Points room is missing or has conflicting metadata")
        self.stdout.write(str(ROO_POINTS_CHANNEL_ID))
