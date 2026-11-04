"""Disposable CI-only wrapper around the actual authenticated bootstrap path."""

from contextlib import ExitStack
import json
import os
import socket
import urllib.request
from unittest.mock import patch

from scripts.check_worker_memory_smoke import require_ci, CANARY

require_ci()
if (os.environ.get("ARTICLES_BOOTSTRAP_MEMORY_CI") != "1" or
        os.environ.get("DATABASE_URL") != "postgresql://mlai:mlai_test_password@127.0.0.1:5432/test_mlai_test"):
    raise ValueError("Bootstrap application requires its disposable CI database")

import django
django.setup()

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.views import APIView
import httpx
import requests

from content_factory import vibe_marketing_views as views


def denied(*args, **kwargs):
    """A bootstrap read cannot acquire provider or reconciliation effects."""
    raise AssertionError("External effects forbidden in the bootstrap memory fixture")


def skip_fixture_throttles(self, request):
    """Bypass fixture throttling without retaining requests in a mock call log."""


_boundaries = ExitStack()
_boundaries.enter_context(patch.object(requests.sessions.Session, "request", denied))
_boundaries.enter_context(patch.object(httpx.Client, "send", denied))
_boundaries.enter_context(patch.object(httpx.AsyncClient, "send", denied))
_boundaries.enter_context(patch.object(urllib.request, "urlopen", denied))
_boundaries.enter_context(patch.object(socket, "create_connection", denied))
_boundaries.enter_context(patch.object(socket.socket, "connect", denied))
_boundaries.enter_context(patch.object(socket, "getaddrinfo", denied))
_boundaries.enter_context(patch("integrations.services.github_app.create_installation_access_token", denied))
_boundaries.enter_context(patch("integrations.services.github_app.probe_installation_liveness", denied))
_boundaries.enter_context(patch.object(views, "_refresh_pending_article_system_setup_pr_status", denied))
_boundaries.enter_context(patch.object(views, "refresh_publish_statuses", denied))
# Measure the application path without substituting rate-limit responses for it.
# This override exists only in this unreferenced test WSGI entry point.
_boundaries.enter_context(patch.object(APIView, "check_throttles", skip_fixture_throttles))

_user_id = os.environ.get("BOOTSTRAP_MEMORY_USER_ID", "")
if not _user_id.isdigit() or int(_user_id) <= 0:
    raise ValueError("Bootstrap memory fixture owner is unavailable")
_user = get_user_model().objects.get(pk=int(_user_id), email="founder@example.com")
_client = APIClient()
_client.force_authenticate(user=_user)


def application(environ, start_response):
    """Run the actual URL/view/ORM/serialization path; export numeric facts only."""
    response = _client.get("/api/v1/vibe-marketing/bootstrap/", HTTP_X_FIXTURE_CANARY=CANARY)
    body = response.content
    if (response.status_code != 200 or response.get("X-MLAI-View") != "summary" or
            not 0 < len(body) < 300000 or len(response.data.get("latestRuns", [])) != 24 or
            any(marker in body for marker in (b"large-review-sentinel", b"raw-diagnostic-sentinel", b"raw-body-sentinel"))):
        raise ValueError("Bootstrap memory fixture read failed")
    result = json.dumps({"runs": 24, "bytes": len(body)}, sort_keys=True).encode()
    start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(result)))])
    return [result]
