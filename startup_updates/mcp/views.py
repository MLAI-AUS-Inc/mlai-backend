"""Stateless JSON Streamable HTTP and OAuth discovery for Valley MCP."""
import json
import re
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from . import oauth, tools
from .config import PROTOCOL_VERSIONS, SCOPES, availability, mcp_url, public_base

MAX_BODY = 256 * 1024


class McpRateThrottle(AnonRateThrottle):
    rate = "120/minute"


def response(payload=None, status=200):
    value = JsonResponse(payload, status=status) if payload is not None else HttpResponse(status=status)
    value["Cache-Control"] = "no-store"
    value["Referrer-Policy"] = "no-referrer"
    return value


def available_response():
    available, reason = availability()
    return None if available else response({"error": "temporarily_unavailable", "error_description": reason}, 503)


def rpc_error(request_id, code, message, status=200):
    return response({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status)


def bearer_failure(exc, *, insufficient=False):
    value = response({"error": "insufficient_scope" if insufficient else "invalid_token", "error_description": str(exc.detail)}, 403 if insufficient else 401)
    metadata_url = public_base() + "/.well-known/oauth-protected-resource/mcp/valley"
    value["WWW-Authenticate"] = f'Bearer resource_metadata="{metadata_url}", error="{"insufficient_scope" if insufficient else "invalid_token"}"'
    return value


class DomainVerificationView(APIView):
    """Serve one public OpenAI ownership token before MCP access is enabled."""
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (McpRateThrottle,)

    def get(self, request):
        token = getattr(settings, "VALLEY_MCP_DOMAIN_VERIFICATION_TOKEN", "")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
            value = HttpResponse(status=404, content_type="text/plain; charset=utf-8")
        else:
            value = HttpResponse(token, content_type="text/plain; charset=utf-8")
        value["Cache-Control"] = "no-store"
        value["X-Content-Type-Options"] = "nosniff"
        return value


class McpView(APIView):
    """No sessions or SSE state: each authenticated POST yields one JSON result."""
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (McpRateThrottle,)

    def dispatch(self, request, *args, **kwargs):
        # Native/client requests use explicit OAuth Bearer authentication only.
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        unavailable = available_response()
        if unavailable:
            return unavailable
        try:
            self._principal(request)
        except AuthenticationFailed as exc:
            return bearer_failure(exc)
        value = response({"error": "method_not_allowed", "description": "Use POST for stateless JSON Streamable HTTP."}, 405)
        value["Allow"] = "POST"
        return value

    def delete(self, request):
        return self.get(request)

    @staticmethod
    def _principal(request):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") not in getattr(settings, "VALLEY_MCP_ALLOWED_ORIGINS", []):
            raise PermissionDenied("This origin is not permitted to access Valley MCP.")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthenticationFailed("Connect this agent to Valley first.")
        return oauth.authenticate_token(header[7:].strip())

    def post(self, request):
        unavailable = available_response()
        if unavailable:
            return unavailable
        try:
            principal = self._principal(request)
        except AuthenticationFailed as exc:
            return bearer_failure(exc)
        except PermissionDenied as exc:
            return bearer_failure(exc, insufficient=True)
        if len(request.body) > MAX_BODY:
            return rpc_error(None, -32600, "Request exceeds the maximum size.", 413)
        if request.content_type != "application/json":
            return rpc_error(None, -32600, "Use application/json.", 415)
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return rpc_error(None, -32600, "Accept application/json and text/event-stream.", 406)
        version = request.headers.get("MCP-Protocol-Version")
        if version and version not in PROTOCOL_VERSIONS:
            return rpc_error(None, -32600, "Unsupported MCP protocol version.", 400)
        try:
            payload = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return rpc_error(None, -32700, "Invalid JSON.", 400)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return rpc_error(None, -32600, "Use a JSON-RPC 2.0 request.", 400)
        request_id = payload.get("id")
        if "id" in payload and (isinstance(request_id, bool) or not isinstance(request_id, (int, str))):
            return rpc_error(None, -32600, "Use an integer or string request ID.", 400)
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(request_id, -32602, "Parameters must be an object.")
        method = payload["method"]
        if "id" not in payload:
            if method.startswith("notifications/"):
                return response(status=202)
            return rpc_error(None, -32600, "Requests need an ID.", 400)
        if method == "initialize":
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                return rpc_error(request_id, -32602, "Provide protocolVersion, capabilities and clientInfo.")
            requested = params.get("protocolVersion")
            result = {"protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "Valley", "version": "1.0.0"},
                "instructions": "Prepare monthly updates from the user's connected sources. Save private narrative drafts with source references. Financial evidence and founder publishing remain in MLAI Chat."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            allowed = [item for item in tools.TOOLS if ("startup:draft:write" if item["name"] == "save_narrative_draft" else "startup:brief:read") in principal.grant["scopes"]]
            result = {"tools": allowed}
        elif method == "tools/call":
            try:
                value = tools.call_tool(principal, params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value, "isError": False}
            except AuthenticationFailed as exc:
                return bearer_failure(exc)
            except PermissionDenied as exc:
                return bearer_failure(exc, insufficient=True)
            except APIException as exc:
                result = {"content": [{"type": "text", "text": json.dumps({"error": exc.default_code, "detail": exc.detail})}], "isError": True}
        else:
            return rpc_error(request_id, -32601, "Method not found.")
        return response({"jsonrpc": "2.0", "id": request_id, "result": result})


class PublicOAuthView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (McpRateThrottle,)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        available, reason = availability()
        if not available:
            from rest_framework.exceptions import NotFound
            raise NotFound(reason)


class ResourceMetadataView(PublicOAuthView):
    def get(self, request):
        return response({"resource": mcp_url(), "authorization_servers": [public_base()],
            "scopes_supported": sorted(SCOPES), "bearer_methods_supported": ["header"], "resource_name": "Valley monthly updates"})


class AuthorizationMetadataView(PublicOAuthView):
    def get(self, request):
        return response(oauth.metadata())


class RegisterView(PublicOAuthView):
    def post(self, request):
        try:
            return response(oauth.register_client(request.data), 201)
        except oauth.OAuthError as exc:
            return response({"error": exc.error, "error_description": exc.description}, 400)


class AuthorizeView(PublicOAuthView):
    def get(self, request):
        try:
            intent = oauth.create_intent(request.query_params)
        except oauth.OAuthError as exc:
            return response({"error": exc.error, "error_description": exc.description}, 400)
        value = redirect(settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/") + "/my-startup/connections?" + urlencode({"mcpAuthorization": intent["requestId"]}))
        value["Cache-Control"] = "no-store"
        value["Referrer-Policy"] = "no-referrer"
        return value


class TokenView(PublicOAuthView):
    def post(self, request):
        try:
            return response(oauth.token_exchange(request.data))
        except oauth.OAuthError as exc:
            return response({"error": exc.error, "error_description": exc.description}, 400)
        except APIException:
            return response({"error": "invalid_grant", "error_description": "Authorisation expired or was revoked."}, 400)


class RevokeView(PublicOAuthView):
    def post(self, request):
        if not isinstance(request.data, dict) and not hasattr(request.data, "get"):
            return response({"error": "invalid_request", "error_description": "Revocation requests must contain named fields."}, 400)
        client_id = str(request.data.get("client_id") or "")
        token = str(request.data.get("token") or "")
        record = oauth.cache.get(oauth.key("refresh" if token.startswith("valley_refresh_") else "access", token))
        if record:
            grant = oauth.cache.get(oauth.key("grant", record["grant_id"]))
            if grant and grant["client_id"] == client_id:
                oauth.revoke_grant(grant["id"])
        return response(status=200)
