"""One operation-backed notice per accepted goal transition; delivery cannot restart setup."""
from copy import deepcopy
from datetime import timedelta
from uuid import UUID
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from . import website_goal as goals
from .website_models import WebsiteConnectionOperation
from .website_goal_notifications import notice_for
from .website_notification_transport import NotificationTransportError

ACTION="goal-notification"


class NotificationFenced(RuntimeError):
    """Cancelled, superseded or lost-lease notification work cannot publish again."""


def enabled(connection):
    """An independent default-off rollout also requires server goal enrollment."""
    return getattr(settings,"ARTICLES_GOAL_NOTIFICATIONS_ENABLED",False) and goals.enrolled(connection.organization_id)


def track_transition(op,*,now):
    """Record public state entries; claims/polls never manufacture a new entry."""
    if not enabled(op.connection):return None
    goal=goals.mapping(op.receipt.get("goal"))
    if not goals.valid_goal(goal,goals.scope_for(op.connection,op.payload.get("company_id"))):return None
    notice=notice_for(op,now=now)
    previous=goals.mapping(goal.get("notification_observation"))
    base=notice["id"] if notice else None
    entry=previous.get("entry",0)
    if type(entry) is not int or not 0<=entry<=256:return None
    if previous and previous.get("base_id")==base:return None
    if base is not None:
        if entry==256:return None
        entry+=1
    goal["notification_observation"]={"entry":entry,"base_id":base}
    goal["revision"]+=1
    if notice is None:return None
    return {**notice,"base_id":base,"entry":entry,"id":goals.digest({"base_id":base,"entry":entry})}


def reserve_notice(op,notice,*,now):
    """Called in the same transaction as a successful prepare lease compare-and-set."""
    if notice is None:return None
    payload={"version":1,"goal_operation_id":str(op.pk),"goal_id":notice["goal_id"],"owner_id":str(op.payload.get("requested_by_user_id") or ""),
        "scope":deepcopy(op.receipt["goal"]["scope"]),"notice":notice}
    return WebsiteConnectionOperation.objects.get_or_create(idempotency_key="articles-notice:"+notice["id"],defaults={
        "connection":op.connection,"generation":op.generation,"action":ACTION,"state":"pending","payload":payload,
        "receipt":{"channels":{}},"next_attempt_at":now})[0]


def current_goal(outbox,*,now):
    """Validate current website/goal/owner authority before each external stage."""
    connection=outbox.connection
    connection.refresh_from_db()
    if not WebsiteConnectionOperation.objects.filter(pk=outbox.pk,action=ACTION,state="running",attempts=outbox.attempts).exists():raise NotificationFenced()
    scope=goals.mapping(outbox.payload.get("scope"))
    from .website_operations import deletion_epoch
    if (connection.state!="connected" or connection.generation!=outbox.generation
        or str(connection.pk)!=scope.get("connection_id") or str(connection.organization_id)!=scope.get("organization_id")
        or str(connection.repository_id)!=scope.get("repository_id") or connection.branch!=scope.get("branch")
        or connection.app_root!=scope.get("app_root") or deletion_epoch(connection)!=scope.get("deletion_epoch")):raise NotificationFenced()
    op=WebsiteConnectionOperation.objects.filter(pk=outbox.payload.get("goal_operation_id"),connection=connection,generation=outbox.generation,action="prepare").first()
    if not op or str(op.payload.get("requested_by_user_id"))!=outbox.payload.get("owner_id"):raise NotificationFenced()
    latest=WebsiteConnectionOperation.objects.filter(connection=connection,generation=outbox.generation,action="prepare").order_by("-created_at","-pk").first()
    if not latest or latest.pk!=op.pk:raise NotificationFenced()
    goal=goals.mapping(op.receipt.get("goal"));saved=outbox.payload["notice"];observation=goals.mapping(goal.get("notification_observation"))
    if op.state=="running" and observation.get("entry")==saved["entry"] and observation.get("base_id")==saved["base_id"]:
        raise NotificationTransportError("goal_observation_pending")
    current=notice_for(op,now=now)
    if (not current or current["id"]!=saved["base_id"] or goal.get("goal_id")!=outbox.payload.get("goal_id")
        or observation.get("entry")!=saved["entry"] or observation.get("base_id")!=saved["base_id"]):raise NotificationFenced()
    owner=outbox.payload.get("owner_id")
    if not isinstance(owner,str) or not owner.isdigit() or not 0<len(owner)<=20:raise NotificationFenced()
    user=get_user_model().objects.filter(pk=owner,is_active=True).first()
    if not user:raise NotificationFenced()
    from founder_tools.models import VibeRaisingCompany,VibeRaisingProfile
    if not VibeRaisingCompany.objects.filter(pk=scope["company_id"],organization_id=scope["organization_id"],profile__user=user,profile__role=VibeRaisingProfile.ROLE_FOUNDER).exists():raise NotificationFenced()
    return op,user


def checkpoint(outbox,receipt,*,now):
    """Persist before/after sends under the claimed attempt; a competing worker wins."""
    changed=WebsiteConnectionOperation.objects.filter(pk=outbox.pk,action=ACTION,state="running",attempts=outbox.attempts).update(receipt=receipt,updated_at=now)
    if changed!=1:raise NotificationFenced()
    outbox.receipt=deepcopy(receipt)


