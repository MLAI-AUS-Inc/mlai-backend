"""Transactional identity resolution and scoped destination work."""
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from integrations.models import (
    CommunityBridgeChannel, ManagedChannelScope, ManagedDelivery, ManagedEntity,
    ManagedReference, ManagedRoom, SlackDmMirrorConversation,
)

from .config import installations, scope_config


def scope_for(config):
    return ManagedChannelScope.objects.get_or_create(
        community=config["community"], provider=config["provider"], scope=config["scope"],
    )[0]


def alias_scope(scope, data):
    alias = data.get("alias") or {}
    target = next((item for item in installations().values()
                   if item["community"] == scope.community and item["provider"] == alias.get("provider")
                   and item["scope"] == alias.get("scope")), None)
    if alias and (not target or not alias.get("external_id")):
        raise ValueError("Unverified reference scope")
    return (scope_for(target), alias["external_id"]) if target else (None, "")


def eligible_room(config, data):
    explicit = (config.get("bindings") or {}).get(data["external_id"], {})
    channel = explicit.get("channel_id") or data.get("channel_id")
    member_mirror = False
    if data.get("slack_channel_id") and data.get("slack_workspace_id"):
        match = CommunityBridgeChannel.objects.filter(
            destination_platform="buzz", destination_workspace_id=config.get("relay_workspace", config["community"]),
            slack_workspace_id=data["slack_workspace_id"], slack_channel_id=data["slack_channel_id"],
        ).exclude(destination_channel_id="").first()
        if match:
            if (not match.enabled or match.mirror_decision != "mirror" or match.hold_reason
                    or match.slack_visibility != "public" or match.slack_deleted or match.slack_archived):
                raise ValueError("Existing Slack mapping requires revalidation")
            member_mirror = match.audience == "members"
            if channel and str(channel) != match.destination_channel_id:
                raise ValueError("Ambiguous shared room")
            channel = match.destination_channel_id
    if channel:
        channel = UUID(str(channel))
        if SlackDmMirrorConversation.objects.filter(mlai_channel_id=channel).exists():
            raise ValueError("Personal import cannot become a shared room")
    community_visible = data.get("visibility") == "public" and data.get("public_authorized") is True
    visibility = "public" if community_visible and not member_mirror else "private"
    members = explicit.get("members") or (config.get("private_rosters") or {}).get(data["external_id"], [])
    if visibility == "private" and (not isinstance(members, list) or not members):
        return None
    if any(not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key)
           for key in members):
        raise ValueError("Invalid verified roster")
    if visibility == "private":
        from community_chat.models import CommunityChatDevice
        valid = set(CommunityChatDevice.objects.filter(public_key__in=members, status="verified",
                    revoked_at__isnull=True, user__is_active=True).values_list("public_key", flat=True))
        if set(members) != valid:
            return None
    return {"channel_id": channel, "visibility": visibility, "members": sorted(set(members))}


def queue(entity, target):
    work, _ = ManagedDelivery.objects.get_or_create(entity=entity, target=target)
    # An authoritative change immediately supersedes retry of its predecessor.
    if work.applied_revision < entity.revision:
        work.due_at = timezone.now()
        work.save(update_fields=["due_at"])


