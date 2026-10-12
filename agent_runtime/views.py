"""Member consent/control APIs and separately authenticated worker ingress."""
import base64
import binascii
import hashlib
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, RequestDataTooBig
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle

from .authentication import (AgentAuthentication, OwnerAuthentication,
                             require_enabled, require_owner, require_service_key)
from .models import AgentInstallation, AgentGrant, AgentTask, AgentActionProposal, AgentArtifact, AgentResponsibility, AgentSubscription
from .policy import AgentPermissionDenied, authorize, validate_grant_base, normalize_limits, instruction_text
from .runtime import AgentRuntimeError
from . import oauth, runtime, sources, serializers as schema


class RuntimeView(APIView):
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "agent_runtime"

    def initial(self, request, *args, **kwargs):
        require_enabled()
        if len(request.body) > 15 * 1024 * 1024:
            raise ValidationError("Request is too large.")
        super().initial(request, *args, **kwargs)

    def handle_exception(self, exc):
        if isinstance(exc, AgentPermissionDenied):
            exc = PermissionDenied(exc.code)
        elif isinstance(exc, AgentRuntimeError):
            exc = ValidationError({"error": exc.code})
        elif isinstance(exc, ObjectDoesNotExist):
            exc = NotFound()
        elif isinstance(exc, RequestDataTooBig):
            exc = ValidationError("Request exceeds the configured upload limit.")
        return super().handle_exception(exc)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "no-referrer"
        return response


class OwnerView(RuntimeView):
    authentication_classes = (OwnerAuthentication,)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_owner(request)


class AgentView(RuntimeView):
    authentication_classes = (AgentAuthentication,)


class WorkerView(RuntimeView):
    authentication_classes = ()
    permission_classes = ()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_service_key(request)


def task_payload(task):
    return {"task_id": str(task.pk), "status": task.status, "instruction": task.instruction,
            "resource": task.resource, "limits": task.limits, "inputs": task.inputs,
            "created_at": task.created_at.isoformat(), "result": {k: v for k, v in task.result.items() if not k.startswith("_")} if isinstance(task.result, dict) else {},
            "actions": [{"id": str(a.pk), "status": a.status} for a in task.actions.all()],
            "artifacts": [{"id": str(a.pk), "name": a.name, "size_bytes": a.size_bytes} for a in task.artifacts.all()],
            "error_code": task.error_code}


def grant_payload(grant):
    return {"grant_id": str(grant.pk), "agent_id": str(grant.installation_id),
            "name": grant.installation.name, "runtime": grant.installation.runtime,
            "scopes": grant.scopes, "resources": grant.resources,
            "expires_at": grant.expires_at.isoformat(),
            "revoked": grant.revoked_at is not None}


class ConnectedAgentsView(OwnerView):
    def get(self, request):
        grants = AgentGrant.objects.select_related("installation").filter(
            installation__owner=request.user).order_by("-created_at")[:100]
        active_installations = list(AgentInstallation.objects.filter(owner=request.user, is_active=True)
            .values_list("pk", flat=True))
        if request.query_params.get("format") == "json":
            return Response({"agents": [grant_payload(g) for g in grants],
                "active_installations": [str(value) for value in active_installations]})
        return render(request, "agent_runtime/connected.html", {"agents": list(grants),
            "active_installations": [str(value) for value in active_installations],
            "actions_enabled": getattr(settings, "AGENT_RUNTIME_ACTIONS_ENABLED", False),
            "public_web_enabled": getattr(settings, "AGENT_RUNTIME_PUBLIC_WEB_ENABLED", False)})

    def post(self, request):
        consent = schema.validated(schema.ConsentSerializer, request.data)
        grant = oauth.create_grant(request.user, **consent)
        return Response(grant_payload(grant), status=201)


class RevokeGrantView(OwnerView):
    def post(self, request, grant_id):
        get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        grant = runtime.revoke_grant(grant_id, owner=request.user)
        return Response(grant_payload(grant))


class RevokeInstallationView(OwnerView):
    def post(self, request, installation_id):
        get_object_or_404(AgentInstallation, pk=installation_id, owner=request.user)
        runtime.revoke_installation(installation_id, owner=request.user)
        return Response({"agent_id": str(installation_id), "is_active": False})


