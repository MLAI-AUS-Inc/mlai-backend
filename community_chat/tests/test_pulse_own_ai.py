"""Database-free "draft with my own AI" journey and its connection lifetime rules.

The journey test drives the real views and OAuth store end to end against
/mcp/mlai. Only the ORM boundary (startup, account, saved updates) is faked.
"""
import base64
import hashlib
import json
import re
import time
from contextlib import ExitStack
from datetime import date, datetime, timezone as datetime_timezone
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.startups import agent_connections
from startup_updates.mcp import config, draft_task, oauth, registry, tools, views
from .test_startup_mcp import COMPANY_ID, SETTINGS, grant

UNIFIED = {**SETTINGS, "MLAI_MCP_ENABLED": True}
PULSE = ["startup:brief:read", "startup:draft:write"]
NOW = datetime(2026, 10, 20, 4, 0, tzinfo=datetime_timezone.utc)
ACCEPT = "application/json, text/event-stream"


class Drafts:
    """The few manager calls the Pulse tools make, over an in-memory list."""
    def __init__(self, rows=None):
        self.rows = [] if rows is None else rows

    def select_for_update(self):
        return self

    def select_related(self, *fields):
        return self

    def filter(self, *unpublished, **query):
        # A positional Q is existing_drafts' "has unpublished work" condition.
        rows = [row for row in self.rows if not unpublished
            or not row.published_at or row.current_revision_id != row.published_revision_id]
        return Drafts([row for row in rows if all(
            getattr(row, "pk" if name == "pk" else name, None) == value for name, value in query.items() if name in {"pk", "month"})])

    def order_by(self, *fields):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def __getitem__(self, item):
        return self.rows[item]


def account():
    user = Obj(pk=7, auth_version=3, is_active=True, is_authenticated=True)
    session = Obj(pk="session", user_id=7, user=user, revoked_at=None, auth_version=3,
        expires_at=datetime(2099, 1, 1, tzinfo=datetime_timezone.utc))
    return user, session


