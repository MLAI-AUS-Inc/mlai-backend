"""P1.14: GitHub callback reasons for new-journey Chat flows, and G7 for everyone else.

Every failure branch of ``github_callback`` and ``connect_browser`` returns a
302 into the app with a typed reason for a new-journey Chat state; bad Chat
states go only to the fixed route; and every other flow (flag off, legacy
organisations with every P1 flag on, Slack, user-level OAuth, a missing or a
non-chat state) keeps today's responses byte for byte.
"""
import base64
import json
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.core import signing
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from community_chat.models import CommunityChatAccountSession
from community_chat.startups import connections
from content_factory.founder_journey_cohort import read_marker
from content_factory.founder_journey_testing import ALL_P1_FLAGS_ON, make_named_legacy_org, make_new_journey_org
from integrations import http_client
from integrations.services import github_callback_reasons as reasons
from integrations.services.github_connections import build_github_oauth_state

FRONTEND = "https://chat.example"
FIXED_ROUTE = FRONTEND + "/my-startup/website?github=state_expired"
FLAG_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED": False}
# The V2 kill switch (and production between turn-on steps 5 and 6): every P1 flag on but V2.
V2_OFF = {**ALL_P1_FLAGS_ON, "MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED": False}
TODAY_BAD_STATE = (400, b"Invalid or expired state", None)
COMMON = {
    "CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    "COMMUNITY_CHAT_FRONTEND_URL": FRONTEND,
    "DEFAULT_FRONTEND_URL": "https://website.example",
    "CONTENT_FACTORY_FRONTEND_URL": "",
    "GITHUB_OAUTH_CLIENT_ID": "synthetic-client",
    "GITHUB_OAUTH_CLIENT_SECRET": "synthetic-secret",
    "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED": True,
}
SECRET_DESCRIPTION = "synthetic-provider-description-never-shown"


def _json(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = http_client.exceptions.HTTPError(str(status), response=response)
    else:
        response.raise_for_status.return_value = None
    return response


def _snapshot(response):
    return response.status_code, bytes(response.content), response.get("Location")


def _query(response):
    return {key: values[0] for key, values in parse_qs(urlsplit(response["Location"]).query).items()}


TOKEN = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600}
USER = {"login": "founder"}


def _repos(*names, push=True):
    return {"repositories": [{"full_name": name, "owner": {"login": name.split("/")[0]},
                              "permissions": {"push": push, "admin": False, "pull": True}} for name in names]}


@override_settings(**COMMON, **ALL_P1_FLAGS_ON)
class CallbackReasonTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.new = make_new_journey_org(domain="banksia.studio")

    def chat_state(self, built, *, domain=None, return_url=None, organization_id=None, native=False):
        company = built.company
        if return_url is None:
            return_url = (f"mlaichat://connections?company_id={company.pk}&provider=github" if native
                          else f"{FRONTEND}/my-startup/website?company_id={company.pk}")
        return build_github_oauth_state(
            domain=domain or built.organization.domain, slack_user_id=f"user:{built.owner.pk}", return_url=return_url,
            chat_context={"user_id": built.owner.pk, "chat_company_id": str(company.pk), "chat_session_id": "session",
                          "organization_id": organization_id or built.organization.pk})

    @contextmanager
    def github(self, *, post=None, get=None):
        with ExitStack() as stack:
            stack.enter_context(patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=True))
            stack.enter_context(patch("integrations.services.github_installations.upsert_github_installation"))
            stack.enter_context(patch("integrations.services.slack.SlackService.send_dm"))
            stack.enter_context(patch("integrations.services.github.trigger_scan_async"))
            self.verify = stack.enter_context(patch("content_factory.website_connections.verify_repository_access",
                return_value={"repository_id": 4242, "github_repo": "founder/banksia-studio", "installation_id": "77", "branch": "main"}))
            self.prepare = stack.enter_context(patch("content_factory.website_prepare.start_prepare", return_value=Obj(pk=1)))
            stack.enter_context(patch("content_factory.website_prepare.advance_prepare"))
            self.post = stack.enter_context(patch.object(http_client, "post", side_effect=post if isinstance(post, list) else None,
                                                          return_value=post if not isinstance(post, list) else None))
            self.get = stack.enter_context(patch.object(http_client, "get", side_effect=get))
            yield

    def callback(self, params, *, post=None, get=None):
        post = _json(TOKEN) if post is None else post
        get = [_json(USER), _json(_repos("founder/banksia-studio"))] if get is None else get
        with self.github(post=post, get=get):
            return self.client.get(reverse("github_callback"), params)

    def params(self, state, **extra):
        values = {"code": "synthetic-code", "installation_id": "77", "setup_action": "install", "state": state.raw}
        values.update(extra)
        return {key: value for key, value in values.items() if value is not None}

    def assertReason(self, response, reason, built=None):
        built = built or self.new
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.get("Content-Type", ""), "text/plain")
        location = response["Location"]
        parts = urlsplit(location)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", FRONTEND + "/my-startup/website")
        self.assertEqual(_query(response), {"company_id": str(built.company.pk), "github": reason})
        self.assertIn(reason, reasons.REASONS)
        self.assertNotIn(SECRET_DESCRIPTION, location)
        self.assertEqual(response["Cache-Control"], "no-store")


