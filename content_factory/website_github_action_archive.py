"""Bounded read-only GitHub App delivery evidence for an original fixture goal.

App JWTs and raw webhook payloads stay on the backend. This reports a closed
delivery-attempt window, not a guarantee about events GitHub never delivered.
The zoo combines two stable snapshots with its independently frozen resources,
internal owner transports, PR and hosting observations.
"""
from datetime import timedelta
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from django.conf import settings
from django.db import connection as database, transaction
from django.utils import timezone
import requests

from . import website_goal as goals

API = "https://api.github.com"
MAX_PAGES = 20
MAX_BYTES = 16 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_READS = 100
DEADLINE = 20
SETTLE = timedelta(minutes=2)
EVENTS = {"push", "installation", "installation_repositories"}


class ArchiveUnavailable(ValueError):
    """A static diagnostic safe for a private reduced observation."""


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _select(original):
    """Read saved authority independently; no caller controls a provider selector."""
    from founder_tools.models import VibeRaisingCompany
    from .models import OrganizationContentConfig
    from .website_models import WebsiteConnectionOperation
    if (not isinstance(original, dict) or original.get("state") != "bound"
            or not all(_uuid(original.get(key)) for key in ("original_goal_id", "company_id"))
            or not re.fullmatch(r"[1-9][0-9]{0,18}", str(original.get("organization_id", "")))
            or database.vendor != "postgresql" or database.in_atomic_block):
        raise ArchiveUnavailable("provider_original_scope_unavailable")
    with transaction.atomic():
        with database.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        company = VibeRaisingCompany.objects.filter(pk=original["company_id"],
            organization_id=original["organization_id"]).first()
        operation = WebsiteConnectionOperation.objects.select_related("connection").filter(
            pk=original["original_goal_id"], action="prepare", connection__organization_id=original["organization_id"]).first()
        if company is None or operation is None:
            raise ArchiveUnavailable("provider_original_scope_unavailable")
        connection = operation.connection
        scope = goals.scope_for(connection, company.pk)
        goal = goals.mapping(operation.receipt).get("goal")
        historical = original.get("policy_historical_scope_v1", False)
        if historical is True:
            # A disconnect increments both generation and configuration once.
            # Only that exact transition can observe the retained original.
            config = OrganizationContentConfig.objects.select_related("website_connection").filter(
                organization_id=company.organization_id).first()
            expected = {**scope, "connection_generation": connection.generation - 1,
                "configuration_revision": connection.configuration_version - 1}
            saved = goals.mapping(goal).get("scope")
            if (getattr(getattr(config, "website_connection", None), "pk", None) != connection.pk
                    or connection.state != "disconnected" or operation.state != "cancelled"
                    or operation.generation != expected["connection_generation"]
                    or not goals.scope_matches(saved, expected)):
                raise ArchiveUnavailable("provider_original_scope_unavailable")
            scope = saved
        elif historical is not False or operation.generation != connection.generation:
            raise ArchiveUnavailable("provider_original_scope_unavailable")
        if (goals.digest(scope) != original.get("scope_digest")
                or not goals.valid_goal(goal, scope) or goal["goal_id"] != str(operation.pk)
                or goal["target"] != original.get("target")
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/mlai-articles-fixture-[a-z0-9-]+", connection.github_repo)
                or not re.fullmatch(r"[1-9][0-9]{0,18}", connection.installation_id)
                or type(connection.repository_id) is not int or connection.repository_id < 1):
            raise ArchiveUnavailable("provider_original_scope_unavailable")
        # A complete target or a stable owner/policy stop bounds the observation
        # window on the server. Pending/leased goals cannot choose an early end.
        if goals.complete(goal):
            stamps = [goals.stamp(goals.mapping(row.get("proof")).get("at"))
                for row in goal["milestones"] if row.get("required")]
            if not stamps or any(value is None for value in stamps):
                raise ArchiveUnavailable("provider_goal_window_unavailable")
            finished = max(stamps)
        elif operation.state in {"needs_user", "needs_attention", "stopped", "failed", "cancelled", "unsupported"} and operation.next_attempt_at is None:
            finished = operation.updated_at
        else:
            raise ArchiveUnavailable("provider_goal_window_open")
        started = goals.stamp(goal.get("started_at"))
        return {"goal_id": str(operation.pk), "scope_digest": goals.digest(scope),
            "repository_id": connection.repository_id, "installation_id": int(connection.installation_id),
            "github_repo": connection.github_repo, "started": started, "finished": finished + SETTLE}


