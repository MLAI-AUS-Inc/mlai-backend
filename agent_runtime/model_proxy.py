"""Task-scoped, budgeted OpenAI-compatible inference; provider keys stay here.

The upstream response is buffered even for streaming clients, so permission is
checked again before any generated content is released. Ambiguous requests keep
their reservation; retrying cannot silently escape a task's inference budget.
"""

from dataclasses import dataclass
import http.client
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.views.decorators.cache import never_cache
from django.db import transaction
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from .models import AgentGrant, AgentTask


class ModelProxyError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class Reservation:
    task_id: object
    grant_id: object
    lease_epoch: int
    tokens: int
    cost_microunits: int
    input_rate: int
    output_rate: int


def token_cost(input_tokens, output_tokens, input_rate, output_rate):
    return (input_tokens * input_rate + output_tokens * output_rate + 999999) // 1000000


def model_rates():
    values = (getattr(settings, "AGENT_RUNTIME_MODEL_INPUT_MICROUNITS_PER_MILLION", 0),
              getattr(settings, "AGENT_RUNTIME_MODEL_OUTPUT_MICROUNITS_PER_MILLION", 0))
    if any(type(value) is not int or not 0 < value <= 1000000000 for value in values):
        raise ModelProxyError("model_pricing_not_configured", 503)
    return values