class NewJourneyCallbackBranchTests(CallbackReasonTestBase):
    """Each failure branch of github_callback, for a new-journey Chat state, returns a typed 302."""

    def test_every_callback_failure_branch_redirects_with_a_reason(self):
        timeout = http_client.exceptions.ConnectionError("offline")
        cases = [
            ("missing code", {"code": None}, None, None, reasons.AUTHORIZATION_FAILED),
            ("cancelled on GitHub", {"code": None, "error": "access_denied", "error_description": SECRET_DESCRIPTION},
             None, None, reasons.CANCELLED),
            ("token exchange offline", {}, [timeout], None, reasons.UNAVAILABLE),
            ("token exchange refused", {}, _json({}, status=401), None, reasons.AUTHORIZATION_FAILED),
            ("provider error", {}, _json({"error": "bad_verification_code", "error_description": SECRET_DESCRIPTION}),
             None, reasons.AUTHORIZATION_FAILED),
            ("no token", {}, _json({"refresh_token": "only"}), None, reasons.AUTHORIZATION_FAILED),
            ("user fetch offline", {}, None, [timeout], reasons.UNAVAILABLE),
            ("user fetch refused", {}, None, [_json({}, status=401)], reasons.AUTHORIZATION_FAILED),
            ("repository access refused", {}, None, [_json(USER), _json({}, status=403)], reasons.REPOSITORY_ACCESS),
            ("repository listing malformed", {}, None, [_json(USER), _json({"repositories": "x"})], reasons.REPOSITORY_ACCESS),
            ("no push repository", {}, None, [_json(USER), _json(_repos("founder/banksia-studio", push=False))], reasons.NO_REPO),
            ("several repositories", {}, None, [_json(USER), _json(_repos("founder/one", "founder/two"))], reasons.MULTIPLE_REPOS),
        ]
        for label, extra, post, get, expected in cases:
            with self.subTest(label):
                cache.clear()
                response = self.callback(self.params(self.chat_state(self.new), **extra), post=post, get=get)
                self.assertReason(response, expected)

    def test_success_binds_and_redirects_connected(self):
        response = self.callback(self.params(self.chat_state(self.new)))
        self.assertReason(response, reasons.CONNECTED)
        self.assertEqual(response["Location"],
                         f"{FRONTEND}/my-startup/website?company_id={self.new.company.pk}&github=connected")

    def test_startup_changed(self):
        response = self.callback(self.params(self.chat_state(self.new, domain="moved.example")))
        self.assertReason(response, reasons.STARTUP_CHANGED)

    def test_owned_elsewhere_never_reveals_the_other_owner(self):
        self.new.config.connected_slack_user_id = "user:someone-else-entirely"
        self.new.config.save(update_fields=["connected_slack_user_id"])
        response = self.callback(self.params(self.chat_state(self.new)))
        self.assertReason(response, reasons.OWNED_ELSEWHERE)
        self.assertNotIn("someone-else", response["Location"])
        self.assertEqual(response.content, b"")

    def test_setup_action_request_is_owner_approval_before_the_installation_check(self):
        response = self.callback(self.params(self.chat_state(self.new), setup_action="request",
                                             installation_id=None, code=None))
        self.assertReason(response, reasons.OWNER_APPROVAL_REQUESTED)
        self.post.assert_not_called()

    def test_missing_installation_resolves_from_user_installations(self):
        get = [_json(USER), _json({"installations": [{"id": 11}, {"id": 77}]}),
               _json(_repos("founder/unrelated")), _json(_repos("founder/banksia-studio")),
               _json(_repos("founder/banksia-studio"))]
        response = self.callback(self.params(self.chat_state(self.new), installation_id=None), get=get)
        self.assertReason(response, reasons.CONNECTED)
        urls = [call.args[0] for call in self.get.call_args_list]
        self.assertIn("https://api.github.com/user/installations", urls)
        self.assertEqual(urls[-1], "https://api.github.com/user/installations/77/repositories")
        self.new.config.refresh_from_db()
        self.assertEqual(self.new.config.github_installation_id, "77")

    def test_missing_installation_without_any_gives_no_repo(self):
        get = [_json(USER), _json({"installations": []})]
        response = self.callback(self.params(self.chat_state(self.new), installation_id=None), get=get)
        self.assertReason(response, reasons.NO_REPO)
        self.new.config.refresh_from_db()
        self.assertFalse(self.new.config.github_token_encrypted)

    def test_no_branch_returns_text_or_echoes_error_description(self):
        state = self.chat_state(self.new)
        response = self.callback(self.params(state), post=_json({"error": "x", "error_description": SECRET_DESCRIPTION}))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(SECRET_DESCRIPTION.encode(), response.content)
        self.assertNotIn(SECRET_DESCRIPTION, response["Location"])

    def test_desktop_return_keeps_the_native_link_and_stores_the_reason(self):
        state = self.chat_state(self.new, native=True)
        response = self.callback(self.params(state), get=[_json(USER), _json({}, status=403)])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"mlaichat://connections?company_id={self.new.company.pk}&provider=github")
        marker = read_marker(self.new.organization)
        self.assertEqual(marker["githubReturn"]["reason"], reasons.REPOSITORY_ACCESS)
        self.assertTrue(marker["githubReturn"]["at"])

    def test_logs_the_founder_github_callback_line(self):
        with self.assertLogs("integrations.services.github_callback_reasons", level="INFO") as logs:
            self.callback(self.params(self.chat_state(self.new), code=None))
        self.assertIn(f"founder_github_callback org={self.new.organization.pk} reason=authorization_failed", "\n".join(logs.output))


