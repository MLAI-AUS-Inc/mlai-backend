import secrets
import urllib.parse
import logging
from datetime import timedelta
from typing import Optional, Set
from django.conf import settings
from django.shortcuts import redirect
from django.http import HttpResponseBadRequest, HttpResponse
from django.contrib.auth import login as auth_login
from django.utils import timezone
from hospital.authentication import CustomJWTAuthentication
from .models import GoogleConnection, UserIntegration
from integrations.services.github_connections import (
    build_github_installation_url,
    build_github_oauth_state,
    store_github_oauth_state,
    validate_github_oauth_state,
)
from integrations.services.external_connectors import (
    ConnectorConfigurationError,
    ConnectorOAuthError,
    build_authorization_url,
    complete_oauth_callback,
    normalize_provider,
)
from integrations import http_client as requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_OAUTH_STATE_SESSION_KEY = "google_oauth_state"
GOOGLE_OAUTH_NEXT_SESSION_KEY = "google_oauth_next"
GOOGLE_OAUTH_SUCCESS_PATH = "/settings?gmail_connected=true"

logger = logging.getLogger(__name__)


def _origin_from_url(url: Optional[str]) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlparse(str(url).strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _known_frontend_origins() -> Set[str]:
    origins = {
        origin
        for origin in (
            _origin_from_url(getattr(settings, "MEDHACK_URL", None)),
            _origin_from_url(getattr(settings, "DEFAULT_FRONTEND_URL", None)),
            _origin_from_url(getattr(settings, "ESAFETY_URL", None)),
            _origin_from_url(getattr(settings, "VIBE_RAISING_URL", None)),
        )
        if origin
    }
    if origins:
        return origins

    fallback = "http://localhost:5173" if getattr(settings, "DEBUG", False) else "https://mlai.au"
    origin = _origin_from_url(fallback)
    return {origin} if origin else set()


def _normalize_google_next(next_url: Optional[str]) -> Optional[str]:
    if not next_url:
        return None

    normalized = str(next_url).strip()
    if not normalized or normalized.startswith("//"):
        return None

    parsed = urllib.parse.urlparse(normalized)
    if not parsed.scheme and not parsed.netloc:
        return normalized if normalized.startswith("/") else f"/{normalized}"

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return normalized if _origin_from_url(normalized) in _known_frontend_origins() else None


def _default_google_success_url() -> str:
    for setting_name in ("MEDHACK_URL", "DEFAULT_FRONTEND_URL", "ESAFETY_URL", "VIBE_RAISING_URL"):
        origin = _origin_from_url(getattr(settings, setting_name, None))
        if origin:
            return f"{origin}{GOOGLE_OAUTH_SUCCESS_PATH}"

    fallback = "http://localhost:5173" if getattr(settings, "DEBUG", False) else "https://mlai.au"
    return f"{_origin_from_url(fallback) or fallback.rstrip('/')}{GOOGLE_OAUTH_SUCCESS_PATH}"


def _vibe_raising_frontend_origin() -> str:
    for setting_name in ("VIBE_RAISING_URL", "DEFAULT_FRONTEND_URL"):
        origin = _origin_from_url(getattr(settings, setting_name, None))
        if origin:
            return origin

    return "http://localhost:5173" if getattr(settings, "DEBUG", False) else "https://mlai.au"


def _path_from_frontend_next(next_url: Optional[str]) -> str:
    normalized = _normalize_google_next(next_url) or "/vibe-raising/create-update?email_draft=1"
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme and parsed.netloc:
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _vibe_raising_login_url(next_url: Optional[str]) -> str:
    params = urllib.parse.urlencode(
        {
            "app": "vibe-raising",
            "next": _path_from_frontend_next(next_url),
        }
    )
    return f"{_vibe_raising_frontend_origin()}/platform/login?{params}"


def _resolve_google_oauth_user(request):
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False):
        return user

    auth_result = CustomJWTAuthentication().authenticate(request)
    if not auth_result:
        return None

    user, _validated_token = auth_result
    request.user = user
    return user


def _dedupe_scopes(*scope_groups):
    scopes = []
    for scope_group in scope_groups:
        for scope in scope_group or []:
            if scope and scope not in scopes:
                scopes.append(scope)
    return scopes


