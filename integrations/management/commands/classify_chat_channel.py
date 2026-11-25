"""Classify a previously verified shared channel while preserving its Chat UUID."""
import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from integrations.models import ManagedChannelScope, ManagedEntity, ManagedRoom
from integrations.services.managed_channels.registry import eligible_room
from integrations.services.managed_channels.relay_client import apply_room


class Command(BaseCommand):
    help = "Preview or apply a shared room section without changing its members, title or lifecycle"

    def add_arguments(self, parser):
        parser.add_argument("--community", required=True, type=UUID)
        parser.add_argument("--room", required=True, type=UUID)
        parser.add_argument("--section", required=True, choices=("committee", "project", "event"))
        parser.add_argument("--visibility", required=True, choices=("public", "private"))
        parser.add_argument("--member", action="append", default=[], help="Verified device public key for a private room")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        community, channel_id = str(options["community"]), str(options["room"])
        section = options["section"]
        visibility = options["visibility"]
        config = {"community":community, "bindings":{channel_id:{"channel_id":channel_id, "members":options["member"]}}}
        snap = {"external_id":channel_id, "visibility":visibility, "public_authorized":visibility == "public"}
        if visibility == "public" and options["member"]:
            raise CommandError("Specify member keys only for a private room")
        policy = eligible_room(config, snap)
        if not policy:
            raise CommandError("The approved private roster could not be verified")
        with transaction.atomic():
            room = ManagedRoom.objects.select_for_update().select_related("entity__authority").filter(
                community=community, channel_id=channel_id).first()
            if room and room.entity.authority.provider != "curated":
                raise CommandError("Room belongs to a provider binding; use its source configuration")
            action = "existing" if room and room.section_override == section else "classify"
            if options["apply"]:
                if not room:
                    scope, _ = ManagedChannelScope.objects.get_or_create(community=community, provider="curated", scope="operator")
                    entity = ManagedEntity.objects.create(community=community, authority=scope, section=section, revision=1)
                    room = ManagedRoom.objects.create(entity=entity, community=community, channel_id=channel_id,
                        visibility=visibility, members=policy["members"], adopted=True, section_override=section)
                else:
                    if room.visibility != visibility:
                        raise CommandError("Audience differs from the registered shared room")
                    if action != "existing":
                        room.entity.revision += 1
                    room.entity.section = section
                    room.entity.save(update_fields=["section", "revision"])
                    room.section_override = section
                    room.members = policy["members"]
                    room.save(update_fields=["section_override","members"])
                # Adopted rooms must already exist and their current verified
                # roster must match. No provider keys or links enter this write.
                apply_room(config, room, {"revision":room.entity.revision, "section":section, "status":"active"})
        self.stdout.write(json.dumps({"channel_id":channel_id, "section":section, "action":action, "apply":options["apply"]}))
