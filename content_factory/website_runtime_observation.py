"""Private, read-only process diagnostics for an existing original goal.

Current configuration is not original execution evidence. CF process records
remain unbound to the selected goal even though their transport is authenticated.
"""

from datetime import timedelta
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.db import connection as database, transaction
from django.utils import timezone
import requests
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from . import website_goal as goals, website_rollout_inspection as rollout, website_zoo_scope as scope

MAX_CF_BYTES = 2 * 1024 * 1024
CF_CONFIGURATION_SCOPE = "named_articles_rollout_flags_v1"
BACKEND_CONFIGURATION_SCOPE = "named_backend_articles_rollout_flags_v1"
SELECTORS = {"company_id", "connection_id", "connection_generation", "goal_id", "target"}
_identity_lock = threading.Lock()
_identity = (None, None)


class ObservationUnavailable(ValueError):
    """Static failure only; never expose private settings or remote text."""


class HasObservationServiceKey(BasePermission):
    """Require the configured internal service key without local/owner bypass."""

    def has_permission(self, request, view):
        expected = getattr(settings, "INTERNAL_API_KEY", None)
        supplied = request.META.get("HTTP_X_API_KEY")
        return (isinstance(expected, str) and bool(expected) and isinstance(supplied, str)
                and secrets.compare_digest(expected.encode(), supplied.encode()))


def _after_fork():
    global _identity_lock, _identity
    _identity_lock, _identity = threading.Lock(), (None, None)


os.register_at_fork(after_in_child=_after_fork)


def _process_instance():
    global _identity
    with _identity_lock:
        pid = os.getpid()
        if _identity[0] != pid:
            _identity = (pid, uuid4().hex)
        return _identity[1]


def backend_process(organization_id):
    """Observe current loaded settings for this responding web process only."""
    value = rollout.rollout_report(organization_id)
    flags = value["backend_flags"]
    digest = goals.digest({"scope": BACKEND_CONFIGURATION_SCOPE, "flags": flags}) if all(
        type(flag) is bool for flag in flags.values()) else None
    return {"role": "backend_web", "instance_id": _process_instance(),
        "observed_at": value["observed_at"], "revision": value["backend_revision"],
        "configuration_scope": BACKEND_CONFIGURATION_SCOPE, "configuration_digest": digest,
        "flags": flags, "new_work_membership": value["enrollment"],
        "new_work_goal_enabled": value["goal_enrolled"], "enrollment_scope_digest": None,
        "original_execution_eligibility": None}


def select_original(query):
    """Accept only exact bounded selectors, not caller-provided observed facts."""
    if (set(query) != SELECTORS or any(len(query.getlist(key)) != 1 for key in SELECTORS)
            or any(not isinstance(query[key], str) or len(query[key]) > 100 for key in SELECTORS)
            or any(not scope.uuid_id(query[key]) for key in ("company_id", "connection_id", "goal_id"))
            or not re.fullmatch(r"[1-9][0-9]{0,8}", query["connection_generation"])
            or query["target"] not in {"ready", "first_article_live"}):
        raise ObservationUnavailable("invalid_original_goal_selection")
    return {"original_goal_id": query["goal_id"], "started_binding": {
        "companyId": query["company_id"], "connectionId": query["connection_id"],
        "connectionGeneration": int(query["connection_generation"])}}, query["target"]


def read_original(row, target):
    """Resolve saved company/goal ownership in an explicitly read-only snapshot."""
    if (database.vendor != "postgresql" or database.in_atomic_block
            or database.connection is not None and not database.get_autocommit()):
        raise ObservationUnavailable("readonly_snapshot_unavailable")
    with transaction.atomic():
        with database.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        selected = scope.resolve_case("selected-original-goal", row, target=target)
        if selected["state"] != "bound":
            raise ObservationUnavailable("original_goal_unavailable")
        return selected, backend_process(selected["organization_id"])