class BadChatStateTests(CallbackReasonTestBase):
    """A bad Chat-shaped state goes only to the fixed route; nothing in it is used."""

    def _unsigned(self):
        payload = {"nonce": "n", "type": "org", "domain": "banksia.studio", "return_url": "https://chat.example/evil",
                   "chat_context": {"chat_company_id": str(self.new.company.pk), "chat_session_id": "s", "user_id": self.new.owner.pk}}
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    def _expired(self, *, native=False):
        with patch("time.time", return_value=time.time() - 3600):
            return self.chat_state(self.new, native=native).raw

    def test_unsigned_tampered_expired_and_replayed_states_use_only_the_fixed_route(self):
        replayed = self.chat_state(self.new)
        self.callback(self.params(replayed))
        tampered = self.chat_state(self.new).raw
        cases = {
            "unsigned": self._unsigned(),
            "tampered": tampered[:-2] + ("aa" if not tampered.endswith("aa") else "bb"),
            "expired": self._expired(),
            "expired native": self._expired(native=True),
            "replayed": replayed.raw,
        }
        for label, raw in cases.items():
            with self.subTest(label):
                response = self.callback({"code": "c", "installation_id": "77", "state": raw})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], FIXED_ROUTE)
                self.post.assert_not_called()
        self.assertIsNone((read_marker(self.new.organization) or {}).get("githubReturn"))

    def test_revoked_chat_session_uses_the_fixed_route(self):
        state = self.chat_state(self.new)
        with self.github(post=_json(TOKEN), get=[]), \
                patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=False):
            response = self.client.get(reverse("github_callback"), self.params(state))
        self.assertEqual(response["Location"], FIXED_ROUTE)

    def _bad_states(self, built):
        """Expired, replayed and tampered Chat states of ``built``, plus a valid one for a revoked session."""
        replayed = self.chat_state(built)
        with override_settings(**FLAG_OFF):
            self.callback(self.params(replayed))
        with patch("time.time", return_value=time.time() - 3600):
            expired = self.chat_state(built).raw
        tampered = self.chat_state(built).raw
        return {"expired": expired, "replayed": replayed.raw,
                "tampered": tampered[:-2] + ("aa" if not tampered.endswith("aa") else "bb")}

    def _revoked(self, built, values):
        state = self.chat_state(built)
        with override_settings(**values), self.github(post=_json(TOKEN), get=[_json(USER), _json(_repos("founder/x"))]), \
                patch("community_chat.startups.oauth_context.valid_chat_oauth_context", return_value=False):
            return _snapshot(self.client.get(reverse("github_callback"), self.params(state)))

    def test_v2_kill_switch_keeps_todays_400_for_every_bad_state(self):
        """G3: with every P1 flag on but V2, no bad state is redirected, for any cohort."""
        legacy = make_named_legacy_org("talathrive", domain=f"talathrive-{uuid.uuid4().hex[:6]}.example")
        for name, built in (("new journey", self.new), ("legacy", legacy)):
            states = {**self._bad_states(built), "unsigned": self._unsigned()}
            for label, raw in states.items():
                with self.subTest(f"{name} {label}"), override_settings(**V2_OFF):
                    response = self.callback({"code": "c", "installation_id": "77", "state": raw})
                    self.assertEqual(_snapshot(response), TODAY_BAD_STATE)
            with self.subTest(f"{name} revoked session"):
                self.assertEqual(self._revoked(built, V2_OFF), self._revoked(built, FLAG_OFF))
        self.assertIsNone((read_marker(self.new.organization) or {}).get("githubReturn"))

    def test_legacy_expired_replayed_and_revoked_states_keep_todays_400_with_every_flag_on(self):
        """G1: a legacy org's validly signed bad state is not redirected; only its org is read."""
        legacy = make_named_legacy_org("vyavos", domain=f"vyavos-{uuid.uuid4().hex[:6]}.example")
        states = self._bad_states(legacy)
        for label in ("expired", "replayed"):
            with self.subTest(label):
                today = _snapshot(self.callback({"code": "c", "installation_id": "77", "state": states[label]}))
                with override_settings(**FLAG_OFF):
                    flag_off = _snapshot(self.callback({"code": "c", "installation_id": "77", "state": states[label]}))
                self.assertEqual(today, TODAY_BAD_STATE)
                self.assertEqual(today, flag_off)
        with self.subTest("revoked session"):
            self.assertEqual(self._revoked(legacy, ALL_P1_FLAGS_ON), self._revoked(legacy, FLAG_OFF))
        with self.subTest("tampered (names no trusted org: the contract's fixed route)"):
            response = self.callback({"code": "c", "installation_id": "77", "state": states["tampered"]})
            self.assertEqual(response["Location"], FIXED_ROUTE)

    def test_missing_and_non_chat_states_keep_todays_400_with_the_flag_on(self):
        missing = self.callback({"code": "c", "installation_id": "77"})
        self.assertEqual(_snapshot(missing)[:2], (400, b"Missing state"))
        malformed = self.callback({"code": "c", "installation_id": "77", "state": "invalid::state"})
        self.assertEqual(_snapshot(malformed)[:2], (400, b"Invalid or expired state"))
        garbage = self.callback({"code": "c", "installation_id": "77", "state": "eyJub3QiOiJjaGF0In0:abc:def"})
        self.assertEqual(_snapshot(garbage)[:2], (400, b"Invalid or expired state"))

    def test_shape_check_is_only_a_classification(self):
        self.assertTrue(reasons.is_chat_shaped_state(self.chat_state(self.new).raw))
        self.assertTrue(reasons.is_chat_shaped_state(self._unsigned()))
        plain = build_github_oauth_state(domain="banksia.studio", slack_user_id="U1")
        self.assertFalse(reasons.is_chat_shaped_state(plain.raw))
        self.assertFalse(reasons.is_chat_shaped_state("U1::nonce::job"))
        self.assertFalse(reasons.is_chat_shaped_state("x" * 9000))