def _google_oauth_scopes_for_request(request):
    requested_scope = request.GET.get("scope")
    if requested_scope in {"website_baseline", "vibe_marketing_baseline"}:
        identity_scopes = getattr(
            settings,
            "GOOGLE_OAUTH_IDENTITY_SCOPES",
            [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            ],
        )
        return _dedupe_scopes(identity_scopes, getattr(settings, "GOOGLE_WEBSITE_BASELINE_SCOPES", []))

    return _dedupe_scopes(getattr(settings, "GOOGLE_OAUTH_SCOPES", []))


def _ensure_django_session_for_user(request, user) -> None:
    if str(request.session.get("_auth_user_id") or "") == str(user.pk):
        return

    auth_login(
        request,
        user,
        backend="django.contrib.auth.backends.ModelBackend",
    )


def google_connect(request):
    """
    Initiates the Google OAuth flow.
    """
    user = _resolve_google_oauth_user(request)
    if user is None:
        return redirect(_vibe_raising_login_url(request.GET.get("next")))

    _ensure_django_session_for_user(request, user)

    state = secrets.token_urlsafe(32)
    request.session[GOOGLE_OAUTH_STATE_SESSION_KEY] = state

    next_url = _normalize_google_next(request.GET.get("next"))
    if next_url:
        request.session[GOOGLE_OAUTH_NEXT_SESSION_KEY] = next_url
    else:
        request.session.pop(GOOGLE_OAUTH_NEXT_SESSION_KEY, None)

    scopes = _google_oauth_scopes_for_request(request)

    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",  # helps ensure refresh_token is returned
        "include_granted_scopes": "true",
        "state": state,
    }

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(url)


def google_callback(request):
    """
    Handles the callback from Google.
    """
    user = _resolve_google_oauth_user(request)
    if user is None:
        return redirect(_vibe_raising_login_url(None))

    # 1) Validate state
    state = request.GET.get("state")
    if not state or state != request.session.get(GOOGLE_OAUTH_STATE_SESSION_KEY):
        return HttpResponseBadRequest("Invalid state")

    request.session.pop(GOOGLE_OAUTH_STATE_SESSION_KEY, None)
    success_url = _normalize_google_next(request.session.pop(GOOGLE_OAUTH_NEXT_SESSION_KEY, None))

    # 2) Handle errors
    if request.GET.get("error"):
        return HttpResponseBadRequest(f"OAuth error: {request.GET.get('error')}")

    code = request.GET.get("code")
    if not code:
        return HttpResponseBadRequest("Missing code")

    existing_connection = GoogleConnection.objects.filter(user=user).first()

    # 3) Exchange code for tokens
    try:
        token_resp = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=(3, 20),
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
    except requests.RequestException:
        logger.exception("Failed to exchange Google OAuth code for user %s", user.id)
        return HttpResponseBadRequest("Failed to exchange Google OAuth code.")

    access_token = token_data.get("access_token")
    if not access_token:
        return HttpResponseBadRequest("Missing access token in Google OAuth response.")

    refresh_token = token_data.get("refresh_token") or (existing_connection.refresh_token if existing_connection else None)
    scope = token_data.get("scope") or (existing_connection.scope if existing_connection else "")

    if not refresh_token:
        return HttpResponseBadRequest(
            "Google did not return a refresh token. Revoke Gmail access in Google and reconnect with consent."
        )

    # 4) Fetch the user's Google email
    try:
        ui_resp = requests.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=(3, 20),
        )
        ui_resp.raise_for_status()
        google_email = ui_resp.json().get("email")
    except requests.RequestException:
        logger.exception("Failed to fetch Google userinfo for user %s", user.id)
        return HttpResponseBadRequest("Failed to fetch Google account details.")

    # Update or create the connection
    defaults = {
        "google_email": google_email or (existing_connection.google_email if existing_connection else ""),
        "scope": scope,
        "refresh_token": refresh_token,
    }

    GoogleConnection.objects.update_or_create(
        user=user,
        defaults=defaults,
    )

    # Redirect to frontend
    return redirect(success_url or _default_google_success_url())