def deliver_channels(outbox,*,now):
    """Resume each channel independently; accepted channels are never resent."""
    from community_chat.models import CommunityChatDevice,DeviceBindingStatus
    from .models import NotificationChannel,NotificationChannelType,NotificationConsentState
    from . import website_notification_transport as transport
    notice=outbox.payload["notice"];receipt=deepcopy(outbox.receipt);channels=receipt.setdefault("channels",{})
    _,user=current_goal(outbox,now=now)
    def current_devices(owner):
        return list(CommunityChatDevice.objects.filter(user=owner,status=DeviceBindingStatus.VERIFIED,revoked_at__isnull=True,verified_at__isnull=False).order_by("public_key").values_list("public_key",flat=True).distinct()[:9])
    for name in ("chat","email"):
        state=channels.setdefault(name,{"status":"pending"})
        if state["status"] in {"sent","skipped","blocked","uncertain"}:continue
        try:
            _,user=current_goal(outbox,now=timezone.now())
            if name=="chat":
                devices=current_devices(user)
                if not devices:state["status"]="skipped"
                elif len(devices)>8:state.update(status="blocked",outcome="chat_recipient_limit",failure_class="unsupported")
                else:
                    saved=state.get("recipients")
                    if saved is not None and any(key not in devices for key in saved):raise NotificationTransportError("chat_recipient_revoked",retryable=False)
                    recipients=saved or devices
                    if not state.get("channel_id"):
                        state["recipients"]=recipients;checkpoint(outbox,receipt,now=timezone.now())
                        state["channel_id"]=transport.open_dm(recipients,notice["id"])
                    if not state.get("event"):
                        state["event"]=transport.message_event(state["channel_id"],recipients,notice,int(outbox.created_at.timestamp()))
                        checkpoint(outbox,receipt,now=timezone.now())
                    _,user=current_goal(outbox,now=timezone.now())
                    if any(key not in current_devices(user) for key in recipients):raise NotificationTransportError("chat_recipient_revoked",retryable=False)
                    transport.post_event(state["event"])
                    state.update(status="sent",message_id=state["event"]["id"])
            else:
                query=NotificationChannel.objects.filter(organization_id=outbox.connection.organization_id,user=user,channel_type=NotificationChannelType.EMAIL,consent_state=NotificationConsentState.ACTIVE,delivery_enabled=True)
                channel=query.filter(pk=state["channel_id"]).first() if state.get("channel_id") else query.filter(route_id__iexact=user.email).order_by("created_at").first()
                if not channel or channel.route_id.lower()!=user.email.lower():state["status"]="skipped"
                else:
                    started=goals.stamp(state.get("first_attempt_at"))
                    if state.get("first_attempt_at") is not None and (not started or started>now):raise NotificationTransportError("email_attempt_invalid",retryable=False)
                    if started and (now-started).total_seconds()>=23*3600:state.update(status="uncertain",outcome="email_dedup_window_expired",failure_class="unsupported")
                    else:
                        if not state.get("payload"):
                            state.update(channel_id=str(channel.pk),payload=transport.email_payload(channel,notice),first_attempt_at=now.isoformat())
                            checkpoint(outbox,receipt,now=timezone.now())
                        _,user=current_goal(outbox,now=timezone.now())
                        if (not query.filter(pk=channel.pk,route_id=channel.route_id).exists() or state["payload"].get("to")!=[channel.route_id]
                            or channel.route_id.lower()!=user.email.lower()):raise NotificationFenced()
                        message_id=transport.post_email(state["payload"],notice["id"])
                        state.update(status="sent",message_id=message_id)
        except NotificationTransportError as exc:state.update(status="pending" if exc.retryable else "blocked",outcome=exc.outcome,failure_class="transient" if exc.retryable else "repairable")
        checkpoint(outbox,receipt,now=timezone.now())
    return receipt


def advance_notice(identifier,*,now=None):
    """Lease one notice and reconcile transport outcomes outside database locks."""
    now=now or timezone.now()
    with transaction.atomic():
        outbox=WebsiteConnectionOperation.objects.select_for_update().select_related("connection__organization").filter(pk=identifier,action=ACTION,state__in=["pending","running"]).first()
        if not outbox or not enabled(outbox.connection) or outbox.next_attempt_at and outbox.next_attempt_at>now:return None
        outbox.state="running";outbox.attempts+=1;outbox.next_attempt_at=now+timedelta(minutes=2)
        outbox.save(update_fields=["state","attempts","next_attempt_at","updated_at"])
    try:
        receipt=deliver_channels(outbox,now=now)
        pending=any(row.get("status")=="pending" for row in receipt["channels"].values())
        state="pending" if pending else "completed"
        retry=now+timedelta(minutes=min(120,2**min(outbox.attempts,7))) if pending else None
    except NotificationTransportError as exc:
        receipt={**outbox.receipt,"outcome":exc.outcome}
        state,retry=("pending",now+timedelta(minutes=2)) if exc.retryable else ("completed",None)
    except NotificationFenced:
        receipt=outbox.receipt;state,retry="cancelled",None
    WebsiteConnectionOperation.objects.filter(pk=outbox.pk,action=ACTION,state="running",attempts=outbox.attempts).update(state=state,receipt=receipt,next_attempt_at=retry,updated_at=timezone.now())
    return state
