"""A member may share an explicitly addressed composer message with Public Roo."""

import hashlib
import re
import time

import requests
from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import User
from integrations.services.community_bridge.identity import verified_identity_for_buzz
from integrations.services.community_bridge.native_mentions import (
    native_mention_bindings,
    native_mention_tags,
    native_recipient_targets,
)
from integrations.services.slack_channel_mentions import render_slack_mentions
from integrations.services.slack_roo import public_roo_endpoint, public_roo_target

from .authentication import CommunityChatAccountAuthentication
from .privacy import has_ai_consent
from .throttles import CommunityChatScopedThrottle


def command_payload(data, workspace, roo_id, actor, profile):
    """Build an actor-bound command containing only the submitted text and selections."""
    if not isinstance(data, dict) or set(data) != {"text", "tags", "request_id"}:
        raise ValueError("Invalid Roo command.")
    text, tags, request_id = data["text"], data["tags"], data["request_id"]
    if (
        not isinstance(text, str)
        or not 1 <= len(text.strip()) <= 8000
        or "<@" in text
        or not isinstance(tags, list)
        or len(tags) > 40
        or not all(
            isinstance(t, list)
            and len(t) == 3
            and all(isinstance(v, str) for v in t)
            and t[0] in {"slack-mention", "mlai-mention"}
            for t in tags
        )
    ):
        raise ValueError("Use the mention picker to address Roo and select people.")
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[0-9]{13}:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", request_id
    ):
        raise ValueError("Invalid Roo request ID.")
    created = int(request_id[:13]) / 1000
    if not -60 <= time.time() - created <= 900:
        raise ValueError("This Roo request expired. Check its outcome before sending again.")

    rendered, used = render_slack_mentions(text, tags)
    if any(t[1] not in used for t in tags if t[0] == "slack-mention"):
        raise ValueError("Select members with distinct mention labels.")
    if roo_id not in used:
        raise ValueError("Select Public Roo in the mention picker.")
    native = native_mention_tags(text, tags)
    bindings = native_mention_bindings(
        workspace_id=workspace, text=text, tags=native, source_created_at=int(created)
    )
    recipients = native_recipient_targets(
        workspace_id=workspace, text=text, tags=native, bindings=bindings
    )
    slack_people = [t for t in tags if t[0] == "slack-mention" and t[1] in used and t[1] != roo_id]
    # Retain every selected identity or reject the whole command. A stale target
    # must never turn a multi-recipient award into a different, partial award.
    if not all(any(t[2] in r["source_labels"] for r in recipients) for t in native):
        raise ValueError("A selected Chat member is unavailable. Select them again.")
    by_profile = {r["profile_id"]: r for r in recipients}
    if slack_people:
        accounts = {
            u.slack_id: u
            for u in User.objects.filter(is_active=True, slack_id__in={t[1] for t in slack_people})
        }
        for _, slack_id, label in slack_people:
            user = accounts.get(slack_id)
            if not user:
                raise ValueError("A selected Slack member has no linked MLAI account.")
            recipient = by_profile.setdefault(
                str(user.community_chat_profile_id),
                {
                    "kind": "mlai_profile",
                    "profile_id": str(user.community_chat_profile_id),
                    "display_name": user.full_name or "MLAI member",
                    "slack_user_id": slack_id,
                    "source_labels": [],
                },
            )
            if label not in recipient["source_labels"]:
                recipient["source_labels"].append(label)
    labels = [label.casefold() for r in by_profile.values() for label in r["source_labels"]]
    if len(labels) != len(set(labels)) or any(
        t[2].casefold() in labels for t in tags if t[1] == roo_id
    ):
        raise ValueError("Select members with distinct mention labels.")
    if len(by_profile) > 20 or len(labels) > 20:
        raise ValueError("Select up to 20 people per command.")
    source = hashlib.sha256(f"private-roo:{profile}:{request_id}".encode()).hexdigest()
    return {
        "text": rendered,
        "workspace_id": workspace,
        "user_id": actor,
        "source_event_id": source,
        "actor_profile_id": profile,
        "version": 2,
        "posting_mode": "member",
        "recipient_targets": list(by_profile.values()),
        "source_labels": list(
            dict.fromkeys(label for r in by_profile.values() for label in r["source_labels"])
        ),
    }


class RooCommandView(APIView):
    """Execute the caller's own explicit command without receiving a conversation ID."""

    authentication_classes = (CommunityChatAccountAuthentication,)
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def post(self, request):
        target, service = public_roo_target(), public_roo_endpoint("/api/chat-command")
        if not target or not service:
            return Response({"detail": "Public Roo is unavailable."}, status=503)
        workspace, roo_id = target
        profile = str(request.user.community_chat_profile_id)
        actor = verified_identity_for_buzz(
            slack_workspace_id=workspace, buzz_pubkey=request.community_chat_public_key
        )
        if (
            not actor
            or actor["identity_source"] != "mlai_account"
            or actor["user_profile_id"] != profile
            or not isinstance(actor.get("slack_user_id"), str)
            or (request.user.slack_id and request.user.slack_id != actor["slack_user_id"])
            or not re.fullmatch(r"[UW][A-Z0-9]+", actor["slack_user_id"])
            or actor["slack_user_id"] == roo_id
        ):
            return Response({"detail": "Link your Slack identity to use Roo."}, status=403)
        if getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True) and not has_ai_consent(
            request.user.pk
        ):
            return Response({"detail": "Turn on AI sharing to use Roo."}, status=403)
        try:
            payload = command_payload(
                request.data, workspace, roo_id, actor["slack_user_id"], profile
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        url, key = service
        try:
            result = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=(5, 120),
                allow_redirects=False,
            )
            if result.status_code == 409:
                return Response(
                    {
                        "detail": "Roo already received this command. Check its outcome before sending again."
                    },
                    status=409,
                )
            result.raise_for_status()
            body = result.json()
            if (
                result.status_code != 200
                or not isinstance(body, dict)
                or not isinstance(body.get("message"), str)
                or not body["message"]
            ):
                raise ValueError("Missing Roo reply")
        except (requests.RequestException, ValueError):
            return Response(
                {
                    "detail": "Roo's reply could not be confirmed. Check the outcome before repeating an action."
                },
                status=502,
            )
        return Response(
            {"message": body["message"]}, headers={"Cache-Control": "private, no-store"}
        )