class G7UnchangedTests(CallbackReasonTestBase):
    """Flag off, legacy Chat states, Slack and user-level OAuth: today's responses byte for byte."""

    BRANCHES = [
        ("missing code", {"code": None}, None, None),
        ("missing installation", {"installation_id": None}, None, None),
        ("owner approval request", {"setup_action": "request", "installation_id": None}, None, None),
        ("token offline", {}, [http_client.exceptions.ConnectionError("offline")], None),
        ("provider error", {}, _json({"error": "bad_verification_code", "error_description": SECRET_DESCRIPTION}), None),
        ("no token", {}, _json({}), None),
        ("user fetch", {}, None, [http_client.exceptions.ConnectionError("offline")]),
        ("repository access", {}, None, [_json(USER), _json({}, status=403)]),
        ("success", {}, None, None),
    ]
    TODAY = {
        "missing code": (400, b"Missing code", None),
        "missing installation": (400, b"Missing installation_id - this endpoint requires GitHub App installation", None),
        "owner approval request": (400, b"Missing installation_id - this endpoint requires GitHub App installation", None),
        "token offline": (400, b"GitHub could not complete authorization. Please connect again.", None),
        "provider error": (400, f"GitHub Error: {SECRET_DESCRIPTION}".encode(), None),
        "no token": (400, b"GitHub did not return an access token. Please connect again.", None),
        "user fetch": (400, b"GitHub account details could not be verified. Please connect again.", None),
        "repository access": (400, b"GitHub repository access could not be verified. Please connect again.", None),
    }

    def run_table(self, make_state, settings_values):
        results = {}
        for label, extra, post, get in self.BRANCHES:
            cache.clear()
            with override_settings(**settings_values):
                results[label] = _snapshot(self.callback(self.params(make_state(), **extra), post=post, get=get))
        return results

    def test_new_journey_org_with_the_flag_off_gets_todays_responses(self):
        results = self.run_table(lambda: self.chat_state(self.new), FLAG_OFF)
        for label, expected in self.TODAY.items():
            with self.subTest(label):
                self.assertEqual(results[label], expected)
        self.assertEqual(results["success"][0], 302)
        self.assertIn("/my-startup/website", results["success"][2])
        self.verify.assert_not_called()

    def test_legacy_chat_state_with_every_flag_on_is_byte_identical(self):
        for name in ("talathrive", "pawshare"):
            with self.subTest(name):
                legacy = make_named_legacy_org(name, domain=f"{name}-{uuid.uuid4().hex[:6]}.example")
                before = read_marker(legacy.organization)
                make = lambda: self.chat_state(legacy, return_url=f"{FRONTEND}/my-startup/connections?company_id={legacy.company.pk}&connected=github")
                today = self.run_table(make, FLAG_OFF)
                now = self.run_table(make, ALL_P1_FLAGS_ON)
                self.assertEqual(now, today)
                for label, expected in self.TODAY.items():
                    self.assertEqual(now[label], expected)
                self.assertEqual(read_marker(legacy.organization), before)

    def test_slack_and_user_level_oauth_with_every_flag_on_are_byte_identical(self):
        makers = {
            "slack org-level": lambda: build_github_oauth_state(domain="slack-site.example", slack_user_id="U123",
                                                                 return_url="https://website.example/start"),
            "user-level": lambda: build_github_oauth_state(slack_user_id="U456", job_id="job-1"),
        }
        for label, make in makers.items():
            with self.subTest(label):
                today = self.run_table(make, FLAG_OFF)
                now = self.run_table(make, ALL_P1_FLAGS_ON)
                self.assertEqual(now, today)


