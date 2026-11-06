"""Content-free owner delivery diagnostics derived from existing durable rows."""

from django.conf import settings

from .models import AgentAuditEvent, AgentInvocation, AgentSubscription


def delivery_payload(installation, grant, *, now, authorized=None):
    """Keep queue state additive so shipped mention pickers keep working."""
    pending = AgentInvocation.objects.filter(installation=installation, grant=grant,
        generation=grant.generation, status="pending", expires_at__gt=now, reply__isnull=True
    ).order_by("created_at", "pk") if grant else None
    oldest = pending.first() if pending is not None else None
    cursor = installation.native_cursor
    problem = cursor.get("last_error")
    if not problem and cursor.get("error_code"):
        problem = {"code": cursor["error_code"], "at": None}
    if not problem and cursor.get("gap"):
        problem = {"code": cursor["gap"].get("reason", "native_cursor_reset_required"), "at": cursor["gap"].get("at")}
    delivery = {"pending_count": pending.count() if pending is not None else 0,
        "oldest_pending_at": oldest.created_at.isoformat() if oldest else None,
        "needs": cursor.get("needs"), "problem": problem,
        "grant_expires_at": grant.expires_at.isoformat() if grant else None}
    rows = list(AgentInvocation.objects.filter(installation=installation).select_related("reply")
                .order_by("-created_at", "-pk")[:20])
    identifiers = [str(row.pk) for row in rows]
    # The grant FK is indexed; never scan the global audit stream for an owner.
    audits = AgentAuditEvent.objects.filter(grant_id__in={row.grant_id for row in rows},
        event_type__in=("native.invocation.sent_to_dot", "native.invocation.fetched"),
        metadata__invocation_id__in=identifiers).order_by("created_at", "pk") if rows else []
    evidence = {}
    for audit in audits:
        identifier = audit.metadata.get("invocation_id")
        state = "delivered" if audit.event_type == "native.invocation.fetched" else "sent_to_dot"
        previous = evidence.get(identifier)
        if previous is None or state == "delivered" or previous[0] != "delivered":
            evidence[identifier] = (state, audit.created_at)
    recent = []
    for row in rows:
        reply = getattr(row, "reply", None)
        state, updated, reason = "queued", row.created_at, None
        if (reply is not None and reply.status == "sent") or row.status == "replied":
            state, updated = "replied", reply.updated_at if reply else row.created_at
        elif reply is not None and reply.status in {"failed", "stopped_unknown", "cancelled"}:
            state, updated, reason = "failed", reply.updated_at, reply.error_code or reply.status
        elif row.status == "cancelled":
            state, reason = "failed", "invocation_cancelled"
        elif row.expires_at <= now or row.status == "expired":
            state, updated, reason = "expired", row.expires_at, "invocation_expired"
        elif str(row.pk) in evidence:
            state, updated = evidence[str(row.pk)]
        recent.append({"source_event_id": row.source_event_id, "state": state,
                       "updated_at": updated.isoformat(), "reason": reason})
    delivery.update(listening_payload(installation, grant, now=now, authorized=authorized))
    return {"delivery": delivery, "recent_invocations": recent}


def listening_payload(installation, grant, *, now, authorized=None):
    """Report held and stopped watches without changing mentionable statuses."""
    from .event_webhooks import lease_live
    rows = list(AgentSubscription.objects.filter(grant__installation=installation,
        event_types=["agent.mentioned"], delivery__mode="webhook").order_by("-updated_at", "-pk"))
    current = [row for row in rows if grant and row.grant_id == grant.pk and row.generation == grant.generation]
    live = next((row for row in current if row.is_active and lease_live(row.delivery, now)), None)
    stopped = next((row for row in rows if not row.is_active), None)
    metadata = live.delivery if live else {}
    listening = bool(live and not metadata.get("held_reason") and authorized is not False
                     and installation.state == "active" and grant.expires_at > now)
    result = {"listening": listening, "held_reason": metadata.get("held_reason"),
        "lease_expires_at": ("unbounded" if metadata.get("lease") == "unbounded" else metadata.get("expires_at")),
        "last_delivered_at": metadata.get("last_delivered_at") or next(
            (row.delivery["last_delivered_at"] for row in rows if row.delivery.get("last_delivered_at")), None),
        "stopped_at": stopped.delivery.get("stopped_at") if stopped else None,
        "stop_reason": stopped.delivery.get("stop_reason") if stopped else None}
    if getattr(settings, "AGENT_RUNTIME_NATIVE_DURABLE_WATCH", False):
        latest = grant or installation.grants.filter(revoked_at__isnull=True).order_by("-created_at").first()
        result["needs"] = ("resume" if installation.state == "paused" else
            "renew" if latest and latest.expires_at <= now else
            "reconnect" if not latest or authorized is False else
            "reconnect" if metadata.get("held_reason") in {"awaiting_activation", "unified_authority_changed"} else
            "subscribe" if not live else None)
    return result