class AuthorizeView(OwnerView):
    def handle_exception(self, exc):
        response = super().handle_exception(exc)
        if self.request.method == "GET" and response.status_code in {401, 403}:
            from urllib.parse import urlencode
            fields = {"client_id", "redirect_uri", "response_type", "scope", "resource", "state", "code_challenge", "code_challenge_method"}
            query = urlencode({k: self.request.query_params[k] for k in fields if k in self.request.query_params})
            sign_in_url = settings.DEFAULT_FRONTEND_URL.rstrip("/") + "/platform/agents?" + query
            return render(self.request, "agent_runtime/signin.html", {"sign_in_url": sign_in_url}, status=response.status_code)
        return response

    def get(self, request):
        authorization = oauth.validate_authorization(request.query_params)
        client = oauth.configured_client(authorization["client_id"])
        if client.get("app_id") == "openai-dot":
            from .native_installations import DOT_SCOPES
            if set(authorization["scopes"]) != set(DOT_SCOPES):
                raise PermissionDenied("Dot requires the exact native mention permissions.")
            installations = AgentInstallation.objects.filter(owner=request.user, app_id="openai-dot",
                is_active=True, state="active").order_by("created_at")[:100]
            choices = []
            for installation in installations:
                grant = installation.grants.filter(revoked_at__isnull=True, expires_at__gt=timezone.now()).first()
                if grant and len(grant.resources) == 1:
                    choices.append({"installation_id": str(installation.pk), "name": installation.name,
                                    "resource": grant.resources[0], "expires_at": grant.expires_at.isoformat()})
            return render(request, "agent_runtime/native_consent.html", {"authorization": authorization,
                "oauth_params": dict(request.query_params.items()), "installations": choices})
        return render(request, "agent_runtime/consent.html", {"authorization": authorization,
            "oauth_params": dict(request.query_params.items())})

    def post(self, request):
        if set(request.data) != {"authorization", "consent"}:
            raise ValidationError("Expected authorization and consent.")
        authorization = oauth.validate_authorization(request.data["authorization"])
        consent = schema.validated(schema.ConsentSerializer, request.data["consent"])
        redirect = oauth.authorize_client(request.user, authorization, consent)
        response = Response({"redirect_uri": redirect})
        response["Cache-Control"] = "no-store"
        return response


