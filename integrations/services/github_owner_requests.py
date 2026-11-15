"""Pick up a GitHub organisation owner's approval for a founder who asked for it.

A founder who is not the owner of the GitHub organisation that holds their
website can only *request* the MLAI app. GitHub then returns with
``setup_action=request`` and no installation. When the owner approves,
GitHub sends an ``installation`` ``created`` (or ``installation_repositories``
``added``) webhook, but nothing linked that installation to the founder, so
their card never moved on.

Now the request is remembered on the startup's content config
(``article_system["github_owner_request"]``: the founder's user id, company
id, time and the founder's own GitHub logins; no schema change). The
logins come from the authorization GitHub returns with the request, and
from the founder's earlier installations.

GitHub names the person who asked for the app as the webhook's
``requester``. An event is only picked up when that requester is one of the
founder's recorded logins, so an installation on someone else's account
can never attach to a founder. Then the new installation's repositories are
matched back to the startup by its domain. When exactly one pending startup
matches exactly one repository, the installation is registered for that
founder, the repository is bound and setup starts, as if the founder had
connected it. If the bind is refused, a registration made for it is removed
again. A startup that is already connected, an event without a matching
requester, an ambiguous match and a request older than :data:`REQUEST_DAYS`
are left alone.

``ARTICLES_GITHUB_OWNER_APPROVAL_PICKUP_ENABLED`` (default on) is the kill switch.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

FLAG = "ARTICLES_GITHUB_OWNER_APPROVAL_PICKUP_ENABLED"
KEY = "github_owner_request"
REQUEST_DAYS = 30
#: At most this many recent pending requests are read for one webhook.
MAX_PENDING = 200


def enabled() -> bool:
    """Kill switch for the owner-approval pickup; default on."""
    return bool(getattr(settings, FLAG, True))


def founder_github_logins(user, *extra) -> list:
    """The founder's own GitHub logins: ``extra`` plus those of their earlier installations."""
    from integrations.models import GitHubInstallation
    logins = [str(login or "").strip() for login in extra]
    if user is not None and getattr(user, "pk", None):
        logins += [str(login or "").strip() for login in
                   GitHubInstallation.objects.filter(user=user).values_list("github_user_name", flat=True)]
    seen, ordered = set(), []
    for login in logins:
        key = login.casefold()
        if login and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def login_from_code(code) -> str:
    """The GitHub login behind the authorization ``code`` returned with a request, or ""."""
    if not code:
        return ""
    from integrations import http_client as requests
    try:
        token = requests.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
            data={"client_id": settings.GITHUB_OAUTH_CLIENT_ID, "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
                  "code": code}, timeout=(3, 10))
        token.raise_for_status()
        access_token = (token.json() or {}).get("access_token")
        if not access_token:
            return ""
        user = requests.get("https://api.github.com/user", headers={
            "Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}, timeout=(3, 10))
        user.raise_for_status()
        return str((user.json() or {}).get("login") or "").strip()
    except Exception:
        logger.warning("github_owner_request_login_unavailable")
        return ""


def record_request(*, organization, user_id, company_id, github_logins=()) -> None:
    """Remember that this founder asked a GitHub organisation owner for the app.

    ``github_logins`` are the founder's own GitHub logins; the approval is
    only picked up when GitHub names one of them as the requester.
    """
    from django.db import transaction
    from content_factory.models import OrganizationContentConfig
    if not enabled() or organization is None or not getattr(organization, "pk", None):
        return
    OrganizationContentConfig.objects.get_or_create(organization=organization)
    with transaction.atomic():
        config = OrganizationContentConfig.objects.select_for_update().get(organization=organization)
        config.article_system = {**(config.article_system or {}), KEY: {
            "user_id": user_id, "company_id": str(company_id), "requested_at": timezone.now().isoformat(),
            "github_logins": [str(login).casefold() for login in github_logins if login]}}
        config.save(update_fields=["article_system", "updated_at"])


def clear_request(config) -> None:
    """Forget a pending request once the startup's website is connected."""
    from django.db import transaction
    if KEY not in (config.article_system or {}):
        return
    with transaction.atomic():
        locked = type(config).objects.select_for_update().get(pk=config.pk)
        system = dict(locked.article_system or {})
        if KEY in system:
            system.pop(KEY)
            locked.article_system = system
            locked.save(update_fields=["article_system", "updated_at"])
    config.article_system = locked.article_system


def _pending(config, now):
    request = (config.article_system or {}).get(KEY)
    if not isinstance(request, dict):
        return None
    requested_at = parse_datetime(str(request.get("requested_at") or ""))
    if requested_at is None or now - requested_at > timedelta(days=REQUEST_DAYS) or requested_at > now + timedelta(minutes=5):
        return None
    return request


def _named_like(domain, repositories):
    from integrations.services.github_callback_reasons import _normalised, domain_name_keys
    keys = domain_name_keys(domain)
    return [repo for repo in repositories
            if "/" in str(repo.get("full_name") or "")
            and _normalised(str(repo["full_name"]).split("/", 1)[1]) in keys]


def match_requests(repositories, *, requester="", now=None):
    """``[(config, request, repo)]``: each pending startup asked for by ``requester``
    with exactly one repository named like its domain, where no other pending
    startup claims that repository."""
    from content_factory.models import OrganizationContentConfig
    now = now or timezone.now()
    requester = str(requester or "").strip().casefold()
    repositories = [repo for repo in repositories or [] if isinstance(repo, dict)]
    if not repositories or not requester:
        return []
    candidates = []
    pending = (OrganizationContentConfig.objects
               .filter(article_system__has_key=KEY, website_connection__isnull=True,
                       updated_at__gte=now - timedelta(days=REQUEST_DAYS))
               .select_related("organization").order_by("-updated_at")[:MAX_PENDING])
    for config in pending:
        request = _pending(config, now)
        if request is None or not config.organization.domain:
            continue
        if requester not in [str(login).casefold() for login in request.get("github_logins") or []]:
            continue
        matches = _named_like(config.organization.domain, repositories)
        if len(matches) == 1:
            candidates.append((config, request, matches[0]))
    claimed = {}
    for _, _, repo in candidates:
        name = str(repo["full_name"]).casefold()
        claimed[name] = claimed.get(name, 0) + 1
    return [row for row in candidates if claimed[str(row[2]["full_name"]).casefold()] == 1]


def _installation_repositories(installation_id, payload, *, added):
    rows = payload.get("repositories_added" if added else "repositories")
    if isinstance(rows, list) and rows:
        return rows
    if added:
        return []
    try:
        from integrations.services.github_app import list_installation_repositories_via_app
        return list_installation_repositories_via_app(str(installation_id))
    except Exception:
        logger.warning("github_owner_request_listing_failed installation_id=%s", installation_id)
        return []


def pick_up(event_type, payload) -> int:
    """Handle ``installation`` ``created`` and ``installation_repositories`` ``added``.

    Returns how many founders' websites were connected.
    """
    if not enabled():
        return 0
    action = str(payload.get("action") or "")
    if not ((event_type == "installation" and action == "created")
            or (event_type == "installation_repositories" and action == "added")):
        return 0
    installation = payload.get("installation") if isinstance(payload.get("installation"), dict) else {}
    installation_id = installation.get("id")
    if type(installation_id) is not int:
        return 0
    account = installation.get("account") if isinstance(installation.get("account"), dict) else {}
    requester = payload.get("requester") if isinstance(payload.get("requester"), dict) else {}
    requester_login = str(requester.get("login") or "").strip()
    if not requester_login:
        # Not an approved request (the owner installed it directly): nothing
        # ties this installation to a founder who asked.
        return 0
    repositories = _installation_repositories(installation_id, payload, added=action == "added")
    connected = 0
    for config, request, repo in match_requests(repositories, requester=requester_login):
        connected += _connect(config, request, repo, installation_id=str(installation_id), account=account)
    return connected


def _connect(config, request, repo, *, installation_id, account) -> int:
    from django.contrib.auth import get_user_model
    from founder_tools.models import VibeRaisingCompany
    from integrations.services import github_callback_reasons as reasons
    from integrations.services.github_installations import upsert_github_installation
    organization = config.organization
    user = get_user_model().objects.filter(pk=request.get("user_id")).first()
    company = VibeRaisingCompany.objects.filter(pk=request.get("company_id"), profile__user=user,
                                                organization=organization).first() if user else None
    if company is None:
        logger.info("github_owner_request_unmatched org=%s reason=founder_changed", organization.pk)
        return 0
    from integrations.models import GitHubInstallation
    # The bind verifies access through the founder's own installations, so
    # register this one first, and remove it again if the bind is refused.
    registered_before = GitHubInstallation.objects.filter(user=user, installation_id=installation_id).exists()
    upsert_github_installation(user=user, installation_id=installation_id,
                               account_login=str(account.get("login") or ""), account_type=str(account.get("type") or ""))
    try:
        reason = reasons.bind_repository(config=config, user=user, company=company, repo=repo,
                                         installation_id=installation_id,
                                         owner_click=reasons.is_new_journey_organization(organization))
    except Exception:
        logger.exception("github_owner_request_bind_failed org=%s", organization.pk)
        reason = reasons.UNAVAILABLE
    reasons.log_return(organization, reason)
    if reason != reasons.CONNECTED:
        if not registered_before:
            GitHubInstallation.objects.filter(user=user, installation_id=installation_id).delete()
        return 0
    config.refresh_from_db()
    clear_request(config)
    try:
        from content_factory.founder_journey_cohort import update_marker
        update_marker(organization, github_return={"reason": reasons.CONNECTED, "at": timezone.now().isoformat()})
    except Exception:
        logger.exception("github_owner_request_marker_failed org=%s", organization.pk)
    logger.info("github_owner_request_connected org=%s installation_id=%s", organization.pk, installation_id)
    return 1