def project_cf_processes(value, *, now):
    """Close the authenticated CF diagnostic projection; never bind it to a run."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("runtime_scope") != "observed_processes_only" or value.get("coverage") != "partial"
            or value.get("run_eligibility", "missing") is not None):
        raise ObservationUnavailable("cf_observation_invalid")
    stamp = goals.stamp(value.get("observed_at"))
    if stamp is None or not now - timedelta(seconds=30) <= stamp <= now:
        raise ObservationUnavailable("cf_observation_stale")
    records = value.get("observations")
    if not isinstance(records, list) or len(records) > 129:
        raise ObservationUnavailable("cf_observation_invalid")
    projected, identities = [], set()
    role_events = {"web": {"read"}, "worker_parent": {"ready", "heartbeat"}, "task_process": {"task_prerun"}}
    for record in records:
        if not isinstance(record, dict):
            raise ObservationUnavailable("cf_observation_invalid")
        role, event, instance = record.get("role"), record.get("event"), record.get("instance_id")
        observed = goals.stamp(record.get("observed_at"))
        flags, sequence = record.get("flags"), record.get("sequence")
        if (type(record.get("schema_version")) is not int or record["schema_version"] != 1
                or not isinstance(role, str) or role not in role_events
                or not isinstance(event, str) or event not in role_events[role]
                or not isinstance(instance, str) or not re.fullmatch(r"[a-f0-9]{32}", instance)
                or (instance, role) in identities or type(sequence) is not int or not 0 < sequence < 2**63
                or observed is None or not now - timedelta(seconds=90) <= observed <= now
                or record.get("configuration_scope") != CF_CONFIGURATION_SCOPE
                or record.get("run_eligibility", "missing") is not None
                or record.get("enrollment_scope_digest", "missing") is not None
                or not isinstance(flags, dict) or set(flags) != set(rollout.CF_FLAGS)
                or any(flag is not None and type(flag) is not bool for flag in flags.values())):
            raise ObservationUnavailable("cf_observation_invalid")
        configuration = record.get("configuration")
        digest = goals.digest({"scope": CF_CONFIGURATION_SCOPE, "flags": flags}) if all(
            type(flag) is bool for flag in flags.values()) else None
        if (record.get("configuration_digest") != digest
                or not isinstance(configuration, str) or configuration not in {"observed", "unavailable"}
                or configuration == "observed" and digest is None
                or configuration == "unavailable" and any(flag is not None for flag in flags.values())):
            raise ObservationUnavailable("cf_observation_invalid")
        revision = record.get("revision")
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision)):
            raise ObservationUnavailable("cf_observation_invalid")
        identities.add((instance, role))
        projected.append({"instance_id": instance, "role": role, "event": event, "sequence": sequence,
            "observed_at": observed.isoformat(), "revision": revision, "configuration": configuration,
            "configuration_scope": CF_CONFIGURATION_SCOPE, "configuration_digest": digest,
            "flags": {name: flags[name] for name in rollout.CF_FLAGS}})
    return {"state": "observed", "transport": "authenticated_service_https",
        "observed_at": stamp.isoformat(), "goal_binding": "unbound", "coverage": "partial",
        "original_execution_eligibility": None, "observations": projected}


def fetch_cf_processes():
    """Use only configured HTTPS and a real service key; redirects are refused."""
    unknown = {"state": "unknown", "goal_binding": "unbound", "coverage": "partial",
        "original_execution_eligibility": None, "observations": []}
    try:
        base = getattr(settings, "CONTENT_FACTORY_URL", "")
        key = getattr(settings, "CONTENT_FACTORY_API_KEY", None)
        if not isinstance(base, str) or not isinstance(key, str) or not key:
            raise ObservationUnavailable("cf_authenticated_transport_unavailable")
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or len(base) > 2048):
            raise ObservationUnavailable("cf_authenticated_transport_unavailable")
        url = base.rstrip("/") + "/api/internal/articles/process-observations"
        started, raw = time.monotonic(), bytearray()
        # The shared http_client eagerly buffers response.content. A fresh
        # session here bounds the private response before decoding it, and
        # ignores ambient netrc/proxy credentials.
        with requests.Session() as client:
            client.trust_env = False
            with client.get(url, headers={"X-API-Key": key, "Accept": "application/json"},
                    stream=True, allow_redirects=False, timeout=(3, 8)) as response:
                if response.status_code != 200 or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ObservationUnavailable("cf_observation_unavailable")
                for chunk in response.iter_content(chunk_size=8192):
                    if time.monotonic() - started > 10 or len(raw) + len(chunk) > MAX_CF_BYTES:
                        raise ObservationUnavailable("cf_observation_bounds")
                    raw.extend(chunk)
        return project_cf_processes(json.loads(raw), now=timezone.now())
    except ObservationUnavailable as exc:
        return {**unknown, "reason": str(exc)}
    except Exception:
        return {**unknown, "reason": "cf_observation_unavailable"}


class WebsiteRuntimeObservationView(APIView):
    """Read an existing original goal and current processes, granting no authority."""

    authentication_classes = []
    permission_classes = [HasObservationServiceKey]

    def get(self, request):
        try:
            row, target = select_original(request.query_params)
        except ObservationUnavailable:
            return Response({"code": "invalid_original_goal_selection"}, status=400)
        try:
            original, process = read_original(row, target)
            cf = fetch_cf_processes()
            current, latest_process = read_original(row, target)
            if original != current or process != {**latest_process, "observed_at": process["observed_at"]}:
                raise ObservationUnavailable("original_observation_changed")
        except ObservationUnavailable as exc:
            return Response({"code": str(exc)}, status=409, headers={"Cache-Control": "no-store"})
        except Exception:
            return Response({"code": "runtime_observation_unavailable"}, status=503,
                headers={"Cache-Control": "no-store"})
        return Response({"schema_version": 1, "observed_at": timezone.now().isoformat(),
            "basis": "original_goal_process_diagnostics_v1", "scope_evidence": original,
            "backend_process": process, "content_factory_processes": cf,
            "coverage": "partial", "original_execution_eligibility": None, "effective_rollout": None,
            "gaps": ["original_execution_observations_missing", "scheduler_observations_missing",
                "cf_processes_unbound", "owner_action_audit_incomplete"]}, headers={"Cache-Control": "no-store"})
