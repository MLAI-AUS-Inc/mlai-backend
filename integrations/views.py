import secrets
import urllib.parse
import requests
from datetime import timedelta
from django.conf import settings
from django.shortcuts import redirect
from django.http import HttpResponseBadRequest, HttpResponse
from django.middleware.csrf import get_token
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from .models import GoogleConnection, UserIntegration

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

@login_required
def google_connect(request):
    """
    Initiates the Google OAuth flow.
    """
    state = secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state

    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # helps ensure refresh_token is returned
        "include_granted_scopes": "true",
        "state": state,
    }

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(url)

@login_required
def google_callback(request):
    """
    Handles the callback from Google.
    """
    # 1) Validate state
    state = request.GET.get("state")
    if not state or state != request.session.get("google_oauth_state"):
        return HttpResponseBadRequest("Invalid state")

    # 2) Handle errors
    if request.GET.get("error"):
        return HttpResponseBadRequest(f"OAuth error: {request.GET.get('error')}")

    code = request.GET.get("code")
    if not code:
        return HttpResponseBadRequest("Missing code")

    # 3) Exchange code for tokens
    token_resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    scope = token_data.get("scope", "")

    # 4) Fetch the user's Google email
    ui_resp = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    ui_resp.raise_for_status()
    google_email = ui_resp.json().get("email")

    if not refresh_token:
        # If user reconnects/modifies scope without prompt=consent, refresh token might be missing.
        # But we force prompt=consent in google_connect, so it should be there.
        # If still missing, we might want to check if we already have one.
        pass

    # Update or create the connection
    defaults = {
        "google_email": google_email or "",
        "scope": scope,
    }
    if refresh_token:
        defaults["refresh_token"] = refresh_token
        
    GoogleConnection.objects.update_or_create(
        user=request.user,
        defaults=defaults,
    )

    # Redirect to frontend
    return redirect(f"{settings.FRONTEND_URL}/settings?gmail_connected=true")

def github_connect(request):
    """
    Initiates GitHub App installation flow.
    Expects 'slack_user_id' in query params to link the token.
    
    This uses the GitHub App installation flow which shows the native
    repository selection UI on GitHub's side.
    """
    slack_user_id = request.GET.get('slack_user_id')
    if not slack_user_id:
        return HttpResponseBadRequest("Missing slack_user_id")

    # State allows us to pass the slack_user_id and optional job_id through the OAuth flow
    rand_token = secrets.token_urlsafe(16)
    job_id = request.GET.get('job_id', '')
    state = f"{slack_user_id}::{rand_token}::{job_id}"
    request.session["github_oauth_state"] = state

    # GitHub App installation URL - this shows the native repo selector
    # App slug is "mlai-tools" based on the public link https://github.com/apps/mlai-tools
    app_slug = "mlai-tools"
    install_url = f"https://github.com/apps/{app_slug}/installations/new"
    
    params = {
        "state": state,
    }
    
    url = install_url + "?" + urllib.parse.urlencode(params)
    return redirect(url)

def github_callback(request):
    """
    Handles GitHub App installation callback.
    
    When a user installs the GitHub App, GitHub redirects here with:
    - installation_id: The ID of the GitHub App installation
    - setup_action: "install" or "update"
    - code: Authorization code to exchange for user access token
    - state: Our state parameter with slack_user_id
    """
    code = request.GET.get("code")
    state = request.GET.get("state")
    installation_id = request.GET.get("installation_id")
    setup_action = request.GET.get("setup_action")
    
    if not code:
        return HttpResponseBadRequest("Missing code")
    
    if not installation_id:
        return HttpResponseBadRequest("Missing installation_id - this endpoint requires GitHub App installation")
    
    # Extract slack_user_id and job_id from state
    job_id = None
    try:
        parts = state.split("::")
        slack_user_id = parts[0]
        # parts[1] is rand_token
        if len(parts) >= 3:
            job_id = parts[2]
            if job_id == 'None': job_id = None # handle potential str conversion artifacts
    except (ValueError, AttributeError):
        return HttpResponseBadRequest("Invalid state format")

    # Exchange code for user access token
    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "code": code,
        },
        timeout=20,
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
        timeout=20,
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
            timeout=20,
        )
        repos_resp.raise_for_status()
        repos_data = repos_resp.json()
        repos = repos_data.get("repositories", [])
    except Exception as e:
        repos = []
    
    # If user selected exactly one repo, auto-link it
    # Otherwise, show selection UI or list what they selected
    selected_repo = None
    if len(repos) == 1:
        selected_repo = repos[0]["full_name"]
    elif len(repos) > 1:
        # Multiple repos selected - pick the first one or show selection
        # For now, we'll use the first one but show a message
        selected_repo = repos[0]["full_name"]

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
    retry_message = ""
    if job_id and selected_repo:
        try:
            from core.models import ContentFactoryJob
            from integrations.services.article_generation import trigger_article_generation
            
            job = ContentFactoryJob.objects.get(job_id=job_id)
            if job.request_meta:
                 # Re-trigger the generation with original request
                 # We don't need to specify domain/topic/keyword again if request_meta has it
                 # But trigger_article_generation expects them in article_request dict
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
        if len(repos) > 1:
            other_repos = [r["full_name"] for r in repos[1:]]
            repo_list_html += f"<p style='color: #666; font-size: 0.9em;'>Other selected repos: {', '.join(other_repos)}</p>"
    else:
        repo_list_html = "<p style='color: orange;'>⚠️ No repositories were selected. Please reinstall the app and select at least one repository.</p>"

    return HttpResponse(f"""
    <html>
    <body style="font-family: sans-serif; text-align: center; padding-top: 50px; max-width: 600px; margin: 0 auto;">
        <h1 style="color: green;">✅ GitHub App Installed!</h1>
        <p>Connected as <strong>{github_login}</strong></p>
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

@login_required
def get_gmail_emails(request):
    """
    API endpoint for frontend to get recent emails.
    """
    subjects = fetch_recent_subject_lines(request.user)
    return JsonResponse({"subjects": subjects})

from django.http import JsonResponse
from django.contrib.auth import get_user_model
from .services import fetch_recent_subject_lines

User = get_user_model()

@login_required
def test_gmail_fetch(request):
    """
    Test endpoint to fetch recent Gmail subjects.
    Only allows authenticated users.
    """
    subjects = fetch_recent_subject_lines(request.user)
    return JsonResponse({"subjects": subjects})