def prepare_request(payload, model, *, max_bytes=262144):
    if not isinstance(payload, dict) or not model or payload.get("model") != model:
        raise ModelProxyError("model_not_allowed")
    allowed = {
        "model", "messages", "tools", "tool_choice", "temperature", "top_p",
        "stop", "max_tokens", "max_completion_tokens", "response_format",
        "parallel_tool_calls", "reasoning_effort", "stream", "stream_options",
    }
    if set(payload) - allowed:
        raise ModelProxyError("unsupported_model_parameter")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise ModelProxyError("invalid_messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ModelProxyError("invalid_messages")
        if set(message) - {"role", "content", "name", "tool_calls", "tool_call_id", "reasoning_content", "refusal"}:
            raise ModelProxyError("unsupported_message_parameter")
        if "tool_calls" in message:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or len(calls) > 32 or any(
                not isinstance(call, dict) or set(call) - {"id", "type", "function", "index"}
                or call.get("type") != "function" or not isinstance(call.get("function"), dict)
                or set(call["function"]) - {"name", "arguments"}
                for call in calls
            ):
                raise ModelProxyError("unsupported_tool_call")
        content = message.get("content")
        # Remote images/audio/files introduce upstream fetch authority. Start
        # with text/tool messages; add media only through a reviewed broker.
        if content is not None and not isinstance(content, str):
            if not isinstance(content, list) or any(
                not isinstance(part, dict) or set(part) != {"type", "text"}
                or part["type"] != "text" or not isinstance(part["text"], str)
                for part in content
            ):
                raise ModelProxyError("remote_media_not_allowed")
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 128 or any(
        not isinstance(tool, dict) or set(tool) != {"type", "function"}
        or tool.get("type") != "function" or not isinstance(tool.get("function"), dict)
        or set(tool["function"]) - {"name", "description", "parameters", "strict"}
        for tool in tools
    ):
        raise ModelProxyError("unsupported_tool_type")
    if "max_tokens" in payload and "max_completion_tokens" in payload:
        raise ModelProxyError("ambiguous_output_limit")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ModelProxyError("model_input_too_large", 413)
    output_limit = payload.get("max_completion_tokens", payload.get("max_tokens", 4096))
    if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 1 <= output_limit <= 16384:
        raise ModelProxyError("invalid_output_limit")
    upstream = dict(payload)
    streaming = upstream.pop("stream", False)
    if not isinstance(streaming, bool):
        raise ModelProxyError("invalid_stream")
    upstream.pop("stream_options", None)
    upstream["stream"] = False
    if "max_completion_tokens" not in upstream and "max_tokens" not in upstream:
        upstream["max_tokens"] = output_limit
    # UTF-8 bytes deliberately over-reserve text BPE tokens, with framing room.
    return upstream, streaming, len(encoded) + 4096 + output_limit


def _valid_task(task, grant, now, lease_epoch):
    return bool(
        task.status == AgentTask.Status.RUNNING
        and task.generation == grant.generation
        and task.lease_epoch == lease_epoch
        and task.lease_expires_at and task.lease_expires_at > now
        and task.deadline_at and task.deadline_at > now
        and not grant.revoked_at and grant.expires_at > now
        and grant.installation.is_active and grant.installation.owner.is_active
    )


def reserve_budget(principal, tokens, output_tokens):
    from .runtime import _task_with_grant_lock
    input_rate, output_rate = model_rates()
    cost = token_cost(tokens - output_tokens, output_tokens, input_rate, output_rate)
    with transaction.atomic():
        task = _task_with_grant_lock(principal.task.pk)
        grant = task.grant
        if not _valid_task(task, grant, timezone.now(), principal.credential.lease_epoch):
            raise ModelProxyError("task_authority_expired", 403)
        max_calls = min(int(task.limits.get("max_model_calls", 20)), 100)
        max_tokens = min(int(task.limits.get("max_model_tokens", 200000)), 1000000)
        if task.model_calls >= max_calls or task.model_tokens_used + task.reserved_model_tokens + tokens > max_tokens:
            raise ModelProxyError("model_budget_exhausted", 429)
        if task.actual_cost_microunits + cost > task.limits.get("max_cost_microunits", 0):
            raise ModelProxyError("model_cost_budget_exhausted", 429)
        task.model_calls += 1
        task.reserved_model_tokens += tokens
        # This counter includes conservative pending/ambiguous charges. A
        # trusted usage receipt may reduce that reservation, never a worker.
        task.actual_cost_microunits += cost
        task.save(update_fields=["model_calls", "reserved_model_tokens", "actual_cost_microunits", "updated_at"])
        return Reservation(task.pk, grant.pk, task.lease_epoch, tokens, cost, input_rate, output_rate)


def settle_budget(reservation, usage):
    from .runtime import _task_with_grant_lock
    # Missing or implausible usage keeps the conservative full reservation.
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= reservation.tokens:
        return
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    measured_cost = reservation.cost_microunits
    if (type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0
            and prompt + completion == total):
        measured_cost = min(measured_cost, token_cost(prompt, completion, reservation.input_rate, reservation.output_rate))
    with transaction.atomic():
        task = _task_with_grant_lock(reservation.task_id)
        task.reserved_model_tokens = max(0, task.reserved_model_tokens - reservation.tokens)
        task.model_tokens_used += total
        task.actual_cost_microunits = max(0, task.actual_cost_microunits - reservation.cost_microunits + measured_cost)
        task.save(update_fields=["reserved_model_tokens", "model_tokens_used", "actual_cost_microunits", "updated_at"])


def upstream_completion(payload):
    base = str(getattr(settings, "AGENT_RUNTIME_MODEL_BASE_URL", "")).rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ModelProxyError("model_provider_not_configured", 503)
    key = str(getattr(settings, "AGENT_RUNTIME_MODEL_API_KEY", ""))
    if not key or any(c in key for c in "\r\n"):
        raise ModelProxyError("model_provider_not_configured", 503)
    limit = int(getattr(settings, "AGENT_RUNTIME_MODEL_MAX_RESPONSE_BYTES", 2097152))
    timeout = min(float(getattr(settings, "AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS", 90)), 120)
    if not 1 <= timeout <= 120 or not 1024 <= limit <= 16777216:
        raise ModelProxyError("model_provider_not_configured", 503)
    # DNS and slow-trickling headers/body share one wall-clock budget. The
    # operator-configured model origin may be private; unlike member webhook
    # destinations it is not caller-selected and does not use the public-IP ACL.
    from .webhook_transport import _bounded_resolve, CallbackEndpointError
    deadline = time.monotonic() + timeout
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or 443
        resolved = _bounded_resolve(hostname, socket.getaddrinfo, timeout=min(3, timeout))
        address = resolved[0][4][0]
    except (CallbackEndpointError, UnicodeError, ValueError, IndexError):
        raise ModelProxyError("model_provider_unavailable", 502) from None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelProxyError("model_provider_unavailable", 502)
    connection = http.client.HTTPSConnection(hostname, port, timeout=remaining, context=ssl.create_default_context())
    active_socket = None
    def connect(_destination, socket_timeout, source_address=None):
        available = deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError()
        sock = socket.create_connection((address, port), min(socket_timeout, available), source_address)
        available = deadline - time.monotonic()
        if available <= 0:
            sock.close()
            raise TimeoutError()
        sock.settimeout(available)
        return sock
    connection._create_connection = connect
    def expire():
        # Keep the original socket reference: HTTPConnection may detach it for
        # a Connection: close response while its buffered reader is still open.
        sock = active_socket or connection.sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
    timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        path = parsed.path.rstrip("/") + "/chat/completions"
        connection.request("POST", path, body=encoded, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Content-Length": str(len(encoded)), "Accept-Encoding": "identity",
        })
        active_socket = connection.sock
        response = connection.getresponse()
        if response.status != 200:
            raise ModelProxyError("model_provider_rejected", 502)
        chunks, length = [], 0
        while True:
            chunk = response.read(min(65536, limit - length + 1))
            if not chunk:
                break
            length += len(chunk)
            if length > limit:
                raise ModelProxyError("model_response_too_large", 502)
            chunks.append(chunk)
        if time.monotonic() > deadline:
            raise ModelProxyError("model_provider_unavailable", 502)
        result = json.loads(b"".join(chunks))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, ModelProxyError):
            raise
        raise ModelProxyError("model_provider_unavailable", 502) from None
    finally:
        timer.cancel()
        connection.close()
    if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
        raise ModelProxyError("invalid_model_response", 502)
    if result.get("model") and not isinstance(result["model"], str):
        raise ModelProxyError("invalid_model_response", 502)
    return {key: result[key] for key in ("id", "object", "created", "model", "choices", "usage") if key in result}