class AppReads:
    """Only fixed App endpoints; refuse redirects, ambient proxies and large data."""
    def __init__(self, token, *, clock=time.monotonic):
        self.token, self.clock = token, clock
        self.started, self.bytes, self.reads = clock(), 0, 0
        self.client = requests.Session()
        self.client.trust_env = False

    def close(self):
        self.client.close()

    def get(self, path):
        if (not re.fullmatch(r"/app(?:/hook/config|/hook/deliveries(?:/[1-9][0-9]*)?(?:\?per_page=100(?:&cursor=[A-Za-z0-9_%.-]{1,512})?)?)?", path)
                or self.reads >= MAX_READS or self.clock() - self.started >= DEADLINE):
            raise ArchiveUnavailable("provider_archive_bounds")
        self.reads += 1
        raw = bytearray()
        with self.client.get(API + path, headers={"Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                timeout=(3, 5), stream=True, allow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ArchiveUnavailable("provider_archive_unavailable")
            for chunk in response.iter_content(chunk_size=8192):
                self.bytes += len(chunk)
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE or self.bytes > MAX_BYTES or self.clock() - self.started >= DEADLINE:
                    raise ArchiveUnavailable("provider_archive_bounds")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ArchiveUnavailable("provider_archive_malformed") from None
            return value, response.headers.get("Link", "")


def _next(link):
    if len(link) > 8192:
        raise ArchiveUnavailable("provider_archive_pagination")
    parts = [item.strip() for item in link.split(",")] if link else []
    if any(not re.fullmatch(r'<[^>]+>;\s*rel="(?:next|prev|first|last)"', item) for item in parts):
        raise ArchiveUnavailable("provider_archive_pagination")
    matches = re.findall(r'<([^>]+)>;\s*rel="next"', link)
    if not matches:
        return None
    if len(matches) != 1:
        raise ArchiveUnavailable("provider_archive_pagination")
    parsed = urlsplit(matches[0])
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != "/app/hook/deliveries"
            or parsed.fragment or set(query) != {"per_page", "cursor"} or query["per_page"] != ["100"]
            or len(query["cursor"]) != 1 or not re.fullmatch(r"[A-Za-z0-9_=+/.:-]{1,512}", query["cursor"][0])):
        raise ArchiveUnavailable("provider_archive_pagination")
    return parsed.path + "?" + urlencode({"per_page": 100, "cursor": query["cursor"][0]})


def _profile(get):
    app, _ = get("/app")
    config, _ = get("/app/hook/config")
    app_id = str(getattr(settings, "GITHUB_APP_ID", ""))
    if (not isinstance(app, dict) or str(app.get("id")) != app_id
            or not isinstance(app.get("events"), list) or "push" not in app["events"]
            or not isinstance(config, dict) or config.get("content_type") != "json"
            or str(config.get("insecure_ssl")) != "0" or urlsplit(str(config.get("url", ""))).scheme != "https"):
        raise ArchiveUnavailable("provider_archive_subscription_unproved")
    configuration = goals.digest({"app_id": app_id, "events": sorted(app["events"]),
        "permissions": app.get("permissions"), "webhook_url": config["url"], "content_type": "json", "tls": True})
    return configuration, app, config


def collect(selected, get, *, now):
    """Enumerate attempted deliveries, including failures and lost ingress."""
    started, finished = selected["started"], selected["finished"]
    if (started is None or finished is None or started.utcoffset() is None or finished.utcoffset() is None
            or not now - timedelta(hours=48) <= started <= finished <= now):
        raise ArchiveUnavailable("provider_archive_window_unavailable")
    configuration, app, config = _profile(get)
    path, seen_paths, seen_ids, deliveries = "/app/hook/deliveries?per_page=100", set(), set(), []
    older, previous = False, None
    for _ in range(MAX_PAGES):
        if path in seen_paths:
            raise ArchiveUnavailable("provider_archive_pagination")
        seen_paths.add(path)
        rows, link = get(path)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ArchiveUnavailable("provider_archive_malformed")
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1
                    or row["id"] in seen_ids or type(row.get("redelivery")) is not bool):
                raise ArchiveUnavailable("provider_archive_malformed")
            seen_ids.add(row["id"])
            at = goals.stamp(row.get("delivered_at"))
            if at is None or at > now or previous is not None and at > previous:
                raise ArchiveUnavailable("provider_archive_order_unproved")
            previous = at
            if at < started:
                older = True
            if started <= at <= finished and row.get("installation_id") == selected["installation_id"] and row.get("event") in EVENTS:
                if row.get("event") == "push" and row.get("repository_id") != selected["repository_id"]:
                    continue
                deliveries.append(row)
        path = _next(link)
        if older or path is None:
            break
    else:
        raise ArchiveUnavailable("provider_archive_pagination_incomplete")
    observations, guids = [], {}
    for row in deliveries:
        detail, _ = get("/app/hook/deliveries/" + str(row["id"]))
        if (not isinstance(detail, dict) or any(detail.get(key) != row.get(key) for key in
                ("id", "guid", "event", "action", "installation_id", "repository_id", "delivered_at", "redelivery"))):
            raise ArchiveUnavailable("provider_archive_delivery_changed")
        payload = goals.mapping(goals.mapping(detail.get("request")).get("payload"))
        headers = goals.mapping(goals.mapping(detail.get("request")).get("headers"))
        headers = {key.lower(): value for key, value in headers.items() if isinstance(key, str)}
        guid = row.get("guid")
        if (not _uuid(guid) or headers.get("x-github-delivery") != guid or headers.get("x-github-event") != row["event"]
                or goals.mapping(payload.get("installation")).get("id") != selected["installation_id"]):
            raise ArchiveUnavailable("provider_archive_delivery_unproved")
        allowed = {"push": {None}, "installation": {"created", "deleted", "new_permissions_accepted", "suspend", "unsuspend"},
            "installation_repositories": {"added", "removed"}}[row["event"]]
        if row.get("action") not in allowed or payload.get("action") != row.get("action"):
            raise ArchiveUnavailable("provider_archive_delivery_unproved")
        if row["event"] == "push":
            repo = goals.mapping(payload.get("repository"))
            if repo.get("id") != selected["repository_id"] or repo.get("full_name") != selected["github_repo"]:
                raise ArchiveUnavailable("provider_archive_repository_mismatch")
        elif row["event"] == "installation_repositories":
            added, removed = payload.get("repositories_added"), payload.get("repositories_removed")
            if not isinstance(added, list) or not isinstance(removed, list):
                raise ArchiveUnavailable("provider_archive_delivery_unproved")
            repos = added + removed
            if len(repos) > 10000 or any(not isinstance(item, dict) or type(item.get("id")) is not int for item in repos):
                raise ArchiveUnavailable("provider_archive_delivery_unproved")
            if selected["repository_id"] not in {item.get("id") for item in repos}:
                continue
        actor = goals.mapping(payload.get("sender"))
        if type(actor.get("id")) is not int or actor["id"] < 1 or actor.get("type") not in {"User", "Bot"}:
            raise ArchiveUnavailable("provider_archive_actor_unproved")
        fact = {"event_id": guid, "source": "github_push" if row["event"] == "push" else "github_access_approval",
            "actor_id": actor["id"], "actor_type": actor["type"], "action": row.get("action"),
            "event": row["event"], "forced": payload.get("forced") if row["event"] == "push" else None}
        if fact["event"] == "push" and type(fact["forced"]) is not bool:
            raise ArchiveUnavailable("provider_archive_delivery_unproved")
        if guid in guids and guids[guid] != fact:
            raise ArchiveUnavailable("provider_archive_redelivery_changed")
        guids[guid] = fact
    observations = [guids[key] for key in sorted(guids)]
    final_app, _ = get("/app")
    final_config, _ = get("/app/hook/config")
    if final_app != app or final_config != config:
        raise ArchiveUnavailable("provider_archive_configuration_changed")
    return {"version": 1, "basis": "github_app_delivery_window_v1", "goal_id": selected["goal_id"],
        "scope_digest": selected["scope_digest"], "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "observed_at": now.isoformat(), "coverage": "complete", "coverage_scope": "enumerated_delivery_attempts_only",
        "configuration_digest": configuration, "sources": observations,
        "snapshot_digest": goals.digest({"configuration": configuration, "sources": observations,
            "started": started.isoformat(), "finished": finished.isoformat(), "scope": selected["scope_digest"]}),
        "remaining_requirement": "stable_repeated_window_and_frozen_provider_configuration"}