@override_settings(**UNIFIED)
class OwnAiJourneyTests(SimpleTestCase):
    """Register, authorise, approve the defaults, draft, come back next month."""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.user, self.session = account()
        self.company = Obj(pk=UUID(COMPANY_ID), name="Example Startup", organization=Obj(pk=8, domain="example.test"))
        self.drafts = Drafts()
        self.sessions = MagicMock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        companies = stack.enter_context(patch.object(oauth.VibeRaisingCompany, "objects"))
        companies.select_related.return_value.filter.return_value.first.return_value = self.company
        # agent_connections imports the same model: organisation-less startups are a separate query.
        companies.filter.side_effect = lambda **query: MagicMock(order_by=MagicMock(
            return_value=[] if query.get("organization__isnull") else [self.company]))
        users = stack.enter_context(patch.object(oauth, "get_user_model"))
        users.return_value.objects.filter.return_value.first.return_value = self.user
        stack.enter_context(patch("django.contrib.auth.get_user_model"))
        stack.enter_context(patch.object(oauth, "require_community_access"))
        stack.enter_context(patch.object(oauth, "_validate_device_owner"))
        stack.enter_context(patch.object(oauth.CommunityChatAccountSession, "objects", self.sessions))
        stack.enter_context(patch.object(agent_connections, "has_community_access", return_value=True))
        stack.enter_context(patch.object(tools.timezone, "now", return_value=NOW))
        profiles = stack.enter_context(patch.object(tools.StartupProfile, "objects"))
        profiles.filter.return_value.first.return_value = Obj(reporting_timezone="Australia/Melbourne",
            short_description="Synthetic test startup", stage="Idea", problem_solved="", target_audience="")
        stack.enter_context(patch.object(tools.MonthlyUpdateDraft, "objects", self.drafts))
        stack.enter_context(patch.object(tools, "resolve_update", side_effect=self.resolve_update))
        stack.enter_context(patch.object(tools, "capture_snapshot", return_value=Obj(payload={})))
        stack.enter_context(patch.object(tools, "save_revision", side_effect=self.save_revision))
        # transaction.atomic needs a database; the body is what is under test.
        stack.enter_context(patch.object(tools, "save_draft", tools.save_draft.__wrapped__))

    def resolve_update(self, organization, *, month, creation_key=None, update_date=None, update_id=None):
        draft = Obj(pk=40 + len(self.drafts.rows), month=month, status="draft", title="October update", structured_memo={},
            current_revision=None, current_revision_id=None, published_revision_id=None, published_at=None,
            groundedness_status="pending", updated_at=NOW, run=None, refresh_from_db=MagicMock(), save=MagicMock(), revisions=MagicMock())
        draft.revisions.filter.return_value.order_by.return_value.first.return_value = None
        self.drafts.rows.append(draft)
        return draft, True

    def save_revision(self, draft, memo, *, snapshot, audience, expected_revision, validation):
        number = (draft.current_revision_id or 500) + 1
        draft.current_revision = Obj(pk=number, content_hash=f"hash-{number}", structured_memo=memo, validation=validation, snapshot=snapshot)
        draft.current_revision_id = number
        return draft.current_revision

    def rpc(self, access, method, params=None, *, identifier=1):
        body = {"jsonrpc": "2.0", "id": identifier, "method": method, **({"params": params} if params is not None else {})}
        headers = {"HTTP_ACCEPT": ACCEPT, **({"HTTP_AUTHORIZATION": f"Bearer {access}"} if access else {})}
        return views.MlaiMcpView.as_view(throttle_classes=())(self.factory.post("/mcp/mlai", body, format="json", **headers))

    def tool(self, access, name, arguments):
        value = json.loads(self.rpc(access, "tools/call", {"name": name, "arguments": arguments}).content)["result"]
        self.assertFalse(value["isError"], value)
        return value["structuredContent"]

    def token(self, **fields):
        response = views.TokenView.as_view(throttle_classes=())(self.factory.post(
            "/mcp/oauth/token", urlencode(fields), content_type="application/x-www-form-urlencoded"))
        return response.status_code, json.loads(response.content)

    def consent(self, request_id, method="get", body=None):
        request = getattr(self.factory, method)(
            f"/api/v1/community-chat/startups/agent-connection/authorization/{request_id}/", body, format="json")
        force_authenticate(request, user=self.user, token=self.session)
        return agent_connections.AgentAuthorizationView.as_view()(request, request_id=request_id)

    def test_first_time_connection_with_default_approval_drafts_and_survives_a_retried_refresh(self):
        # 1. The AI app is pointed at the server and is told what to ask for.
        challenge = self.rpc(None, "initialize")
        self.assertEqual(challenge.status_code, 401)
        header = challenge["WWW-Authenticate"]
        self.assertIn('resource_metadata="https://api.example.test/.well-known/oauth-protected-resource/mcp/mlai"', header)
        scope = re.search(r'scope="([^"]+)"', header).group(1)
        self.assertEqual(scope, "startup:brief:read startup:draft:write")

        # 2. Dynamic client registration, then the browser sign-in hand-off.
        registered = views.RegisterView.as_view(throttle_classes=())(self.factory.post("/mcp/oauth/register", {
            "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "token_endpoint_auth_method": "none"}, format="json"))
        self.assertEqual(registered.status_code, 201)
        client = json.loads(registered.content)
        verifier = "v" * 64
        authorize = views.AuthorizeView.as_view(throttle_classes=())(self.factory.get("/mcp/oauth/authorize", {
            "client_id": client["client_id"], "redirect_uri": client["redirect_uris"][0], "response_type": "code",
            "resource": config.mlai_url(), "scope": scope + " offline_access", "state": "client-state", "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()},
            HTTP_ACCEPT="text/html"))
        self.assertEqual(authorize.status_code, 302)
        location = urlsplit(authorize["Location"])
        self.assertEqual(location.path, "/connections/mcp")
        request_id = parse_qs(location.query)["mcpAuthorization"][0]

        # 3. The permission screen: one Pulse capability, an eligible founder.
        screen = self.consent(request_id)
        self.assertEqual(screen.status_code, 200)
        self.assertEqual(screen.data["eligibility"], {"ok": True, "reason": None, "code": None})
        self.assertEqual([item["id"] for item in screen.data["capabilities"]], ["pulse"])
        self.assertEqual(screen.data["companies"], [{"id": COMPANY_ID, "name": "Example Startup"}])
        self.assertEqual(screen.data["unavailableCompanies"], [])

        # 4. Allow with what older screens tick by default: read only. Pulse is
        #    one capability, so the connection can still save the draft.
        approved = self.consent(request_id, "post", {"requestId": request_id, "approve": True,
            "companyId": COMPANY_ID, "scopes": ["startup:brief:read"]})
        self.assertEqual(approved.status_code, 200, approved.data)
        callback = parse_qs(urlsplit(approved.data["redirectUrl"]).query)
        self.assertEqual(callback["state"], ["client-state"])

        # 5. Code exchange. A trailing slash names the same resource.
        status, tokens = self.token(grant_type="authorization_code", code=callback["code"][0], client_id=client["client_id"],
            redirect_uri=client["redirect_uris"][0], code_verifier=verifier, resource=config.mlai_url() + "/")
        self.assertEqual(status, 200, tokens)
        self.assertEqual(tokens["scope"].split(), PULSE)
        access = tokens["access_token"]

        # 6. initialize and tools/list: the save tool is there, named for MLAI.
        initialized = json.loads(self.rpc(access, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}).content)["result"]
        self.assertEqual(initialized["serverInfo"]["name"], "MLAI")
        listed = json.loads(self.rpc(access, "tools/list").content)["result"]["tools"]
        names = [item["name"] for item in listed]
        for name in ("list_startups", "get_monthly_update_brief", "save_narrative_draft", "get_draft_status"):
            self.assertIn(name, names)
        self.assertNotIn("Valley", json.dumps([item for item in listed if item["name"] in tools_by_name()]))
        save_tool = next(item for item in listed if item["name"] == "save_narrative_draft")
        # OpenAI Apps SDK per-tool auth fields stay intact.
        self.assertEqual(save_tool["securitySchemes"], [{"type": "oauth2", "scopes": ["startup:draft:write"]}])
        self.assertEqual(save_tool["_meta"]["securitySchemes"], save_tool["securitySchemes"])

        # 7. Brief, save, status.
        self.assertEqual(self.tool(access, "list_startups", {}), {"startups": [{"id": COMPANY_ID, "name": "Example Startup"}]})
        window = {"startInclusive": "2026-10-01T00:00:00+10:00", "endExclusive": "2026-10-20T15:00:00+11:00"}
        brief = self.tool(access, "get_monthly_update_brief", {"companyId": COMPANY_ID, "month": "2026-10", "activityWindow": window})
        self.assertEqual(brief["existingDrafts"], [])
        self.assertEqual(brief["activityWindow"], window)
        self.assertNotIn("Valley", json.dumps(brief["instructions"]))
        saved = self.tool(access, "save_narrative_draft", {"companyId": COMPANY_ID, "month": "2026-10", "requestId": str(uuid4()),
            "narrative": {"summary": "We shipped the pilot.", "highlights": "Signed two pilots.\nLaunched onboarding."},
            "sources": [{"provider": "linear", "title": "Pilot launch"}], "coverageNotes": "Linear only."})
        self.assertFalse(saved["replayed"])
        self.assertEqual(saved["provenance"], "agent_supplied")
        self.assertTrue(saved["requiresReview"])
        self.assertIn(f"update_id={saved['updateId']}", saved["reviewUrl"])
        draft = self.drafts.rows[0]
        self.assertEqual(draft.current_revision.structured_memo["highlights"], ["Signed two pilots.", "Launched onboarding."])
        self.assertEqual(draft.current_revision.validation, {"groundedness_status": "needs_review",
            "provenance": "agent_supplied", "source_verification": "unverified_external_agent"})
        status = self.tool(access, "get_draft_status", {"companyId": COMPANY_ID, "updateId": saved["updateId"]})
        self.assertEqual((status["updateId"], status["revisionId"]), (saved["updateId"], saved["revisionId"]))

        # 8. A later chat finds the draft in the brief and revises it in place.
        again = self.tool(access, "get_monthly_update_brief", {"companyId": COMPANY_ID, "month": "2026-10"})
        self.assertEqual(again["existingDrafts"], [{"updateId": saved["updateId"], "revisionId": saved["revisionId"],
            "status": "draft", "title": "October update", "updatedAt": NOW.isoformat()}])
        revised = self.tool(access, "save_narrative_draft", {"companyId": COMPANY_ID, "month": "2026-10", "requestId": str(uuid4()),
            "updateId": saved["updateId"], "expectedRevision": saved["revisionId"], "narrative": {"summary": "We shipped the pilot to two customers."}})
        self.assertEqual(revised["updateId"], saved["updateId"])
        self.assertNotEqual(revised["revisionId"], saved["revisionId"])
        self.assertEqual(len(self.drafts.rows), 1)

        # 9. Refresh without resource, resending the scopes first asked for.
        grant_id = cache.get(oauth.key("access", access))["grant_id"]
        cache.set(oauth.key("grant", grant_id), {**cache.get(oauth.key("grant", grant_id)), "expiresAt": int(time.time()) + 86400})
        refresh = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client["client_id"],
            "scope": scope + " offline_access"}
        status_code, rotated = self.token(**refresh)
        self.assertEqual(status_code, 200, rotated)
        self.assertNotEqual(rotated["refresh_token"], tokens["refresh_token"])
        self.assertEqual(rotated["scope"].split(), PULSE)
        self.assertGreater(cache.get(oauth.key("grant", grant_id))["expiresAt"], time.time() + 179 * 86400)

        # 10. The response was lost and the host retries: same successor, no revocation.
        status_code, replayed = self.token(**refresh)
        self.assertEqual(status_code, 200, replayed)
        self.assertEqual((replayed["access_token"], replayed["refresh_token"]), (rotated["access_token"], rotated["refresh_token"]))
        self.assertFalse(cache.get(oauth.key("grant", grant_id))["revoked"])

        # 11. Still works, on the successor and on the next rotation.
        self.assertIn("save_narrative_draft", [item["name"] for item in json.loads(self.rpc(rotated["access_token"], "tools/list").content)["result"]["tools"]])
        self.tool(rotated["access_token"], "get_draft_status", {"companyId": COMPANY_ID, "updateId": saved["updateId"]})
        status_code, third = self.token(grant_type="refresh_token", refresh_token=rotated["refresh_token"], client_id=client["client_id"])
        self.assertEqual(status_code, 200, third)
        self.tool(third["access_token"], "list_startups", {})
        # The browser sign-in that approved the connection was never consulted again.
        self.sessions.select_related.assert_not_called()


def tools_by_name():
    return {item["name"] for item in tools.TOOLS}


@override_settings(**UNIFIED)
class ConnectionLifetimeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.client = oauth.register_client({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "client_name": "Claude"})
        self.user = Obj(pk=7, auth_version=3, is_active=True)
        stack = ExitStack()
        self.addCleanup(stack.close)
        users = stack.enter_context(patch.object(oauth, "get_user_model"))
        users.return_value.objects.filter.return_value.first.return_value = self.user
        self.sessions = stack.enter_context(patch.object(oauth.CommunityChatAccountSession, "objects"))
        self.valid_session = stack.enter_context(patch.object(oauth, "_valid_session", return_value=True))
        stack.enter_context(patch.object(oauth, "_validate_device_owner"))
        self.membership = stack.enter_context(patch.object(oauth, "require_community_access"))
        self.company_for = stack.enter_context(patch.object(oauth, "company_for", return_value=Obj(pk=UUID(COMPANY_ID))))

    def stored(self, **changes):
        value = grant(client_id=self.client["client_id"], resource=config.mlai_url(), **changes)
        cache.set(oauth.key("grant", value["id"]), value, timeout=max(1, value["expiresAt"] - int(time.time())))
        return value

    def refresh(self, token, **changes):
        return oauth.token_exchange({"grant_type": "refresh_token", "refresh_token": token, "client_id": self.client["client_id"], **changes})

    def approve(self, scopes, selected=None, resource=None):
        resource = resource or config.mlai_url()
        intent = oauth.create_intent({"client_id": self.client["client_id"], "redirect_uri": self.client["redirect_uris"][0],
            "response_type": "code", "resource": resource, "scope": " ".join(scopes), "code_challenge": "x" * 43,
            "code_challenge_method": "S256", "state": "state"})
        url = oauth.approve_intent(intent["requestId"], user=self.user, session=Obj(pk="session", user_id=7),
            company_id=COMPANY_ID, approve=True, scopes=selected)
        return cache.get(oauth.key("grant", cache.get(oauth.key("code", parse_qs(urlsplit(url).query)["code"][0]))["grant_id"]))

    def test_either_pulse_permission_grants_the_whole_pulse_capability_on_mlai_only(self):
        for selected in (["startup:brief:read"], ["startup:draft:write"]):
            with self.subTest(selected=selected):
                self.assertEqual(self.approve(PULSE, selected)["scopes"], PULSE)
        # Even when the host asked for one half, and alongside other choices.
        self.assertEqual(self.approve(["startup:brief:read"], ["startup:brief:read"])["scopes"], PULSE)
        self.assertEqual(self.approve([*PULSE, "article:read"], ["article:read", "startup:brief:read"])["scopes"], ["article:read", *PULSE])
        # Nothing Pulse was chosen, so nothing Pulse is granted.
        self.assertEqual(self.approve([*PULSE, "article:read"], ["article:read"])["scopes"], ["article:read"])
        # The legacy resource still grants exactly what was requested.
        with override_settings(MLAI_MCP_ENABLED=False):
            self.assertEqual(self.approve(["startup:brief:read"], resource=config.mcp_url())["scopes"], ["startup:brief:read"])

    def test_new_grant_lasts_the_idle_period_and_each_refresh_restarts_it(self):
        now = int(time.time())
        with patch("time.time", return_value=now) as clock:
            value = self.approve(PULSE, PULSE)
            self.assertEqual(value["expiresAt"], now + 180 * 86400)
            tokens = oauth.issue_tokens(value["id"])
            for month in range(1, 9):  # Eight monthly updates, well past the first 180 days.
                clock.return_value = now + month * 31 * 86400
                tokens = self.refresh(tokens["refresh_token"])
                self.assertEqual(cache.get(oauth.key("grant", value["id"]))["expiresAt"], clock.return_value + 180 * 86400)
                self.assertEqual(oauth.authenticate_token(tokens["access_token"], resource=config.mlai_url()).user, self.user)
            # Left unused, it ends after the idle period.
            clock.return_value += 180 * 86400 + 1
            with self.assertRaises(oauth.OAuthError):
                self.refresh(tokens["refresh_token"])
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant(value["id"])

    @override_settings(MLAI_MCP_GRANT_IDLE_TTL_DAYS=45)
    def test_idle_period_is_configurable(self):
        self.assertAlmostEqual(self.approve(PULSE, PULSE)["expiresAt"], time.time() + 45 * 86400, delta=5)

    def test_grant_made_before_this_change_keeps_working_and_gains_sliding_expiry(self):
        # Thirty-day grant bound to a browser sign-in that has since been replaced.
        value = self.stored(id="older", scopes=["startup:brief:read"], expiresAt=int(time.time()) + 2 * 86400)
        self.valid_session.return_value = False
        self.sessions.select_related.return_value.filter.return_value.first.return_value = None
        tokens = oauth.issue_tokens("older")
        rotated = self.refresh(tokens["refresh_token"], resource=config.mlai_url())
        self.assertGreater(cache.get(oauth.key("grant", "older"))["expiresAt"], time.time() + 179 * 86400)
        self.assertEqual(rotated["scope"], "startup:brief:read")  # Existing grants are never widened.
        # Its listing entry had lapsed with the old thirty-day index; refresh restores it.
        self.assertEqual([row["id"] for row in oauth.grants_for(self.user, COMPANY_ID)], ["older"])
        self.sessions.select_related.assert_not_called()

    def test_community_and_personal_agent_grants_stay_bound_to_their_session(self):
        self.stored(id="community", scopes=["community:read", "startup:brief:read"])
        self.assertEqual(oauth.valid_grant("community").user, self.user)
        self.valid_session.return_value = False
        with self.assertRaises(AuthenticationFailed):
            oauth.valid_grant("community")

    def test_personal_agent_grant_keeps_its_fixed_lifetime(self):
        expires = int(time.time()) + 86400
        value = self.stored(id="dot", company_id=None, scopes=sorted(config.DOT_SCOPES), expiresAt=expires)
        self.assertEqual(oauth._slide_grant(value)["expiresAt"], expires)
        self.assertEqual(cache.get(oauth.key("grant", "dot"))["expiresAt"], expires)

    def test_replay_inside_grace_returns_the_same_successor_and_after_it_revokes(self):
        self.stored()
        tokens = oauth.issue_tokens("grant")
        rotated = self.refresh(tokens["refresh_token"])
        self.assertEqual(self.refresh(tokens["refresh_token"]), rotated)
        self.assertEqual(self.refresh(tokens["refresh_token"]), rotated)
        # Raw successor tokens are not readable from the cache without the spent token.
        self.assertNotIn(rotated["refresh_token"], str(cache.get(oauth.key("replay", tokens["refresh_token"]))))
        # Another registered client cannot use the window.
        other = oauth.register_client({"redirect_uris": ["https://other.example.test/callback"]})
        with self.assertRaises(oauth.OAuthError):
            oauth.token_exchange({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": other["client_id"]})
        self.assertFalse(cache.get(oauth.key("grant", "grant"))["revoked"])
        # Once the window has passed, reuse is treated as a stolen token.
        cache.delete(oauth.key("replay", tokens["refresh_token"]))
        with self.assertRaises(oauth.OAuthError):
            self.refresh(tokens["refresh_token"])
        with self.assertRaises(AuthenticationFailed):
            oauth.authenticate_token(rotated["access_token"])

    def test_replay_is_refused_once_the_connection_was_disconnected(self):
        self.stored()
        tokens = oauth.issue_tokens("grant")
        self.refresh(tokens["refresh_token"])
        oauth.disconnect_company(self.user, COMPANY_ID)
        with self.assertRaises(AuthenticationFailed):
            self.refresh(tokens["refresh_token"])

    def test_stale_refresh_lock_clears_in_seconds(self):
        self.stored()
        tokens = oauth.issue_tokens("grant")
        now = time.time()
        with patch("time.time", return_value=now) as clock, patch.object(oauth, "REFRESH_LOCK_WAIT", 0):
            cache.add(oauth.key("lock-refresh", tokens["refresh_token"]), "worker-that-died", timeout=oauth.REFRESH_LOCK_TTL)
            with self.assertRaises(oauth.OAuthError) as caught:
                self.refresh(tokens["refresh_token"])
            self.assertEqual(caught.exception.error, "temporarily_unavailable")
            clock.return_value = now + oauth.REFRESH_LOCK_TTL + 1
            self.assertIn("access_token", self.refresh(tokens["refresh_token"]))

    def test_token_resource_may_be_absent_or_slashed_but_never_different(self):
        value = self.stored()
        for index, resource in enumerate((None, config.mlai_url(), config.mlai_url() + "/")):
            with self.subTest(resource=resource):
                code = f"code-{index}"
                cache.set(oauth.key("code", code), {"grant_id": "grant", "client_id": self.client["client_id"],
                    "redirect_uri": self.client["redirect_uris"][0], "code_challenge": base64.urlsafe_b64encode(
                        hashlib.sha256(b"v" * 43).digest()).rstrip(b"=").decode(), "resource": value["resource"]})
                tokens = oauth.token_exchange({"grant_type": "authorization_code", "code": code, "client_id": self.client["client_id"],
                    "redirect_uri": self.client["redirect_uris"][0], "code_verifier": "v" * 43, **({"resource": resource} if resource else {})})
                self.assertIn("access_token", self.refresh(tokens["refresh_token"], **({"resource": resource} if resource else {})))
        tokens = oauth.issue_tokens("grant")
        for wrong, error in ((config.mcp_url(), "invalid_target"), ("https://attacker.test/mcp", "invalid_target")):
            with self.subTest(wrong=wrong), self.assertRaises(oauth.OAuthError) as caught:
                self.refresh(tokens["refresh_token"], resource=wrong)
            self.assertEqual(caught.exception.error, error)
        self.assertNotIn("spent", cache.get(oauth.key("refresh", tokens["refresh_token"])))

    def test_disconnect_marker_outlives_later_connections(self):
        now = int(time.time())
        with patch("time.time", return_value=now) as clock:
            first = self.approve(PULSE, PULSE)
            oauth.disconnect_company(self.user, COMPANY_ID)
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant(first["id"])
            second = self.approve(PULSE, PULSE)
            tokens = oauth.issue_tokens(second["id"])
            # The old marker lapsed thirty days after the disconnect and took later connections with it.
            clock.return_value = now + 60 * 86400
            self.assertEqual(oauth.valid_grant(second["id"]).user, self.user)
            self.assertIn("access_token", self.refresh(tokens["refresh_token"]))
            with self.assertRaises(AuthenticationFailed):
                oauth.valid_grant(first["id"])

    def test_marker_written_before_this_change_is_kept_when_its_grant_is_used(self):
        subject = f"7:{COMPANY_ID}"
        now = int(time.time())
        with patch("time.time", return_value=now) as clock:
            cache.set(oauth.key("epoch", subject), "marker", timeout=30 * 86400 + 1)
            self.stored(revocation_epoch="marker", expiresAt=now + 29 * 86400)
            tokens = oauth.issue_tokens("grant")
            clock.return_value = now + 86400
            tokens = self.refresh(tokens["refresh_token"])
            clock.return_value = now + 90 * 86400
            self.assertEqual(oauth.valid_grant("grant").user, self.user)

    def test_revocation_survives_a_racing_refresh_that_rewrites_the_grant(self):
        value = self.stored()
        oauth.revoke_grant("grant")
        cache.set(oauth.key("grant", "grant"), {**value, "revoked": False, "expiresAt": int(time.time()) + 180 * 86400})
        with self.assertRaises(AuthenticationFailed):
            oauth.valid_grant("grant")


@override_settings(**UNIFIED)
class RealReasonTests(SimpleTestCase):
    """Account and startup problems say what they are, not "missing permission"."""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.user = Obj(pk=7, auth_version=3, is_active=True)
        stack = ExitStack()
        self.addCleanup(stack.close)
        users = stack.enter_context(patch.object(oauth, "get_user_model"))
        users.return_value.objects.filter.return_value.first.return_value = self.user
        self.membership = stack.enter_context(patch.object(oauth, "require_community_access"))
        self.companies = stack.enter_context(patch.object(oauth.VibeRaisingCompany, "objects"))
        self.companies.select_related.return_value.filter.return_value.first.return_value = Obj(
            pk=UUID(COMPANY_ID), name="Example", organization=Obj(pk=8))
        self.client = oauth.register_client({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
        cache.set(oauth.key("grant", "grant"), grant(resource=config.mlai_url(), client_id=self.client["client_id"]))
        self.access = oauth.issue_tokens("grant")

    def post(self, body):
        return views.MlaiMcpView.as_view(throttle_classes=())(self.factory.post("/mcp/mlai", body, format="json",
            HTTP_ACCEPT=ACCEPT, HTTP_AUTHORIZATION=f"Bearer {self.access['access_token']}"))

    def test_membership_and_startup_problems_are_named_without_a_permission_challenge(self):
        cases = (
            ("community_membership_required", "membership is not approved", lambda: setattr(self.membership, "side_effect", PermissionDenied({"error": "onboarding_required"}))),
            ("startup_unavailable", "startup is unavailable", lambda: (setattr(self.membership, "side_effect", None),
                setattr(self.companies.select_related.return_value.filter.return_value.first, "return_value", None))),
        )
        for code, text, arrange in cases:
            arrange()
            with self.subTest(code=code):
                with self.assertRaises(oauth.AccessProblem) as caught:
                    oauth.valid_grant("grant")
                self.assertEqual(caught.exception.reason_code, code)
                response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(response.status_code, 403)
                body = json.loads(response.content)
                self.assertEqual(body["error"], code)
                self.assertIn(text, body["error_description"])
                self.assertNotIn("WWW-Authenticate", response)
                refreshed = views.TokenView.as_view(throttle_classes=())(self.factory.post("/mcp/oauth/token", urlencode({
                    "grant_type": "refresh_token", "refresh_token": self.access["refresh_token"], "client_id": self.client["client_id"]}),
                    content_type="application/x-www-form-urlencoded"))
                self.assertEqual(refreshed.status_code, 400)
                token_error = json.loads(refreshed.content)
                self.assertEqual((token_error["error"], token_error["reason"]), ("invalid_grant", code))
                self.assertIn(text, token_error["error_description"])

    def test_tool_call_for_another_startup_explains_itself_without_a_reconnect_prompt(self):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_draft_status", "arguments": {"companyId": "7e399ce7-8d85-4970-ad8e-aa13bc3b028c", "updateId": 3}}})
        result = json.loads(response.content)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "startup_not_authorised")
        self.assertIn("different startup", result["content"][0]["text"])
        self.assertNotIn("_meta", result)

    def test_a_missing_permission_still_gets_the_step_up_challenge(self):
        with patch.object(registry, "call", side_effect=PermissionDenied("Reconnect MLAI with permission for this capability.")):
            result = json.loads(self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "save_narrative_draft", "arguments": {}}}).content)["result"]
        self.assertIn('error="insufficient_scope"', result["_meta"]["mcp/www_authenticate"][0])
        self.assertIn('scope="startup:draft:write"', result["_meta"]["mcp/www_authenticate"][0])

    def test_unauthenticated_challenge_names_pulse_scopes_on_both_servers(self):
        for view, path in ((views.MlaiMcpView, "mlai"), (views.McpView, "valley")):
            for method in ("get", "post"):
                with self.subTest(path=path, method=method):
                    response = view.as_view(throttle_classes=())(getattr(self.factory, method)(f"/mcp/{path}"))
                    self.assertEqual(response.status_code, 401)
                    self.assertIn(f'resource_metadata="https://api.example.test/.well-known/oauth-protected-resource/mcp/{path}"', response["WWW-Authenticate"])
                    self.assertIn('scope="startup:brief:read startup:draft:write"', response["WWW-Authenticate"])
        self.assertIn("Connect this agent to MLAI first.", json.loads(views.MlaiMcpView.as_view(throttle_classes=())(
            self.factory.post("/mcp/mlai")).content)["error_description"])

    def test_authorize_error_is_a_readable_page_in_a_browser_and_json_elsewhere(self):
        view = views.AuthorizeView.as_view(throttle_classes=())
        query = {"client_id": "valley_client_<script>alert(1)</script>", "response_type": "code"}
        page = view(self.factory.get("/mcp/oauth/authorize", query, HTTP_ACCEPT="text/html,application/xhtml+xml,*/*;q=0.8"))
        self.assertEqual(page.status_code, 400)
        self.assertEqual(page["Content-Type"], "text/html; charset=utf-8")
        html = page.content.decode()
        self.assertIn("MLAI couldn't start this connection", html)
        self.assertIn("Remove MLAI from your AI app, add it again", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("{", html.split("</style>")[1])
        self.assertEqual(page["Cache-Control"], "no-store")
        with override_settings(MLAI_MCP_ENABLED=False, VALLEY_MCP_ENABLED=False):
            off = view(self.factory.get("/mcp/oauth/authorize", query, HTTP_ACCEPT="text/html,*/*;q=0.8"))
            self.assertEqual((off.status_code, off["Content-Type"]), (404, "text/html; charset=utf-8"))
            self.assertIn("connect to AI apps right now", off.content.decode())
            self.assertEqual(view(self.factory.get("/mcp/oauth/authorize", query, HTTP_ACCEPT="application/json")).status_code, 404)
        for accept in ("application/json", "*/*"):
            with self.subTest(accept=accept):
                response = view(self.factory.get("/mcp/oauth/authorize", query, HTTP_ACCEPT=accept))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.content)["error"], "invalid_client")


@override_settings(**UNIFIED)
class ConsentEligibilityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.user, self.session = account()
        self.client = oauth.register_client({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "client_name": "Claude"})
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.member = stack.enter_context(patch.object(agent_connections, "has_community_access", return_value=True))
        self.companies = stack.enter_context(patch.object(agent_connections.VibeRaisingCompany, "objects"))
        self.owned, self.unfinished = [], []
        self.companies.filter.side_effect = lambda **query: MagicMock(order_by=MagicMock(
            return_value=self.unfinished if query.get("organization__isnull") else self.owned))

    def intent(self, scope="startup:brief:read startup:draft:write"):
        return oauth.create_intent({"client_id": self.client["client_id"], "redirect_uri": self.client["redirect_uris"][0],
            "response_type": "code", "resource": config.mlai_url(), "scope": scope, "code_challenge": "x" * 43,
            "code_challenge_method": "S256", "state": "state"})["requestId"]

    def call(self, request_id, method="get", body=None):
        request = getattr(self.factory, method)(f"/authorization/{request_id}/", body, format="json")
        force_authenticate(request, user=self.user, token=self.session)
        return agent_connections.AgentAuthorizationView.as_view()(request, request_id=request_id)

    def test_member_awaiting_approval_is_told_before_approving_and_cannot_approve(self):
        self.member.return_value = False
        self.owned.append(Obj(pk=UUID(COMPANY_ID), name="Example"))
        request_id = self.intent()
        screen = self.call(request_id)
        self.assertEqual(screen.status_code, 200)
        self.assertEqual(screen.data["eligibility"], {"ok": False, "reason": agent_connections.MEMBERSHIP_REASON,
            "code": "community_membership_required"})
        # Existing fields keep their shape for clients that predate eligibility.
        for field in ("resource", "capabilities", "nativeInstallations", "communityChannels", "requestId", "clientName",
                      "scopes", "redirectOrigin", "expiresAt", "companies"):
            self.assertIn(field, screen.data)
        with patch.object(oauth, "approve_intent") as approve:
            refused = self.call(request_id, "post", {"requestId": request_id, "approve": True, "companyId": COMPANY_ID, "scopes": PULSE})
            self.assertEqual(refused.status_code, 400)
            self.assertIn("membership isn't approved", str(refused.data))
            approve.assert_not_called()
            # Declining still returns the AI app to a clean "access denied".
            approve.return_value = "https://claude.ai/api/mcp/auth_callback?error=access_denied"
            self.assertEqual(self.call(request_id, "post", {"requestId": request_id, "approve": False}).status_code, 200)

    def test_pending_members_reach_the_screen_through_onboarding_authentication(self):
        from community_chat.authentication import CommunityChatOnboardingAuthentication
        self.assertEqual(agent_connections.AgentAuthorizationView.authentication_classes, (CommunityChatOnboardingAuthentication,))
        # Every other startup endpoint, the draft request included, stays members-only.
        self.assertFalse(agent_connections.AgentDraftTaskView.authentication_classes[0].allow_pending_membership)
        self.assertFalse(agent_connections.AgentConnectionView.authentication_classes[0].allow_pending_membership)

    def test_no_startup_and_startups_that_cannot_be_chosen_carry_plain_reasons(self):
        screen = self.call(self.intent())
        self.assertEqual(screen.data["eligibility"], {"ok": False, "reason": agent_connections.NO_STARTUP_REASON, "code": "no_startup"})
        self.assertEqual(screen.data["unavailableCompanies"], [])
        self.unfinished.append(Obj(pk=UUID(COMPANY_ID), name="Half set up"))
        team = [{"id": "7e399ce7-8d85-4970-ad8e-aa13bc3b028c", "name": "Someone else's", "role": "member"},
                {"id": COMPANY_ID, "name": "Listed once"}]
        with override_settings(MY_STARTUP_TEAM_ENABLED=True), patch("founder_tools.startup_team.member_companies", return_value=team):
            screen = self.call(self.intent())
        self.assertEqual(screen.data["companies"], [])
        self.assertEqual(screen.data["unavailableCompanies"], [
            {"id": COMPANY_ID, "name": "Half set up", "reason": agent_connections.NOT_SET_UP_REASON},
            {"id": "7e399ce7-8d85-4970-ad8e-aa13bc3b028c", "name": "Someone else's", "reason": agent_connections.TEAM_MEMBER_REASON}])
        self.assertEqual(screen.data["eligibility"], {"ok": False, "reason": agent_connections.STARTUPS_BLOCKED_REASON, "code": "no_startup"})
        for row in screen.data["unavailableCompanies"]:
            self.assertNotRegex(row["reason"], r"(?i)valley|mcp|agent|organi[sz]ation")
        # A failing team lookup never stops the screen loading.
        with override_settings(MY_STARTUP_TEAM_ENABLED=True), patch("founder_tools.startup_team.member_companies", side_effect=RuntimeError):
            self.assertEqual(self.call(self.intent()).status_code, 200)

    def test_request_that_can_proceed_without_a_startup_stays_eligible(self):
        with patch("startup_updates.mcp.community.consent_channels", return_value=[]):
            screen = self.call(self.intent("startup:brief:read community:read"))
        self.assertTrue(screen.data["eligibility"]["ok"])


@override_settings(**UNIFIED)
class BriefAndSchemaTests(SimpleTestCase):
    def test_existing_drafts_lists_unpublished_work_for_the_month_newest_first(self):
        stamp = datetime(2026, 10, 9, 1, 0, tzinfo=datetime_timezone.utc)
        # The database leaves out fully published updates before the newest 20 are taken.
        rows = [
            Obj(pk=3, current_revision_id=30, published_revision_id=None, published_at=None, status="draft", title="October update #2", updated_at=stamp),
            Obj(pk=4, current_revision_id=41, published_revision_id=40, published_at=stamp, status="draft", title="October update #3", updated_at=stamp),
            Obj(pk=5, current_revision_id=None, published_revision_id=None, published_at=None, status="draft", title="October update #4", updated_at=None),
        ]
        company = Obj(organization=Obj(pk=8))
        with patch.object(tools.MonthlyUpdateDraft, "objects") as drafts:
            unpublished = drafts.filter.return_value.filter
            unpublished.return_value.order_by.return_value.__getitem__.return_value = rows
            value = tools.existing_drafts(company, date(2026, 10, 1))
            drafts.filter.assert_called_once_with(organization=company.organization, month=date(2026, 10, 1))
            where = str(unpublished.call_args.args[0])
            for clause in ("published_at__isnull", "current_revision__isnull", "published_revision__isnull", "F(published_revision)"):
                self.assertIn(clause, where)
            unpublished.return_value.order_by.assert_called_once_with("-updated_at", "-pk")
            unpublished.return_value.order_by.return_value.__getitem__.assert_called_once_with(slice(None, 20))
        self.assertEqual(value, [
            {"updateId": 3, "revisionId": 30, "status": "draft", "title": "October update #2", "updatedAt": stamp.isoformat()},
            {"updateId": 4, "revisionId": 41, "status": "draft", "title": "October update #3", "updatedAt": stamp.isoformat()},
            {"updateId": 5, "revisionId": None, "status": "draft", "title": "October update #4", "updatedAt": None}])

    def test_identity_fields_are_described_and_new_versus_revise_is_explained(self):
        save = next(item for item in tools.TOOLS if item["name"] == "save_narrative_draft")
        properties = save["inputSchema"]["properties"]
        for field in ("requestId", "updateId", "expectedRevision"):
            self.assertGreater(len(properties[field]["description"]), 40, field)
        self.assertIn("revisionId", properties["expectedRevision"]["description"])
        self.assertIn("omit updateId and expectedRevision", save["description"])
        self.assertIn("Revise an existing draft", save["description"])
        self.assertEqual(save["inputSchema"]["required"], ["companyId", "month", "requestId", "narrative"])
        self.assertIn("existingDrafts", next(item for item in tools.TOOLS if item["name"] == "get_monthly_update_brief")["description"])
        # Financial metric fields remain refused.
        with self.assertRaises(ValidationError):
            tools.validate_save({"companyId": COMPANY_ID, "month": "2026-10", "requestId": str(uuid4()),
                "narrative": {"summary": "Up"}, "metrics": {"revenue": 1}})

    def test_brief_text_names_the_installed_service_and_explains_existing_drafts(self):
        company = Obj(pk=UUID(COMPANY_ID), name="Example", organization=Obj(pk=8, domain="example.test"))
        existing = [{"updateId": 3, "revisionId": 30, "status": "draft", "title": "October update", "updatedAt": None}]
        for resource, brand, absent in ((config.mlai_url(), "MLAI", "Valley"), (config.mcp_url(), "Valley", "MLAI from")):
            principal = oauth.Principal(user=Obj(pk=7), grant=grant(resource=resource))
            with self.subTest(brand=brand), patch.object(tools, "valid_grant", return_value=principal), patch.object(tools, "company_for", return_value=company), \
                    patch.object(tools.StartupProfile, "objects") as profiles, patch.object(tools, "existing_drafts", return_value=existing), \
                    patch.object(tools.timezone, "now", return_value=NOW):
                profiles.filter.return_value.first.return_value = None
                brief = tools.call_tool(principal, "get_monthly_update_brief", {"companyId": COMPANY_ID, "month": "2026-10"})
                text = " ".join(brief["instructions"])
                self.assertEqual(brief["existingDrafts"], existing)
                self.assertIn(f"attached by {brand} from the founder's connected Xero and Stripe", text)
                self.assertNotIn(absent, text)
                self.assertIn("pass its updateId, and its revisionId as expectedRevision", text)
                catalogue = json.dumps([item for item in registry.catalogue(principal) if item["name"] in tools_by_name()])
                self.assertIn(brand, catalogue)
                self.assertNotIn(absent, catalogue)

    def test_a_whole_month_that_ends_daylight_saving_is_an_accepted_window(self):
        # October 2026 in Berlin is 31 days and one hour long.
        window = {"startInclusive": "2026-10-01T00:00:00+02:00", "endExclusive": "2026-11-01T00:00:00+01:00"}
        later = datetime(2026, 11, 5, tzinfo=datetime_timezone.utc)
        period = {"start": "x", "cutoff": "y"}
        self.assertEqual(tools._activity_window(window, period, later)["endExclusive"], window["endExclusive"])


@override_settings(**UNIFIED)
class DraftTaskTests(SimpleTestCase):
    def setUp(self):
        self.company = Obj(pk=UUID(COMPANY_ID), name='Example "Startup"', organization=Obj(pk=8))
        self.melbourne = ZoneInfo("Australia/Melbourne")

    def task(self, **changes):
        return draft_task.build_task(**{"company": self.company, "month": "2026-09", "zone": self.melbourne, "now": NOW,
            "request_id": "8ec75f46-340a-46fb-9aa2-b99e8974d99b", **changes})

    def test_finished_month_covers_the_whole_month_in_the_founder_timezone(self):
        task = self.task()
        self.assertEqual(set(task), {"prompt", "month", "windowStart", "windowEnd", "requestId", "serviceName"})
        self.assertEqual((task["month"], task["serviceName"]), ("2026-09", "MLAI"))
        self.assertEqual((task["windowStart"], task["windowEnd"]), ("2026-09-01T00:00:00+10:00", "2026-10-01T00:00:00+10:00"))
        prompt = task["prompt"]
        context = json.loads(prompt.split("Context: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(context, {"companyId": COMPANY_ID, "companyName": 'Example "Startup"', "month": "2026-09",
            "requestId": task["requestId"], "activityWindow": {"startInclusive": task["windowStart"], "endExclusive": task["windowEnd"]}})
        self.assertIn("The window covers the whole of September 2026.", prompt)
        self.assertNotIn("rolling", prompt)
        self.assertNotIn("Valley", prompt)
        self.assertIn("existingDrafts", prompt)
        self.assertIn("omit updateId and expectedRevision", prompt)
        self.assertIn("MLAI attaches the financial charts and figures itself, from my Xero and Stripe connected to MLAI", prompt)
        self.assertIn("Do not send revenue, costs, cash or other financial figures as metrics", prompt)
        self.assertIn("in words only if my own tools show it", prompt)
        # The brief accepts exactly this window.
        self.assertEqual(tools._activity_window(context["activityWindow"], {}, NOW)["startInclusive"], task["windowStart"])

    def test_current_month_is_cut_off_at_now_and_says_so(self):
        task = self.task(month="2026-10")
        self.assertEqual((task["windowStart"], task["windowEnd"]), ("2026-10-01T00:00:00+10:00", "2026-10-20T15:00:00+11:00"))
        self.assertIn("covers October 2026 to date", task["prompt"])

    def test_future_months_are_refused_in_both_timezones(self):
        with self.assertRaises(ValidationError):
            self.task(month="2026-11")
        # 1 November in Melbourne while the startup's reporting timezone is still in October.
        early = datetime(2026, 10, 31, 20, 0, tzinfo=datetime_timezone.utc)
        with self.assertRaises(ValidationError):
            self.task(month="2026-11", now=early, reporting_zone=ZoneInfo("UTC"))
        self.assertEqual(self.task(month="2026-11", now=early)["windowStart"], "2026-11-01T00:00:00+11:00")
        for month in ("2026-13", "October", None):
            with self.subTest(month=month), self.assertRaises(ValidationError):
                self.task(month=month)

    def test_open_update_is_revised_in_place(self):
        prompt = self.task(update=Obj(pk=41), expected_revision=507)["prompt"]
        self.assertTrue(prompt.startswith("Update my private Pulse monthly update draft in MLAI"))
        self.assertIn("pass updateId 41 and expectedRevision 507 exactly", prompt)
        context = json.loads(prompt.split("Context: ", 1)[1].split("\n", 1)[0])
        self.assertEqual((context["updateId"], context["expectedRevision"]), (41, 507))
        empty = self.task(update=Obj(pk=41))["prompt"]
        self.assertIn("pass updateId 41 exactly. It has no saved text yet, so omit expectedRevision", empty)
        self.assertNotIn("expectedRevision", json.loads(empty.split("Context: ", 1)[1].split("\n", 1)[0]))

    def test_legacy_service_name_and_timezone_fallback(self):
        with override_settings(MLAI_MCP_ENABLED=False):
            task = self.task()
        self.assertEqual(task["serviceName"], "Valley")
        self.assertIn("draft in Valley", task["prompt"])
        self.assertEqual(draft_task.zone_for("Not/AZone", "", None, "Australia/Melbourne").key, "Australia/Melbourne")
        self.assertEqual(draft_task.zone_for("../../etc/passwd").key, "UTC")

    def call(self, body, drafts=()):
        user, session = account()
        request = APIRequestFactory().post(f"/api/v1/community-chat/startups/agent-draft-task/?company_id={COMPANY_ID}", body, format="json")
        force_authenticate(request, user=user, token=session)
        with patch.object(agent_connections, "get_object_or_404", create=True), patch("community_chat.startups.views.get_object_or_404", return_value=self.company), \
                patch("startup_updates.models.StartupProfile.objects") as profiles, patch("startup_updates.models.MonthlyUpdateDraft.objects", Drafts(list(drafts))), \
                patch("django.utils.timezone.now", return_value=NOW):
            profiles.filter.return_value.first.return_value = Obj(reporting_timezone="Australia/Melbourne")
            return agent_connections.AgentDraftTaskView.as_view()(request)

    def test_endpoint_returns_the_contract_and_binds_an_open_update_to_its_saved_revision(self):
        response = self.call({"month": "2026-09", "timezone": "Australia/Perth"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {"prompt", "month", "windowStart", "windowEnd", "requestId", "serviceName"})
        self.assertEqual(response.data["windowStart"], "2026-09-01T00:00:00+08:00")
        UUID(response.data["requestId"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        draft = Obj(pk=41, month=date(2026, 9, 1), current_revision_id=507)
        revise = self.call({"month": "2026-09", "updateId": 41, "expectedRevision": 3}, [draft])
        # The server's saved revision wins over a stale client value.
        self.assertIn("pass updateId 41 and expectedRevision 507 exactly", revise.data["prompt"])
        self.assertEqual(self.call({"month": "2026-09", "updateId": 99}, [draft]).status_code, 404)
        self.assertEqual(self.call({"month": "2026-10", "updateId": 41}, [draft]).status_code, 400)
        for body in ({}, {"month": "2026-11"}, {"month": "2026-09", "updateId": True}):
            with self.subTest(body=body):
                self.assertEqual(self.call(body, [draft]).status_code, 400)

    def test_endpoint_is_routed_beside_the_other_startup_apis(self):
        from django.urls import resolve
        match = resolve("/api/v1/community-chat/startups/agent-draft-task/")
        self.assertIs(match.func.view_class, agent_connections.AgentDraftTaskView)
        self.assertTrue(agent_connections.AgentDraftTaskView.requires_company)