def connector_connect(request, provider):
    """
    Initiates OAuth/consent flows for Vibe Raising data-source connectors.
    Gmail delegates to the existing Google OAuth flow.
    """
    try:
        normalized_provider = normalize_provider(provider)
    except ConnectorConfigurationError as exc:
        return HttpResponseBadRequest(str(exc))

    if normalized_provider == "gmail":
        return google_connect(request)

    user = _resolve_google_oauth_user(request)
    if user is None:
        return redirect(_vibe_raising_login_url(request.GET.get("next")))

    _ensure_django_session_for_user(request, user)

    try:
        return redirect(build_authorization_url(request, normalized_provider))
    except ConnectorConfigurationError as exc:
        return HttpResponse(str(exc), status=503)
    except ConnectorOAuthError as exc:
        return HttpResponseBadRequest(str(exc))


def connector_callback(request, provider):
    """
    Handles OAuth/consent callbacks for Vibe Raising data-source connectors.
    Gmail delegates to the existing Google callback.
    """
    try:
        normalized_provider = normalize_provider(provider)
    except ConnectorConfigurationError as exc:
        return HttpResponseBadRequest(str(exc))

    if normalized_provider == "gmail":
        return google_callback(request)

    user = _resolve_google_oauth_user(request)
    if user is None:
        return redirect(_vibe_raising_login_url(None))

    request.user = user

    try:
        return redirect(complete_oauth_callback(request, normalized_provider))
    except ConnectorOAuthError as exc:
        return HttpResponseBadRequest(str(exc))


def github_connect(request):
    """
    Initiates GitHub App installation flow.
    Expects 'slack_user_id' in query params to link the token.
    Optional 'domain' param triggers org-level OAuth (preserves domain through callback).

    This uses the GitHub App installation flow which shows the native
    repository selection UI on GitHub's side.
    """
    slack_user_id = request.GET.get('slack_user_id')
    domain = request.GET.get('domain')
    if not slack_user_id:
        return HttpResponseBadRequest("Missing slack_user_id")

    if domain:
        oauth_state = build_github_oauth_state(domain=domain, slack_user_id=slack_user_id)
    else:
        job_id = request.GET.get('job_id', '')
        oauth_state = build_github_oauth_state(slack_user_id=slack_user_id, job_id=job_id)

    store_github_oauth_state(oauth_state, request=request)
    return redirect(build_github_installation_url(oauth_state.raw))