def read_bound_archive(original):
    """Resolve original ownership and reduce App data; never return JWT or payload."""
    unknown = {"version": 1, "basis": "github_app_delivery_window_v1", "coverage": "partial", "sources": []}
    reader = None
    try:
        if not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
            raise ArchiveUnavailable("provider_archive_disabled")
        selected = _select(original)
        from integrations.services.github_app import _github_app_jwt
        reader = AppReads(_github_app_jwt())
        return collect(selected, reader.get, now=timezone.now())
    except ArchiveUnavailable as exc:
        return {**unknown, "reason": str(exc)}
    except Exception:
        return {**unknown, "reason": "provider_archive_unavailable"}
    finally:
        if reader is not None:
            reader.close()


def read_profile():
    """A reduced preflight fingerprint to freeze before admitting the test cohort."""
    unknown = {"version": 1, "basis": "github_app_action_profile_v1", "coverage": "partial", "configuration_digest": None}
    reader = None
    try:
        if not getattr(settings, "ARTICLES_WEBSITE_DOCTOR_ENABLED", False):
            raise ArchiveUnavailable("provider_archive_disabled")
        from integrations.services.github_app import _github_app_jwt
        reader = AppReads(_github_app_jwt())
        configuration, _, _ = _profile(reader.get)
        return {**unknown, "coverage": "complete", "configuration_digest": configuration,
            "observed_at": timezone.now().isoformat()}
    except ArchiveUnavailable as exc:
        return {**unknown, "reason": str(exc)}
    except Exception:
        return {**unknown, "reason": "provider_archive_unavailable"}
    finally:
        if reader is not None:
            reader.close()
