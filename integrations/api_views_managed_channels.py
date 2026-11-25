"""Provider-specific verified notifications are persisted before acknowledgment."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services.managed_channels.config import installations
from .services.managed_channels.worker import adapter, receive


@csrf_exempt
@require_POST
def managed_channel_webhook(request, installation):
    config = installations().get(installation)
    if not config:
        return JsonResponse({}, status=404)
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > 256 * 1024:
            return JsonResponse({}, status=413)
        body = request.body
        if len(body) > 256 * 1024:
            return JsonResponse({}, status=413)
        event = adapter(config["provider"]).decode_webhook(config["provider"], config, request.headers, body)
        if event:
            delivery, external_id, action = event
            if not all(isinstance(x, str) and 0 < len(x) <= 255 for x in (external_id, delivery)):
                raise ValueError("Invalid identifiers")
            receive(config, delivery, external_id, action)
    except PermissionError:
        return JsonResponse({}, status=403)
    except (ValueError, TypeError, KeyError, AttributeError):
        return JsonResponse({}, status=400)
    return JsonResponse({"received": True})
