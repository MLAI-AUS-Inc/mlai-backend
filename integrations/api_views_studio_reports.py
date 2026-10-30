"""A separate service key grants only the configured client source export."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import time

from django.conf import settings
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from .services.studio_report_source import SourceIncomplete, instant, prepare, mailbox_ids


class HasStudioSourceKey(BasePermission):
    def has_permission(self, request, view):
        key = getattr(settings, "STUDIO_REPORT_SOURCE_KEY", "")
        return bool(getattr(settings, "STUDIO_REPORT_SOURCE_ENABLED", False) and len(key) >= 32
                    and key not in {getattr(settings, "ROO_API_KEY", ""), getattr(settings, "INTERNAL_API_KEY", "")}
                    and hmac.compare_digest(key.encode(), request.headers.get("X-Studio-Source-Key", "").encode()))


class StudioReportPrepareView(APIView):
    authentication_classes = []
    permission_classes = [HasStudioSourceKey]
    throttle_classes = []  # Private worker coordinator serializes and backs off.

    def post(self, request):
        data = request.data
        try:
            if (not isinstance(data, dict) or set(data) != {"owner", "team", "cutoff", "nonce"}
                    or any(not isinstance(data[key], str) for key in data)
                    or re.fullmatch(r"[a-f0-9]{64}", data["nonce"]) is None):
                raise ValueError
            instant(data["cutoff"])
            scopes = json.loads(Path(settings.STUDIO_REPORT_SOURCE_CONFIG).read_text())
            scope = scopes[data["owner"]]
            if scope["team"] != data["team"]:
                raise ValueError
        except (KeyError, TypeError, ValueError, OSError):
            return Response({"error": "invalid_source_scope"}, status=400)
        try:
            from .models import ExternalServiceConnection
            from .services.external_connectors import _xero_get_json
            from django.db import transaction
            deadline = time.monotonic() + 120
            # Serialize with other source requests; re-read encrypted tokens
            # inside the lock. The existing token owner remains the backend.
            with transaction.atomic():
                connection = ExternalServiceConnection.objects.select_for_update().get(
                    id=scope["xero_connection_id"], organization_id=scope["organization_id"], provider="xero")
                def get(path, **kwargs):
                    if time.monotonic() >= deadline:
                        raise SourceIncomplete("finance_time_limit")
                    return _xero_get_json(connection, path, **kwargs)
                result = prepare(scope, data, get=get, mail=mailbox_ids)
        except Exception:
            return Response({"error": "source_preparation_failed"}, status=503)
        body = json.dumps(result, sort_keys=True, separators=(",", ":"))
        signature = hmac.new(settings.STUDIO_REPORT_SOURCE_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
        return Response({"result": result, "signature": signature})