TODAY_BAD_TICKET = b"This connection link expired or was already used. Start again in MLAI Chat."


class ConnectBrowserTests(CallbackReasonTestBase):
    """connect_browser's text 400s become typed 302s for new-journey GitHub handoffs."""

    def session_for(self, user):
        now = timezone.now()
        return CommunityChatAccountSession.objects.create(
            user=user, public_key="k" * 64, installation_id=uuid.uuid4(), client_id="client", origin=FRONTEND,
            platform="web", access_token_hash=uuid.uuid4().hex, refresh_token_hash=uuid.uuid4().hex,
            auth_version=user.auth_version, access_expires_at=now + timedelta(hours=1), expires_at=now + timedelta(days=1))

    def ticket(self, built, *, provider="github", return_to=None, editor=None, age=0):
        session = self.session_for(built.owner)
        with patch("time.time", return_value=time.time() - age):
            return signing.dumps({"uid": built.owner.pk, "company": str(built.company.pk), "session": str(session.pk),
                                  "provider": provider, "return_to": return_to, "github_settings_return": editor,
                                  "nonce": uuid.uuid4().hex}, salt=connections.SALT)

    def browse(self, ticket):
        return connections.connect_browser(RequestFactory().get("/connect/browser/", {"ticket": ticket}))

    def test_new_journey_github_state_returns_to_the_website_page(self):
        response = self.browse(self.ticket(self.new))
        self.assertEqual(response.status_code, 302)
        state = parse_qs(urlsplit(response["Location"]).query)["state"][0]
        payload = signing.loads(state, salt="integrations.github.oauth.state")
        self.assertEqual(payload["return_url"], f"{FRONTEND}/my-startup/website?company_id={self.new.company.pk}")

    def test_unavailable_branches_redirect(self):
        with override_settings(GITHUB_OAUTH_CLIENT_ID=""):
            response = self.browse(self.ticket(self.new))
        self.assertReason(response, reasons.UNAVAILABLE)
        with override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False):
            response = self.browse(self.ticket(self.new))
        self.assertReason(response, reasons.UNAVAILABLE)

    def test_bad_ticket_uses_only_the_fixed_route(self):
        used = self.ticket(self.new)
        self.browse(used)
        revoked = self.ticket(self.new)
        CommunityChatAccountSession.objects.filter(pk=signing.loads(revoked, salt=connections.SALT)["session"]).delete()
        for label, raw in {"replayed": used, "expired": self.ticket(self.new, age=3600), "revoked session": revoked}.items():
            with self.subTest(label):
                response = self.browse(raw)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], FIXED_ROUTE)
        with self.subTest("tampered names no trusted company: today's 400"):
            response = self.browse(used[:-2] + "zz")
            self.assertEqual(_snapshot(response)[:2], (400, TODAY_BAD_TICKET))

    def test_bad_tickets_of_legacy_orgs_and_with_v2_off_keep_todays_400(self):
        """G1 and G3: only a new-journey org's ticket is redirected, and never with V2 off."""
        legacy = make_named_legacy_org("vyavos", domain=f"vyavos-{uuid.uuid4().hex[:6]}.example")
        for label, built, values in (("legacy, every flag on", legacy, ALL_P1_FLAGS_ON),
                                     ("new journey, V2 off", self.new, V2_OFF)):
            with self.subTest(label), override_settings(**values):
                used = self.ticket(built)
                self.browse(used)
                for kind, raw in {"replayed": used, "expired": self.ticket(built, age=3600)}.items():
                    response = self.browse(raw)
                    self.assertEqual(_snapshot(response), (400, TODAY_BAD_TICKET, None), kind)

    def test_no_website_mints_no_state(self):
        placeholder = make_new_journey_org(domain=f"startup-{uuid.uuid4().hex[:8]}.invalid")
        with patch("integrations.services.github_connections.build_github_oauth_state") as build:
            response = self.browse(self.ticket(placeholder))
        self.assertReason(response, reasons.NO_WEBSITE, placeholder)
        build.assert_not_called()

    def test_desktop_handoff_failure_keeps_the_native_link(self):
        with override_settings(GITHUB_OAUTH_CLIENT_ID=""):
            response = self.browse(self.ticket(self.new, return_to="mobile"))
        self.assertEqual(response["Location"], f"mlaichat://connections?company_id={self.new.company.pk}&provider=github")
        self.assertEqual(read_marker(self.new.organization)["githubReturn"]["reason"], reasons.UNAVAILABLE)

    def test_flag_off_and_legacy_keep_todays_text_400s(self):
        legacy = make_named_legacy_org("vyavos", domain=f"vyavos-{uuid.uuid4().hex[:6]}.example")
        for label, built, values in (("flag off", self.new, FLAG_OFF), ("legacy", legacy, ALL_P1_FLAGS_ON)):
            with self.subTest(label), override_settings(**values):
                with override_settings(GITHUB_OAUTH_CLIENT_ID=""):
                    response = self.browse(self.ticket(built))
                self.assertEqual(_snapshot(response)[:2], (400, b"GitHub is not available yet. Please try again later."))
                with override_settings(COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False):
                    response = self.browse(self.ticket(built))
                self.assertEqual(_snapshot(response)[:2], (400, b"Startup connections are unavailable."))
        with override_settings(**FLAG_OFF):
            response = self.browse("not-a-ticket")
        self.assertEqual(_snapshot(response)[:2], (400, b"This connection link expired or was already used. Start again in MLAI Chat."))
        response = self.browse(self.ticket(self.new, provider="gmail")[:-2] + "zz")
        self.assertEqual(response.status_code, 400)

    def test_connect_view_refuses_a_startup_without_a_website(self):
        placeholder = make_new_journey_org(domain=f"startup-{uuid.uuid4().hex[:8]}.invalid")
        view = connections.ConnectView()
        view.company = placeholder.company
        request = Obj(user=placeholder.owner, auth=Obj(pk="session"), data={},
                      build_absolute_uri=lambda path: "https://api.example" + path)
        response = view.post(request, "github")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"detail": "Add your website before connecting GitHub.", "reason": "no_website"})
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            self.assertEqual(view.post(request, "github").status_code, 200)
        view.company = self.new.company
        self.assertEqual(view.post(request, "github").status_code, 200)


class SettingsReturnTests(CallbackReasonTestBase):
    def test_github_settings_return_accepts_the_website_page_only_with_the_flag_on(self):
        company_id = self.new.company.pk
        value = f"/my-startup/website?company_id={company_id}"
        self.assertEqual(connections.github_settings_return(value, company_id), value)
        for bad in (f"/my-startup/website?company_id={uuid.uuid4()}", value + "&next=https://evil.example",
                    f"https://evil.example{value}", value + "#x", "/my-startup/website"):
            with self.subTest(bad):
                self.assertIsNone(connections.github_settings_return(bad, company_id))
        editor = f"/my-startup/connections/github?company_id={company_id}"
        self.assertEqual(connections.github_settings_return(editor, company_id), editor)
        with override_settings(MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED=False):
            self.assertIsNone(connections.github_settings_return(value, company_id))
            self.assertEqual(connections.github_settings_return(editor, company_id), editor)