@transaction.atomic
def observe(scope, config, data, *, canceled=False):
    # Ingest callers serialize observations of this installation. Unique reference
    # and primary room constraints also arbitrate Linear/Plane cross-scope races.
    external_id = data["external_id"]
    other_scope, other_id = alias_scope(scope, data)
    ref = ManagedReference.objects.select_for_update().filter(scope=scope, external_id=external_id).first()
    other = (ManagedReference.objects.select_for_update().filter(scope=other_scope, external_id=other_id).first()
             if other_scope else None)
    if ref and other and ref.entity_id != other.entity_id:
        raise ValueError("Conflicting imported project identities")
    if ref or other:
        entity = ManagedEntity.objects.select_for_update().get(pk=(ref or other).entity_id)
    else:
        entity = ManagedEntity.objects.create(community=scope.community, authority=other_scope or scope,
                                              section="event" if scope.provider == "luma" else "project")
    if other_scope and not other:
        ManagedReference.objects.create(scope=other_scope, external_id=other_id, entity=entity)
    if not ref:
        ref = ManagedReference.objects.create(scope=scope, external_id=external_id, entity=entity)
    ref.snapshot = data
    ref.seen_at = timezone.now()
    ref.canceled = ref.canceled or canceled
    ref.save(update_fields=["snapshot", "seen_at", "canceled"])
    # Explicit, per-project migration cutover. Both references survive and a
    # late Linear callback cannot reclaim authority after this decision.
    if (scope != entity.authority and scope.provider == "plane" and other and
            external_id in config.get("authoritative_project_ids", [])):
        entity.authority = scope
        entity.save(update_fields=["authority"])
    authority_ref = ref
    if scope != entity.authority:
        authority_ref = ManagedReference.objects.select_related("scope").filter(
            entity=entity, scope=entity.authority).first()
        if not authority_ref or not authority_ref.snapshot:
            return entity
        config = scope_config(entity.authority)
        if not config:
            return entity
        scope = entity.authority
        data = authority_ref.snapshot
        external_id = authority_ref.external_id
    desired = dict(data)
    if authority_ref.canceled and desired["status"] != "restricted":
        desired.update(status="canceled", url="")

    terminal = desired["status"] in {"removed", "archived", "ended", "canceled", "completed", "closed"}
    room = ManagedRoom.objects.select_for_update().filter(entity=entity).first()
    policy = None if desired["status"] == "restricted" else eligible_room(config, desired)
    policy_changed = False
    # Historical bootstrap only adopts existing verified rooms unless specifically enabled.
    if not room and policy and (policy["channel_id"] or not terminal or config.get("backfill_historical") is True):
        policy_changed = True
        kwargs = {"channel_id": policy["channel_id"]} if policy["channel_id"] else {}
        existing = (ManagedRoom.objects.select_for_update().select_related("entity__authority").filter(
            community=scope.community, channel_id=policy["channel_id"]).first() if policy["channel_id"] else None)
        if existing:
            explicit = (config.get("bindings") or {}).get(external_id, {})
            if (existing.entity.authority.provider != "curated" or explicit.get("adopt_curated") is not True
                    or existing.visibility != policy["visibility"]):
                raise ValueError("Existing room requires an explicit primary binding")
            entity.revision = max(entity.revision, existing.entity.revision)
            previous = existing.entity
            existing.entity = entity
            existing.save(update_fields=["entity"])
            previous.delete()
            room = existing
        else:
            room = ManagedRoom.objects.create(entity=entity, community=scope.community, **kwargs,
                                              adopted=bool(kwargs), visibility=policy["visibility"], members=policy["members"])
    if room:
        if policy and policy["channel_id"] and policy["channel_id"] != room.channel_id:
            raise ValueError("Explicit binding differs from registered channel")
        # The relay writer must verify access before publishing. If audience is
        # no longer proven, remove provider metadata even on previously public rooms.
        if not policy or policy["visibility"] != room.visibility:
            desired = {"external_id": external_id, "status": "restricted", "title": "", "url": "",
                       "visibility": "unknown", "public_authorized": False}
        elif policy["members"] != room.members:
            room.members = policy["members"]
            room.save(update_fields=["members"])
            policy_changed = True
        if policy and desired["status"] not in {"restricted", "removed"}:
            links = {}
            for alias_ref in ManagedReference.objects.filter(entity=entity).select_related("scope"):
                source, alias_config = alias_ref.snapshot, scope_config(alias_ref.scope)
                if (not source or not alias_config or alias_ref.canceled
                        or source.get("status") in {"restricted", "removed"} or not source.get("url")):
                    continue
                alias_policy = eligible_room(alias_config, source)
                if not alias_policy:
                    continue
                if (alias_policy["visibility"] == "public" or
                    room.visibility == "private" and set(room.members).issubset(alias_policy["members"])):
                    links[alias_ref.scope.provider] = source["url"]
            desired["links"] = links
    if desired != entity.snapshot or policy_changed:
        entity.snapshot = desired
        entity.revision += 1
        entity.save(update_fields=["snapshot", "revision"])
        if room:
            queue(entity, "chat")
            if ManagedReference.objects.filter(entity=entity, scope__provider="linear").exists():
                queue(entity, "backlink")
            if scope.provider == "luma" and config.get("slack_events") is True:
                queue(entity, "slack")
    elif room and not ManagedDelivery.objects.filter(entity=entity, target="chat").exists():
        queue(entity, "chat")
    return entity
