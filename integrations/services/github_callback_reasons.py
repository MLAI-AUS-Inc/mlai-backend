"""P1.14: GitHub callbacks that never strand a new-journey founder.

With ``MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED`` on, a GitHub
connection started from MLAI Chat for a new-journey organisation
(``founder_journey_cohort.is_new_journey``) never ends on a text error page.
Every failure branch of the GitHub callback and of the Chat browser handoff
returns a 302 back into the app with one typed reason from :data:`REASONS`
(sent as the ``github`` query value on the web, or stored in the cohort
marker on the desktop, whose native link carries no extra keys).

What this module owns:

- :data:`REASONS`, the closed enum (never a ``code`` key);
- the chat-state shape check (:func:`is_chat_shaped_state`), which decodes a
  state without trusting it, only to decide whether a bad state goes to the
  fixed route (:func:`state_expired_redirect`) or keeps today's 400;
- the redirect builder (:func:`redirect_with_reason`);
- :func:`begin_callback`, the gate that decides whether a callback takes the
  new-journey path at all;
- the binding helper (:func:`bind_new_journey_repository`), which binds the
  repository that matches the domain (or the only one the founder can push
  to) and starts website preparation.

Everything else (Slack and user-level OAuth, valid legacy Chat callbacks,
every flow with the flag off, a missing or non-chat state) keeps today's
responses byte for byte: :func:`begin_callback` returns None for them and the
callers run their unchanged code.

Nothing from a state that failed validation is ever used: a bad chat-shaped
state goes to :func:`state_expired_redirect`, which carries no company, no
return URL and no ``return_to``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import zlib
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

FLAG = "MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED"

CONNECTED = "connected"
MULTIPLE_REPOS = "multiple_repos"
NO_REPO = "no_repo"
STATE_EXPIRED = "state_expired"
OWNER_APPROVAL_REQUESTED = "owner_approval_requested"
AUTHORIZATION_FAILED = "authorization_failed"
REPOSITORY_ACCESS = "repository_access"
OWNED_ELSEWHERE = "owned_elsewhere"
NO_WEBSITE = "no_website"
STARTUP_CHANGED = "startup_changed"
CANCELLED = "cancelled"
UNAVAILABLE = "unavailable"

#: The closed set of reasons a GitHub return can carry (contract IF-P1-17).
REASONS = frozenset({
    CONNECTED, MULTIPLE_REPOS, NO_REPO, STATE_EXPIRED, OWNER_APPROVAL_REQUESTED, AUTHORIZATION_FAILED,
    REPOSITORY_ACCESS, OWNED_ELSEWHERE, NO_WEBSITE, STARTUP_CHANGED, CANCELLED, UNAVAILABLE,
})

#: Founder copy for the JSON refusal of a startup without a website (contract section 11.7).
NO_WEBSITE_DETAIL = "Add your website before connecting GitHub."

WEBSITE_PATH = "/my-startup/website"
_NATIVE_RETURNS = frozenset({"mobile", "desktop-dev"})
# Bounds for decoding a state we do not trust yet.
_MAX_STATE_LENGTH = 8192
_MAX_DECODED_BYTES = 65536
# Bind-time failures worth one fresh installation token before asking again.
_REMINT_FAILURES = frozenset({"github_repository_unavailable", "github_access_unverified"})
_TRANSIENT_FAILURES = frozenset({"github_repository_transient", "github_source_transient"})


def redirects_enabled() -> bool:
    """True when ``MY_STARTUP_GITHUB_CALLBACK_REDIRECTS_ENABLED`` is on (code default off)."""
    return bool(getattr(settings, FLAG, False))


def is_new_journey_organization(organization) -> bool:
    """The cohort gate: the organisation is in the new journey (V2 on, not disabled)."""
    if organization is None or not getattr(organization, "pk", None):
        return False
    from content_factory.founder_journey_cohort import is_new_journey
    return bool(is_new_journey(organization))


def gate_open(organization) -> bool:
    """The flag is on and the organisation is in the new journey."""
    return redirects_enabled() and is_new_journey_organization(organization)


def log_return(organization, reason) -> None:
    """The staff-only ``founder_github_callback`` line: ids and enums only."""
    logger.info("founder_github_callback org=%s reason=%s",
                getattr(organization, "pk", None) if organization is not None else None, reason)


# ---------------------------------------------------------------------------
# Untrusted shape checks


def _decode_untrusted(raw, *, max_length=_MAX_STATE_LENGTH):
    """Decode the payload part of a Django signed value without checking it.

    Used only to classify a value; the result is never trusted or used.
    """
    if not isinstance(raw, str) or not raw or len(raw) > max_length:
        return None
    body = raw.split(":", 1)[0]
    compressed = body.startswith(".")
    if compressed:
        body = body[1:]
    try:
        data = signing.b64_decode(body.encode("ascii"))
        if compressed:
            inflater = zlib.decompressobj()
            data = inflater.decompress(data, _MAX_DECODED_BYTES)
            if inflater.unconsumed_tail:
                return None
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def is_chat_shaped_state(raw_state) -> bool:
    """True when ``raw_state`` decodes to the Chat flow's GitHub state structure.

    The Chat flow (``connect_browser``) signs an org-level state that carries a
    ``chat_context`` with the Chat session and company. This check does not
    verify the signature or the age: it only tells a Chat state (which, when
    bad, goes to the fixed route) from every other state (which keeps today's
    400). Nothing decoded here is used.
    """
    payload = _decode_untrusted(raw_state)
    if payload is None:
        return False
    context = payload.get("chat_context")
    return (payload.get("type") == "org" and isinstance(payload.get("nonce"), str)
            and isinstance(context, dict) and bool(context.get("chat_company_id"))
            and bool(context.get("chat_session_id")))


def is_github_chat_ticket(raw_ticket) -> bool:
    """True when a Chat connect ticket decodes (unverified) to a GitHub handoff.

    Like :func:`is_chat_shaped_state`, only a classification: nothing decoded is used.
    """
    payload = _decode_untrusted(raw_ticket)
    return bool(payload and payload.get("provider") == "github" and payload.get("company") and payload.get("session"))


# ---------------------------------------------------------------------------
# Redirects


def _frontend() -> str:
    return str(getattr(settings, "COMMUNITY_CHAT_FRONTEND_URL", "") or "").rstrip("/")


def website_return_path(company_id) -> str:
    """``/my-startup/website?company_id=<uuid>``: where a new-journey Chat flow returns on the web."""
    return WEBSITE_PATH + "?" + urlencode({"company_id": str(company_id)})


def state_expired_redirect():
    """The fixed allowlisted route for a bad Chat state, or None when it cannot be built.

    It carries no company, no return URL and no ``return_to`` from the state:
    the client restarts the connection for its active company.
    """
    from integrations.services.chat_oauth_return import oauth_return_redirect
    from integrations.services.github_connections import is_allowed_return_url
    frontend = _frontend()
    target = f"{frontend}{WEBSITE_PATH}?" + urlencode({"github": STATE_EXPIRED}) if frontend else ""
    if not target or not is_allowed_return_url(target):
        return None
    log_return(None, STATE_EXPIRED)
    return oauth_return_redirect(target)


def _with_reason(url, reason) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "github"]
    query.append(("github", reason))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redirect_with_reason(reason, *, organization, company_id, return_url):
    """A 302 back into the app with ``reason``, used only after the state or ticket validated.

    - Desktop (a native ``mlaichat://connections`` link): today's link, unchanged
      (the native parser rejects other keys), and the reason is stored with
      ``update_marker(org, github_return={"reason", "at"})``.
    - Web: the validated ``return_url`` with ``github=<reason>``; an untrusted or
      missing one falls back to ``/my-startup/website?company_id=<uuid>``.
    """
    from integrations.services.chat_oauth_return import native_chat_connection_return_url, oauth_return_redirect
    from integrations.services.github_connections import is_allowed_return_url
    if reason not in REASONS:
        raise ValueError("Unknown GitHub return reason")
    log_return(organization, reason)
    native = native_chat_connection_return_url(return_url)
    if native:
        if organization is not None:
            from content_factory.founder_journey_cohort import update_marker
            try:
                update_marker(organization, github_return={"reason": reason, "at": timezone.now().isoformat()})
            except Exception:
                logger.exception("founder_github_return_marker_failed org=%s", getattr(organization, "pk", None))
        return oauth_return_redirect(native)
    base = return_url if return_url and is_allowed_return_url(return_url) else ""
    if not base and company_id:
        candidate = _frontend() + website_return_path(company_id)
        base = candidate if is_allowed_return_url(candidate) else ""
    if not base:
        from integrations.services.github_connections import default_frontend_url
        base = default_frontend_url()
    return oauth_return_redirect(_with_reason(base, reason))


def chat_return_url(*, company_id, return_to, editor_return) -> str:
    """The return URL a new-journey Chat GitHub connection signs into its state.

    The native link for the desktop, the owned GitHub editor when the client
    asked for it, and otherwise the website page.
    """
    if return_to in _NATIVE_RETURNS:
        scheme = "mlaichat-dev" if return_to == "desktop-dev" else "mlaichat"
        return f"{scheme}://connections?" + urlencode({"company_id": str(company_id), "provider": "github"})
    if editor_return:
        return _frontend() + editor_return
    return _frontend() + website_return_path(company_id)


def website_settings_return(value, company_id):
    """``/my-startup/website?company_id=<uuid>`` when ``value`` is exactly that, else None.

    Accepted by ``github_settings_return`` only while the flag is on.
    """
    if not redirects_enabled() or not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlsplit(value)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        UUID(str(company_id))
    except (ValueError, TypeError):
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != WEBSITE_PATH:
        return None
    if pairs != [("company_id", str(company_id))]:
        return None
    return website_return_path(company_id)


# ---------------------------------------------------------------------------
# The callback gate


@dataclass
class ChatCallback:
    """A GitHub callback on the new-journey path. ``response`` set means: return it now."""
    oauth_state: object = None
    company: object = None
    organization: object = None
    response: object = None

    def redirect(self, reason):
        """A 302 with ``reason`` to this flow's validated return."""
        return redirect_with_reason(reason, organization=self.organization,
                                    company_id=getattr(self.company, "pk", None) or self._state_company_id(),
                                    return_url=getattr(self.oauth_state, "return_url", None))

    def _state_company_id(self):
        context = getattr(self.oauth_state, "chat_context", None) or {}
        return context.get("chat_company_id") if isinstance(context, dict) else None

    def refuse(self, reason, legacy_response):
        """``legacy_response`` is ignored on this path: every failure is a redirect."""
        return self.redirect(reason)


def _used(raw_state) -> bool:
    return bool(cache.get("github-oauth-used:" + hashlib.sha256(raw_state.encode()).hexdigest()))


def _bad_state():
    response = state_expired_redirect()
    return ChatCallback(response=response) if response is not None else None


def begin_callback(request) -> Optional[ChatCallback]:
    """Decide whether a GitHub callback takes the new-journey path.

    Returns None for today's path (flag off, no state, a non-chat state, or a
    valid Chat state of a legacy organisation): the caller then runs its
    unchanged code, so those responses stay byte for byte. Otherwise returns
    a :class:`ChatCallback`; when its ``response`` is set the caller returns
    it at once (a bad state, a changed startup, an owner approval request, a
    cancelled or failed authorization).

    The legacy decision is a pure read: the state is only consumed once the
    organisation is known to be new-journey.
    """
    if not redirects_enabled():
        return None
    raw_state = request.GET.get("state")
    if not raw_state or not is_chat_shaped_state(raw_state):
        return None
    from integrations.services.github_connections import (
        GITHUB_OAUTH_STATE_SALT, GITHUB_OAUTH_STATE_TIMEOUT_SECONDS, validate_github_oauth_state,
    )
    try:
        payload = signing.loads(raw_state, salt=GITHUB_OAUTH_STATE_SALT, max_age=GITHUB_OAUTH_STATE_TIMEOUT_SECONDS)
    except (signing.BadSignature, ValueError, TypeError):
        return _bad_state()
    context = payload.get("chat_context") if isinstance(payload, dict) else None
    if not isinstance(context, dict) or payload.get("type") != "org" or _used(raw_state):
        return _bad_state()
    from community_chat.startups.oauth_context import valid_chat_oauth_context
    if not valid_chat_oauth_context(context, context.get("user_id")):
        return _bad_state()
    from organizations.models import Organization
    try:
        organization = Organization.objects.filter(pk=context.get("organization_id")).first() \
            if context.get("organization_id") is not None else None
    except (ValueError, TypeError):
        organization = None
    if not gate_open(organization):
        return None
    try:
        oauth_state = validate_github_oauth_state(raw_state, request=request)
    except ValueError:
        return _bad_state()
    from founder_tools.models import VibeRaisingCompany
    company = VibeRaisingCompany.objects.select_related("organization").filter(
        pk=context["chat_company_id"], profile__user_id=context["user_id"],
        organization__domain=oauth_state.domain,
    ).first()
    flow = ChatCallback(oauth_state=oauth_state, company=company, organization=organization)
    if company is None:
        flow.response = flow.redirect(STARTUP_CHANGED)
        return flow
    flow.organization = company.organization
    if (request.GET.get("setup_action") or "") == "request":
        flow.response = flow.redirect(OWNER_APPROVAL_REQUESTED)
    elif not request.GET.get("code"):
        flow.response = flow.redirect(CANCELLED if request.GET.get("error") == "access_denied" else AUTHORIZATION_FAILED)
    return flow


def transport_reason(exc) -> str:
    """``unavailable`` for a timeout, a dropped connection or a GitHub 5xx/429, else ``authorization_failed``."""
    from integrations import http_client
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(exc, (http_client.exceptions.Timeout, http_client.exceptions.ConnectionError)):
        return UNAVAILABLE
    if type(status) is int and (status == 429 or status >= 500):
        return UNAVAILABLE
    if status is None and not isinstance(exc, http_client.exceptions.HTTPError):
        return UNAVAILABLE
    return AUTHORIZATION_FAILED


# ---------------------------------------------------------------------------
# Repository matching and binding


def _normalised(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def domain_name_keys(domain) -> set:
    """Normalised repository names that count as a match for ``domain``.

    The full domain (``banksia.studio`` gives ``banksiastudio``; the hyphenated
    ``banksia-studio`` normalises to the same) and, only for a domain of exactly
    two labels, the first label (``banksia``). ``banksia.com.au`` gives only
    ``banksiacomau``.
    """
    host = str(domain or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return set()
    keys = {_normalised(host), _normalised(host.replace(".", "-"))}
    labels = host.split(".")
    if len(labels) == 2 and labels[0]:
        keys.add(_normalised(labels[0]))
    return {key for key in keys if key}


def push_candidates(repositories) -> list:
    """The repositories the founder may push to (``permissions.push`` or ``permissions.admin``)."""
    candidates = []
    for repo in repositories or []:
        if not isinstance(repo, dict):
            continue
        full_name = str(repo.get("full_name") or "").strip()
        permissions = repo.get("permissions") if isinstance(repo.get("permissions"), dict) else {}
        if full_name and "/" in full_name and (permissions.get("push") is True or permissions.get("admin") is True):
            candidates.append(repo)
    return candidates


def match_repository(domain, repositories):
    """``(reason, repo)``: the one repository to bind, or why there is none.

    Exactly one candidate whose name matches the domain binds; with no match,
    the only candidate binds. Several candidates without a single match give
    ``multiple_repos``; no candidate gives ``no_repo``.
    """
    candidates = push_candidates(repositories)
    if not candidates:
        return NO_REPO, None
    keys = domain_name_keys(domain)
    matches = [repo for repo in candidates
               if _normalised(str(repo["full_name"]).split("/", 1)[1]) in keys]
    if len(matches) == 1:
        return CONNECTED, matches[0]
    if not matches and len(candidates) == 1:
        return CONNECTED, candidates[0]
    return MULTIPLE_REPOS, None


def _user_installations(access_token):
    from integrations import http_client
    response = http_client.get("https://api.github.com/user/installations",
                               headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3+json"},
                               timeout=(3, 20))
    response.raise_for_status()
    installations = response.json().get("installations")
    return [row for row in installations if isinstance(row, dict) and row.get("id")] if isinstance(installations, list) else []


def _installation_repositories(access_token, installation_id):
    from integrations import http_client
    response = http_client.get(f"https://api.github.com/user/installations/{installation_id}/repositories",
                               headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github.v3+json"},
                               timeout=(3, 20))
    response.raise_for_status()
    repositories = response.json().get("repositories")
    return repositories if isinstance(repositories, list) else []


def resolve_installation_id(access_token, domain) -> Optional[str]:
    """The installation to use when GitHub's callback carried no ``installation_id``.

    Lists ``GET /user/installations`` with the founder's user token and
    prefers the installation whose repositories include the domain-matching
    repository; the only installation is used when none matches. Returns None
    when there is none (the caller answers ``no_repo``).
    """
    try:
        installations = _user_installations(access_token)
    except Exception:
        logger.warning("founder_github_installation_lookup_failed")
        return None
    if not installations:
        return None
    for installation in installations:
        try:
            repositories = _installation_repositories(access_token, installation["id"])
        except Exception:
            continue
        keys = domain_name_keys(domain)
        if any(_normalised(str(repo["full_name"]).split("/", 1)[1]) in keys for repo in push_candidates(repositories)):
            return str(installation["id"])
    return str(installations[0]["id"]) if len(installations) == 1 else None


def _repository_owned_elsewhere(organization, repo) -> bool:
    from content_factory.models import OrganizationContentConfig
    from content_factory.website_models import WebsiteConnection
    return (OrganizationContentConfig.objects.filter(github_repo__iexact=repo).exclude(organization=organization).exists()
            or WebsiteConnection.objects.filter(github_repo__iexact=repo, state=WebsiteConnection.State.CONNECTED)
            .exclude(organization=organization).exists())


def remint_installation_token(installation_id, repo) -> bool:
    """Drop the cached read token for ``repo`` and mint a fresh one, once. True when it minted."""
    from integrations.services import github_app
    try:
        cache.delete(github_app._cache_key(installation_id=str(installation_id), repository=str(repo), permission_mode="read"))
        github_app.create_installation_access_token(installation_id=str(installation_id), repository=str(repo),
                                                    permission_mode="read")
    except Exception:
        logger.warning("founder_github_token_remint_failed installation_id=%s", installation_id)
        return False
    return True


def _failure_reason(exc) -> str:
    failure = getattr(exc, "code", "")
    if failure in _TRANSIENT_FAILURES or getattr(exc, "status", None) == 503:
        return UNAVAILABLE
    return REPOSITORY_ACCESS


def bind_new_journey_repository(*, config, user, company, repositories, installation_id) -> str:
    """Bind the domain's repository for a new-journey Chat connection and start setup.

    Returns the reason for the redirect: ``connected`` after a bind (or when
    the current connection already is that repository), ``multiple_repos``,
    ``no_repo``, ``owned_elsewhere``, ``repository_access`` or ``unavailable``.

    Calls ``website_connections.bind_website`` with an empty folder and branch
    (the default branch is inferred from the repository), then
    ``website_prepare.start_prepare``, and records ``ownerClickRequired`` on
    the marker. Before answering ``repository_access`` after
    ``github_repository_unavailable`` or an unverified token, the installation
    token is re-minted once and the bind retried.
    """
    from content_factory import website_connections
    from content_factory.founder_journey_cohort import update_marker
    from content_factory.website_contract import WebsiteAuthorityError
    organization = config.organization
    reason, repo = match_repository(organization.domain, repositories)
    if repo is None:
        return reason
    full_name = str(repo["full_name"]).strip()
    current = config.website_connection
    if current is not None:
        if current.state == "connected" and current.github_repo.casefold() == full_name.casefold():
            _start_prepare(config, company=company, user=user)
            return CONNECTED
        # A different or paused website stays as it is; the founder chooses explicitly.
        return MULTIPLE_REPOS
    if _repository_owned_elsewhere(organization, full_name):
        return OWNED_ELSEWHERE
    reminted = False
    while True:
        try:
            website_connections.bind_website(config, user=user, repo=full_name, app_root="", branch="")
            break
        except WebsiteAuthorityError as exc:
            if not reminted and getattr(exc, "code", "") in _REMINT_FAILURES and installation_id:
                reminted = True
                if remint_installation_token(installation_id, full_name):
                    continue
            logger.warning("founder_github_bind_refused org=%s", organization.pk)
            return _failure_reason(exc)
    try:
        update_marker(organization, owner_click_required=True)
    except Exception:
        logger.exception("founder_github_owner_click_marker_failed org=%s", organization.pk)
    _start_prepare(config, company=company, user=user)
    return CONNECTED


def _start_prepare(config, *, company, user) -> None:
    from content_factory import website_prepare
    try:
        config.refresh_from_db()
        operation = website_prepare.start_prepare(config, company_id=company.pk, user=user)
    except Exception:
        # The connection is bound; the website page shows the preparation state.
        logger.exception("founder_github_prepare_start_failed org=%s", config.organization_id)
        return
    try:
        website_prepare.advance_prepare(operation.pk)
    except Exception:
        logger.exception("founder_github_prepare_advance_failed org=%s", config.organization_id)