class TokenView(RuntimeView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        data = schema.validated(schema.TokenSerializer, request.data)
        if data["grant_type"] == "authorization_code":
            payload = oauth.exchange_code(data)
        elif data["grant_type"] == "refresh_token":
            payload = oauth.rotate_refresh(data)
        else:
            raise ValidationError({"error": "unsupported_grant_type"})
        response = Response(payload)
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


class OAuthMetadataView(RuntimeView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request):
        issuer = settings.AGENT_RUNTIME_ISSUER.rstrip("/")
        prefix = issuer + "/api/v1/agent-runtime"
        return Response({"issuer": issuer, "authorization_endpoint": prefix + "/oauth/authorize/",
            "token_endpoint": prefix + "/oauth/token/", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": sorted(oauth.permitted_scopes())})


class ResourceMetadataView(RuntimeView):
    authentication_classes = ()
    permission_classes = ()

    def get(self, request):
        return Response({"resource": settings.AGENT_RUNTIME_RESOURCE,
                         "authorization_servers": [settings.AGENT_RUNTIME_ISSUER.rstrip("/")],
                         "scopes_supported": sorted(oauth.permitted_scopes()),
                         "bearer_methods_supported": ["header"]})


class ToolsView(AgentView):
    def post(self, request):
        from .broker import execute
        data = schema.validated(schema.ToolSerializer, request.data)
        return Response(execute(request.agent_principal, data["tool"], data["arguments"]))


class TasksView(AgentView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.agent_principal.credential.kind != "access":
            raise PermissionDenied("Task capabilities cannot create or enumerate other tasks.")

    def get(self, request):
        from .broker import require_scope
        principal = request.agent_principal
        require_scope(principal, "tasks:read")
        tasks = list(AgentTask.objects.filter(grant=principal.grant).order_by("-created_at")[:50])
        for task in tasks:
            validate_grant_base(principal.grant, expected_generation=task.generation)
            if task.resource:
                require_scope(principal, "messages:read", task.resource)
        return Response({"tasks": [task_payload(t) for t in tasks]})

    def post(self, request):
        from .broker import require_scope
        principal = request.agent_principal
        require_scope(principal, "tasks:write")
        data = schema.validated(schema.TaskCreateSerializer, request.data)
        task = runtime.enqueue_task(principal.grant, access_checker=sources.live_access, **data)
        return Response(task_payload(task), status=201)


class OwnerTasksView(OwnerView):
    def get(self, request, grant_id):
        grant = get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        validate_grant_base(grant)
        tasks = list(AgentTask.objects.filter(grant=grant).order_by("-created_at")[:50])
        for task in tasks:
            validate_grant_base(grant, expected_generation=task.generation)
            if task.resource:
                authorize(grant, "messages:read", task.resource, access_checker=sources.live_access)
        return Response({"tasks": [task_payload(t) for t in tasks]})

    def post(self, request, grant_id):
        grant = get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        data = schema.validated(schema.TaskCreateSerializer, request.data)
        task = runtime.enqueue_task(grant, access_checker=sources.live_access, **data)
        return Response(task_payload(task), status=201)


class CancelTaskView(OwnerView):
    def post(self, request, task_id):
        get_object_or_404(AgentTask, pk=task_id, grant__installation__owner=request.user)
        task = runtime.cancel_task(task_id, owner=request.user)
        # Stopping work remains possible after source access is removed. It
        # must not also become an alternate path to read the previous result.
        return Response({"task_id": str(task.pk), "status": task.status})


class ClaimTaskView(WorkerView):
    def post(self, request):
        data = schema.validated(schema.ClaimSerializer, request.data)
        task = runtime.claim_task(access_checker=sources.live_access, **data)
        if task is None:
            return Response({"task": None})
        # A lease can expire/revoke between claim and credential creation. The
        # second locked validation prevents minting fresh authority afterwards.
        with transaction.atomic():
            task = runtime._task_with_grant_lock(task.pk)
            runtime.require_lease(task, task.lease_token, task.lease_epoch)
            runtime._authorize_task(task, access_checker=sources.live_access)
            tools, _ = oauth.issue_credential(task.grant, kind="task_tool", audience=settings.AGENT_RUNTIME_RESOURCE,
                scopes=task.grant.scopes, expires_at=task.deadline_at, task=task)
            model, _ = oauth.issue_credential(task.grant, kind="task_model", audience="agent-model",
                scopes=task.grant.scopes, expires_at=task.deadline_at, task=task)
            result = {**task_payload(task), "grant_id": str(task.grant_id),
                      "lease_token": str(task.lease_token), "lease_epoch": task.lease_epoch,
                      "lease_expires_at": task.lease_expires_at.isoformat(),
                      "tool_token": tools, "model_token": model,
                      "capabilities": {"public_web": bool(getattr(settings, "AGENT_RUNTIME_PUBLIC_WEB_ENABLED", False)
                          and "web:access" in task.grant.scopes)},
                      "model": getattr(settings, "AGENT_RUNTIME_MODEL_NAME", "")}
        response = Response({"task": result})
        response["Cache-Control"] = "no-store"
        return response


class HeartbeatTaskView(WorkerView):
    def post(self, request, task_id):
        data = schema.validated(schema.LeaseSerializer, request.data)
        task = runtime.heartbeat_task(task_id, access_checker=sources.live_access, **data)
        return Response({"lease_expires_at": task.lease_expires_at.isoformat(), "status": task.status})


class CompleteTaskView(WorkerView):
    def post(self, request, task_id):
        payload = dict(request.data)
        artifacts = payload.pop("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 10:
            raise ValidationError("Invalid artifacts.")
        data = schema.validated(schema.CompleteSerializer, payload)
        if not isinstance(data["result"], dict) or any(str(k).startswith("_") for k in data["result"]):
            raise ValidationError("Result must be an object without reserved fields.")
        declarations = []
        for artifact in artifacts:
            content = decode_artifact(artifact)
            declarations.append({"name": artifact["name"], "sha256": artifact["sha256"],
                                 "size_bytes": len(content), "media_type": artifact.get("media_type", "application/octet-stream")})
        if len({item["name"] for item in declarations}) != len(declarations):
            raise ValidationError("Artifact names must be unique within a completion.")
        data["result"] = {**data["result"], "_artifacts": declarations}
        stored = []
        try:
            with transaction.atomic():
                task = runtime._task_with_grant_lock(task_id)
                if task.status != "completed":
                    runtime.require_lease(task, data["lease_token"], data["lease_epoch"])
                    for artifact in artifacts:
                        stored.append(store_artifact(task_id, lease_token=data["lease_token"], lease_epoch=data["lease_epoch"], artifact=artifact))
                task = runtime.complete_task(task_id, access_checker=sources.live_access, **data)
        except Exception:
            root = Path(getattr(settings, "AGENT_RUNTIME_ARTIFACT_ROOT", "")).resolve()
            for row in stored:
                target = (root / row.storage_key).resolve()
                if target.is_relative_to(root):
                    target.unlink(missing_ok=True)
            raise
        return Response(task_payload(task))


class FailTaskView(WorkerView):
    def post(self, request, task_id):
        data = schema.validated(schema.FailSerializer, request.data)
        return Response(task_payload(runtime.fail_task(task_id, **data)))


def decode_artifact(artifact):
    if not isinstance(artifact, dict) or set(artifact) - {"name", "sha256", "content_base64", "media_type"}:
        raise ValidationError("Invalid artifact.")
    raw = artifact.get("content_base64", "")
    if not isinstance(raw, str) or len(raw) > 14 * 1024 * 1024:
        raise ValidationError("Artifact is too large.")
    try:
        content = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValidationError("Invalid artifact content.") from exc
    digest = hashlib.sha256(content).hexdigest()
    if len(content) > 10 * 1024 * 1024 or digest != artifact.get("sha256"):
        raise ValidationError("Artifact size or digest mismatch.")
    if not isinstance(artifact.get("name"), str):
        raise ValidationError("Artifact name is required.")
    return content


def store_artifact(task_id, *, lease_token, lease_epoch, artifact):
    content = decode_artifact(artifact)
    digest = hashlib.sha256(content).hexdigest()
    configured_root = getattr(settings, "AGENT_RUNTIME_ARTIFACT_ROOT", "")
    if not configured_root or not Path(configured_root).is_absolute():
        raise PermissionDenied("Private artifact storage has not been configured.")
    with transaction.atomic():
        task = runtime._task_with_grant_lock(task_id)
        runtime.require_lease(task, lease_token, lease_epoch)
        if task.resource:
            authorize(task.grant, "messages:read", task.resource, access_checker=sources.live_access)
        row = runtime.record_artifact(task_id, lease_token=lease_token, lease_epoch=lease_epoch,
            name=artifact.get("name", ""), media_type=artifact.get("media_type", "application/octet-stream"),
            sha256=digest, size_bytes=len(content))
        root = Path(configured_root).resolve()
        target = root / row.storage_key
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.resolve().is_relative_to(root):
            raise PermissionDenied("Invalid artifact storage path.")
        try:
            with target.open("xb") as output:
                output.write(content)
            target.chmod(0o600)
        except OSError as exc:
            raise ValidationError("Artifact could not be stored.") from exc
    return row


class ArtifactView(OwnerView):
    def get(self, request, artifact_id):
        row = get_object_or_404(AgentArtifact.objects.select_related("task__grant__installation__owner"),
                               pk=artifact_id, task__grant__installation__owner=request.user)
        validate_grant_base(row.task.grant, expected_generation=row.task.generation)
        if row.expires_at and row.expires_at <= timezone.now():
            raise NotFound()
        if row.task.resource:
            authorize(row.task.grant, "messages:read", row.task.resource, access_checker=sources.live_access)
        configured_root = getattr(settings, "AGENT_RUNTIME_ARTIFACT_ROOT", "")
        if not configured_root:
            raise NotFound()
        root = Path(configured_root).resolve()
        target = (root / row.storage_key).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise NotFound()
        response = FileResponse(target.open("rb"), as_attachment=True, filename=row.name,
                                content_type="application/octet-stream")
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response["Cache-Control"] = "no-store"
        return response


class ActionApprovalView(OwnerView):
    def get(self, request, action_id):
        action = get_object_or_404(AgentActionProposal, pk=action_id, task__grant__installation__owner=request.user)
        validate_grant_base(action.task.grant, expected_generation=action.generation)
        authorize(action.task.grant, "messages:read", action.resource, access_checker=sources.live_access)
        if request.query_params.get("format") != "json":
            return render(request, "agent_runtime/action.html", {"action": action})
        return Response({"proposal_id": str(action.pk), "kind": action.kind, "resource": action.resource,
                         "payload": action.payload, "payload_hash": action.payload_hash, "status": action.status})

    def post(self, request, action_id):
        from .actions import approve_action, reject_action
        get_object_or_404(AgentActionProposal, pk=action_id, task__grant__installation__owner=request.user)
        if request.data.get("decision") == "reject":
            action = reject_action(action_id, owner=request.user)
        elif request.data.get("decision") == "approve" and set(request.data) == {"decision", "expected_hash"}:
            action = approve_action(action_id, owner=request.user, expected_hash=request.data["expected_hash"],
                                    access_checker=sources.live_access)
        else:
            raise ValidationError("Choose approve with the displayed exact hash, or reject.")
        return Response({"proposal_id": str(action.pk), "status": action.status})


class IngestEventView(RuntimeView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        from .events import ingest_event
        from django.utils.dateparse import parse_datetime
        require_service_key(request, setting="AGENT_RUNTIME_EVENT_KEY")
        data = dict(request.data)
        if set(data) - {"source", "source_event_id", "resource", "event_type", "occurred_at", "payload", "actor_kind", "chain_id", "chain_depth"} or data.get("source") != "slack":
            raise ValidationError("Unsupported event fields.")
        resource = str(data.get("resource", ""))
        workspace, channel = sources.parse_resource(resource)
        if not str(data.get("source_event_id", "")).startswith(f"slack:{workspace}:"):
            raise ValidationError("Source event identity must match the workspace.")
        if workspace not in set(getattr(settings, "AGENT_RUNTIME_SLACK_WORKSPACES", [])):
            raise PermissionDenied("Workspace is outside the pilot.")
        if channel not in set(getattr(settings, "AGENT_RUNTIME_SLACK_CHANNEL_IDS", [])):
            raise PermissionDenied("Channel is outside the pilot.")
        metadata = data.get("payload", {})
        allowed_metadata = {"team_id", "channel_id", "message_ts", "thread_ts", "source_author_id", "subtype", "source_client_msg_id"}
        if (not isinstance(metadata, dict) or set(metadata) - allowed_metadata
                or any(not isinstance(v, str) or len(v) > 200 for v in metadata.values())
                or metadata.get("team_id", workspace) != workspace
                or metadata.get("channel_id", channel) != channel):
            raise ValidationError("Only matching message identifiers may be ingested.")
        occurred = parse_datetime(str(data.get("occurred_at", "")))
        if occurred is None or timezone.is_naive(occurred):
            raise ValidationError("occurred_at must be an aware timestamp.")
        actor_kind = data.get("actor_kind", "human")
        action_rows = AgentActionProposal.objects.filter(resource=resource)
        message_ts = metadata.get("message_ts", "")
        if message_ts and action_rows.filter(status="sent", receipt__workspace_id=workspace,
                receipt__channel_id=channel, receipt__message_ts=message_ts).exists():
            actor_kind = "agent"
        client_message_id = metadata.get("source_client_msg_id")
        if client_message_id:
            import uuid
            try:
                action_id = uuid.UUID(client_message_id)
            except (ValueError, TypeError):
                action_id = None
            if action_id and action_rows.filter(pk=action_id, status__in=("sending", "sent", "unknown")).exists():
                actor_kind = "agent"
        event = ingest_event(source="slack", source_event_id=str(data.get("source_event_id", "")),
            resource=resource, event_type=data.get("event_type"), payload=metadata,
            actor_kind=actor_kind, chain_id=data.get("chain_id"),
            chain_depth=data.get("chain_depth", 0), occurred_at=occurred)
        return Response({"event_id": str(event.pk)}, status=202)


class ResponsibilitiesView(OwnerView):
    def get(self, request, grant_id):
        grant = get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        rows = AgentResponsibility.objects.filter(grant=grant).order_by("-created_at")[:100]
        return Response({"responsibilities": [{"id": str(r.pk), "name": r.name, "instruction": r.instruction,
            "resource": r.resource, "is_active": r.is_active, "next_run_at": r.next_run_at,
            "interval_seconds": r.interval_seconds, "timezone_name": r.timezone_name,
            "quiet_start": r.quiet_start, "quiet_end": r.quiet_end} for r in rows]})

    def post(self, request, grant_id):
        from .policy import after_quiet_hours
        get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        data = schema.validated(schema.ResponsibilitySerializer, request.data)
        with transaction.atomic():
            grant = runtime._grant(grant_id)
            authorize(grant, "responsibilities:write")
            authorize(grant, "tasks:write")
            authorize(grant, "messages:read", data["resource"], access_checker=sources.live_access)
            if data["next_run_at"] >= grant.expires_at:
                raise ValidationError("Schedule begins after the grant expires.")
            data["instruction"] = instruction_text(data["instruction"])
            data["limits"] = normalize_limits(data["limits"], max_seconds=grant.max_task_seconds)
            data["next_run_at"] = after_quiet_hours(max(timezone.now(), data["next_run_at"]),
                timezone_name=data["timezone_name"], quiet_start=data["quiet_start"], quiet_end=data["quiet_end"])
            row = AgentResponsibility.objects.create(grant=grant, generation=grant.generation, **data)
            runtime.audit("responsibility.created", grant=grant, responsibility_id=str(row.pk))
        return Response({"id": str(row.pk), "next_run_at": row.next_run_at}, status=201)


class StopResponsibilityView(OwnerView):
    def post(self, request, responsibility_id):
        identity = get_object_or_404(AgentResponsibility, pk=responsibility_id, grant__installation__owner=request.user)
        with transaction.atomic():
            grant = runtime._grant(identity.grant_id)
            AgentResponsibility.objects.filter(pk=responsibility_id, grant=grant).update(is_active=False)
            runtime.audit("responsibility.stopped", grant=grant, responsibility_id=str(responsibility_id))
        return Response({"id": str(responsibility_id), "is_active": False})


class SubscriptionsView(OwnerView):
    def get(self, request, grant_id):
        grant = get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        rows = AgentSubscription.objects.filter(grant=grant).order_by("-created_at")[:100]
        return Response({"subscriptions": [{"id": str(r.pk), "resource": r.resource,
            "event_types": r.event_types, "instruction": r.instruction, "is_active": r.is_active} for r in rows]})

    def post(self, request, grant_id):
        get_object_or_404(AgentGrant, pk=grant_id, installation__owner=request.user)
        data = schema.validated(schema.SubscriptionSerializer, request.data)
        with transaction.atomic():
            grant = runtime._grant(grant_id)
            authorize(grant, "events:read", data["resource"], access_checker=sources.live_access)
            authorize(grant, "tasks:write")
            authorize(grant, "messages:read", data["resource"], access_checker=sources.live_access)
            data["instruction"] = instruction_text(data["instruction"])
            data["limits"] = normalize_limits(data["limits"], max_seconds=grant.max_task_seconds)
            from .events import lock_event_resource
            from .models import AgentEvent
            lock_event_resource(data["resource"])
            cursor = AgentEvent.objects.filter(resource=data["resource"]).order_by("-pk").values_list("pk", flat=True).first() or 0
            row = AgentSubscription.objects.create(grant=grant, generation=grant.generation, cursor=cursor, **data)
            runtime.audit("subscription.created", grant=grant, subscription_id=str(row.pk))
        return Response({"id": str(row.pk)}, status=201)


class StopSubscriptionView(OwnerView):
    def post(self, request, subscription_id):
        identity = get_object_or_404(AgentSubscription, pk=subscription_id, grant__installation__owner=request.user)
        with transaction.atomic():
            grant = runtime._grant(identity.grant_id)
            AgentSubscription.objects.filter(pk=subscription_id, grant=grant).update(is_active=False)
            runtime.audit("subscription.stopped", grant=grant, subscription_id=str(subscription_id))
        return Response({"id": str(subscription_id), "is_active": False})


class EligibleChannelsView(OwnerView):
    def get(self, request):
        if request.query_params.get("community"):
            from .native_sources import eligible_channels
            return Response({"channels": eligible_channels(request.user, request.query_params["community"]), "unavailable_count": 0})
        channels = []
        unavailable = 0
        resources = [f"slack:{workspace}:{channel}" for workspace in settings.AGENT_RUNTIME_SLACK_WORKSPACES
                     for channel in settings.AGENT_RUNTIME_SLACK_CHANNEL_IDS]
        for resource in resources[:100]:
            try:
                channels.append(sources.channel_summary(request.user, resource))
            except PermissionDenied:
                continue
            except (sources.SourceUnavailable, APIException):
                unavailable += 1
        return Response({"channels": channels, "unavailable_count": unavailable})


class SlackAdmissionView(RuntimeView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        from .admission import admit_slack_command
        require_service_key(request, setting="AGENT_RUNTIME_ADMISSION_KEY")
        data = schema.validated(schema.SlackAdmissionSerializer, request.data)
        task, created = admit_slack_command(**data)
        return Response({"task_id": str(task.pk), "status": task.status, "new": created,
                         "owner_url": settings.AGENT_RUNTIME_ISSUER.rstrip("/") + "/api/v1/agent-runtime/connected/"}, status=202)