def github_callback(request):
    """
    Handles GitHub App installation callback.

    When a user installs the GitHub App, GitHub redirects here with:
    - installation_id: The ID of the GitHub App installation
    - setup_action: "install" or "update"
    - code: Authorization code to exchange for user access token
    - state: Our state parameter with slack_user_id OR domain (for org-level OAuth)

    State formats:
    - User-level: slack_user_id::random_token::job_id (legacy)
    - Org-level: domain::random_token::slack_user_id::org
    """
    code = request.GET.get("code")
    raw_state = request.GET.get("state")
    installation_id = request.GET.get("installation_id")
    setup_action = request.GET.get("setup_action") or "install"

    if not code:
        return HttpResponseBadRequest("Missing code")

    if not raw_state:
        return HttpResponseBadRequest("Missing state")

    if not installation_id:
        return HttpResponseBadRequest("Missing installation_id - this endpoint requires GitHub App installation")

    try:
        oauth_state = validate_github_oauth_state(raw_state, request=request)
    except ValueError as exc:
        logger.warning("Rejected GitHub callback state: %s", exc)
        return HttpResponseBadRequest("Invalid or expired state")

    is_org_oauth = oauth_state.is_org_oauth
    slack_user_id = oauth_state.slack_user_id
    job_id = oauth_state.job_id
    normalized_domain = oauth_state.domain

    # Exchange code for user access token
    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "code": code,
        },
        timeout=(3, 20),
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()

    if "error" in token_data:
        return HttpResponseBadRequest(f"GitHub Error: {token_data.get('error_description')}")

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")  # seconds until expiry (8 hours = 28800)

    # Calculate token expiry time
    token_expires_at = None
    if expires_in:
        token_expires_at = timezone.now() + timedelta(seconds=expires_in)
    
    # Fetch GitHub user info
    user_resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=(3, 20),
    )
    user_resp.raise_for_status()
    github_user = user_resp.json()
    github_login = github_user.get("login")

    # Fetch repositories accessible via this installation
    # This returns only the repos the user selected during installation
    try:
        repos_resp = requests.get(
            f"https://api.github.com/user/installations/{installation_id}/repositories",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=(3, 20),
        )
        repos_resp.raise_for_status()
        repos_data = repos_resp.json()
        repos = repos_data.get("repositories", [])
    except Exception:
        repos = []

    repo_names = [str(repo.get("full_name") or "").strip() for repo in repos if repo.get("full_name")]
    selected_repo = repo_names[0] if len(repo_names) == 1 else None

    retry_message = ""
    domain_display = ""
    if is_org_oauth:
        # ====== ORG-LEVEL OAUTH ======
        # Store credentials in OrganizationContentConfig for the domain
        from organizations.models import Organization
        from content_factory.models import OrganizationContentConfig

        if not normalized_domain:
            return HttpResponseBadRequest("Missing domain in state")

        # Get or create organization
        org, _ = Organization.objects.get_or_create(
            domain=normalized_domain,
            defaults={'name': normalized_domain}
        )

        # Get or create config and update GitHub credentials
        config, _ = OrganizationContentConfig.objects.get_or_create(organization=org)
        previous_repo = str(config.github_repo or "").strip()
        if not selected_repo and len(repo_names) > 1 and previous_repo:
            selected_repo = next(
                (repo_name for repo_name in repo_names if repo_name.casefold() == previous_repo.casefold()),
                None,
            )
        repo_to_store = selected_repo
        if not repo_to_store and len(repo_names) > 1 and previous_repo:
            repo_to_store = previous_repo

        config.github_token_encrypted = access_token
        config.github_refresh_token_encrypted = refresh_token
        config.github_token_expires_at = token_expires_at
        config.github_user_name = github_login
        config.connected_slack_user_id = slack_user_id or config.connected_slack_user_id
        config.github_repo = repo_to_store if repo_to_store else None
        config.github_installation_id = installation_id
        config.github_scopes = []
        config.save()

        logger.info(
            "Org-level GitHub %s for %s: repo=%s, user=%s",
            setup_action,
            normalized_domain,
            repo_to_store,
            github_login,
        )
        domain_display = f"<p>Domain: <strong>{normalized_domain}</strong></p>"

        # Also update UserIntegration if slack_user_id provided.
        # IMPORTANT: For org-level OAuth, tokens belong on OrganizationContentConfig (per-domain).
        # We must NOT overwrite UserIntegration's token/repo — that would break multi-domain
        # support by clobbering the user's existing credentials with a different domain's token.
        if slack_user_id:
            existing_integration = UserIntegration.objects.filter(slack_user_id=slack_user_id).first()
            if existing_integration:
                # Only update identity metadata — preserve token, refresh, repo, and expiry.
                # Each domain's token lives on its own OrganizationContentConfig.
                existing_integration.github_user_name = github_login
                existing_integration.github_installation_id = installation_id
                existing_integration.save()
            else:
                # No existing integration — create one with this repo+token as the default.
                # This is the user's first GitHub connection, so it becomes their default.
                UserIntegration.objects.create(
                    slack_user_id=slack_user_id,
                    github_access_token=access_token,
                    github_refresh_token=refresh_token,
                    github_token_expires_at=token_expires_at,
                    github_user_name=github_login,
                    github_repo=selected_repo,
                    github_installation_id=installation_id,
                    github_scopes=[],
                )

        # Notify via Slack and auto-trigger scan only when the installation is bound to one repo.
        if slack_user_id and selected_repo:
            try:
                from integrations.services.slack import SlackService
                SlackService.send_dm(
                    slack_user_id,
                    f"✅ GitHub connected for *{normalized_domain}*! Repository `{selected_repo}` is now linked.\n\n🔍 Starting automatic scan..."
                )
            except Exception as e:
                logger.warning(f"Failed to send Slack notification: {e}")

            # Auto-trigger scan with domain context
            try:
                from integrations.services.github import trigger_scan_async
                trigger_scan_async(slack_user_id, domain=normalized_domain)
            except Exception as e:
                logger.warning(f"Failed to auto-trigger scan for {normalized_domain}: {e}")
        elif slack_user_id:
            try:
                from integrations.services.slack import SlackService
                SlackService.send_dm(
                    slack_user_id,
                    (
                        f"⚠️ GitHub connected for *{normalized_domain}*, but Roo needs exactly one repository selected "
                        "for this domain. Update the installation and select a single repo, then reconnect."
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to send GitHub repo-selection warning: {e}")

    else:
        # ====== USER-LEVEL OAUTH (Legacy) ======
        # Store in UserIntegration
        UserIntegration.objects.update_or_create(
            slack_user_id=slack_user_id,
            defaults={
                "github_access_token": access_token,
                "github_refresh_token": refresh_token,
                "github_token_expires_at": token_expires_at,
                "github_user_name": github_login,
                "github_repo": selected_repo,
                "github_installation_id": installation_id,
                "github_scopes": [],  # GitHub Apps use permissions, not scopes
            }
        )

        # Trigger background scan if repo is selected
        if selected_repo:
            from integrations.services.github import trigger_scan_async
            trigger_scan_async(slack_user_id)

        # Trigger RETRY if job_id was present
        if job_id and selected_repo:
            try:
                from content_factory.models import ContentFactoryJob
                from integrations.services.article_generation import trigger_article_generation

                job = ContentFactoryJob.objects.get(job_id=job_id)
                if job.request_meta:
                    # Re-trigger the generation with original request
                    trigger_article_generation(slack_user_id, job.request_meta)
                    retry_message = "<p>🔄 <strong>Successfully retried your article generation!</strong> You'll get a notification in Slack shortly.</p>"
                else:
                    logger.warning(f"Could not retry job {job_id}: No request_meta found")
            except Exception as e:
                logger.error(f"Failed to auto-retry job {job_id}: {e}")
                retry_message = f"<p style='color:orange'>⚠️ Could not auto-retry: {e}</p>"

    # Build success message
    if selected_repo:
        repo_list_html = f"<p>Linked repository: <strong>{selected_repo}</strong></p>"
    elif len(repos) > 1:
        repo_names_html = ", ".join(repo_names)
        repo_list_html = (
            "<p style='color: orange;'>⚠️ Multiple repositories are selected for this installation.</p>"
            "<p>Roo requires exactly one repository per domain binding. Update the GitHub App installation "
            "and reconnect after narrowing it to a single repository.</p>"
            f"<p style='color: #666; font-size: 0.9em;'>Currently selected: {repo_names_html}</p>"
        )
    else:
        repo_list_html = "<p style='color: orange;'>⚠️ No repositories were selected. Please reinstall the app and select at least one repository.</p>"

    return HttpResponse(f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px; max-width: 600px; margin: 0 auto;">
        <h1 style="color: green;">✅ GitHub App Installed!</h1>
        <p>Connected as <strong>{github_login}</strong></p>
        {domain_display}
        {repo_list_html}
        {retry_message}
        <p>You can now close this window and return to Slack.</p>
    </body>
    </html>
    """)


def github_select_repo(request):
    """
    Handle repository selection from the list.
    """
    if request.method != 'POST':
        return HttpResponseBadRequest("Method not allowed")
        
    slack_user_id = request.POST.get('slack_user_id')
    github_repo = request.POST.get('github_repo')
    
    if not slack_user_id or not github_repo:
        return HttpResponseBadRequest("Missing slack_user_id or selection")

    try:
        integration = UserIntegration.objects.get(slack_user_id=slack_user_id)
        integration.github_repo = github_repo
        integration.save()
        
        # Trigger background scan
        from integrations.services.github import trigger_scan_async
        trigger_scan_async(slack_user_id)
        
        return HttpResponse(f"""
        <html>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <h1 style="color: green;">✅ Success!</h1>
            <p>Repository <strong>{github_repo}</strong> has been linked.</p>
            <p>You can now close this window and return to Slack.</p>
        </body>
        </html>
        """)
    except UserIntegration.DoesNotExist:
        return HttpResponseBadRequest("Integration not found. Please try connecting again.")