def sse_completion(result):
    base = {key: result[key] for key in ("id", "created", "model") if key in result}
    base["object"] = "chat.completion.chunk"
    for choice in result["choices"]:
        message = dict(choice.get("message") or {})
        if isinstance(message.get("tool_calls"), list):
            message["tool_calls"] = [{**call, "index": i} for i, call in enumerate(message["tool_calls"])]
        yield "data: " + json.dumps({**base, "choices": [{"index": choice.get("index", 0), "delta": message, "finish_reason": None}]}) + "\n\n"
        yield "data: " + json.dumps({**base, "choices": [{"index": choice.get("index", 0), "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]}) + "\n\n"
    if "usage" in result:
        yield "data: " + json.dumps({**base, "choices": [], "usage": result["usage"]}) + "\n\n"
    yield "data: [DONE]\n\n"


@never_cache
@api_view(["POST"])
@authentication_classes([])
@permission_classes([])
def model_chat_completions(request):
    from .authentication import require_task_principal

    if not getattr(settings, "AGENT_RUNTIME_ENABLED", False):
        return JsonResponse({"error": "not_found"}, status=404)
    try:
        principal = require_task_principal(request, audience="agent-model")
        maximum = int(getattr(settings, "AGENT_RUNTIME_MODEL_MAX_REQUEST_BYTES", 262144))
        if len(request.body) > maximum:
            raise ModelProxyError("model_input_too_large", 413)
        payload, streaming, tokens = prepare_request(
            json.loads(request.body), str(getattr(settings, "AGENT_RUNTIME_MODEL_NAME", "")), max_bytes=maximum,
        )
        output_tokens = payload.get("max_completion_tokens", payload.get("max_tokens", 4096))
        reservation = reserve_budget(principal, tokens, output_tokens)
        result = upstream_completion(payload)
        settle_budget(reservation, result.get("usage"))
        require_task_principal(request, audience="agent-model")
        response = StreamingHttpResponse(sse_completion(result), content_type="text/event-stream") if streaming else JsonResponse(result)
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    except ModelProxyError as exc:
        return JsonResponse({"error": {"code": exc.code}}, status=exc.status)
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": {"code": "invalid_model_request"}}, status=400)
