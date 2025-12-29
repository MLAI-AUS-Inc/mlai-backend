import secrets
import urllib.parse
import requests
from django.conf import settings
from django.shortcuts import redirect
from django.http import HttpResponseBadRequest, HttpResponse
from django.middleware.csrf import get_token
from django.contrib.auth.decorators import login_required
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
    Initiates GitHub OAuth flow.
    Expects 'slack_user_id' in query params to link the token.
    """
    slack_user_id = request.GET.get('slack_user_id')
    if not slack_user_id:
        return HttpResponseBadRequest("Missing slack_user_id")

    # State allows us to pass the slack_user_id through the OAuth flow
    # We encrypt or sign it? For simplicity, we just pass it in state, 
    # but strictly it should be unpredictable to prevent CSRF.
    # Since this is a bot flow, we'll use a random token + slack_id.
    
    rand_token = secrets.token_urlsafe(16)
    state = f"{slack_user_id}::{rand_token}"
    request.session["github_oauth_state"] = state

    params = {
        "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        "scope": "repo read:user",
        "state": state,
    }

    url = "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(params)
    return redirect(url)

def github_callback(request):
    """
    Handles GitHub OAuth callback.
    """
    code = request.GET.get("code")
    state = request.GET.get("state")
    
    if not code:
        return HttpResponseBadRequest("Missing code")

    # Verify state matches session
    # expected_state = request.session.get("github_oauth_state")
    # if not state or state != expected_state:
    #     return HttpResponseBadRequest("Invalid state or session expired")
    
    # Extract slack_user_id from state
    try:
        slack_user_id, _ = state.split("::")
    except ValueError:
        return HttpResponseBadRequest("Invalid state format")

    # Exchange code for token
    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        },
        timeout=20,
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()
    
    if "error" in token_data:
        return HttpResponseBadRequest(f"GitHub Error: {token_data.get('error_description')}")

    access_token = token_data.get("access_token")
    scope = token_data.get("scope", "")

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

    # Store in UserIntegration
    UserIntegration.objects.update_or_create(
        slack_user_id=slack_user_id,
        defaults={
            "github_access_token": access_token,
            "github_user_name": github_login,
            "github_scopes": scope.split(",") if scope else [],
        }
    )

    # ------------------------------------------------------------------
    # Repository Selection Step
    # ------------------------------------------------------------------
    # Fetch user's repositories (limit 100 recent)
    try:
        repos_resp = requests.get(
            "https://api.github.com/user/repos",
            params={"sort": "updated", "per_page": "100", "type": "all"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10,
        )
        repos_resp.raise_for_status()
        repos = repos_resp.json()
    except Exception as e:
        # Fallback if fetching repos fails: just show connected message
        return HttpResponse(f"✅ GitHub connected for Slack user {slack_user_id}! (Could not list repos: {e}) You can close this window.")

    # Render simple Selection UI
    csrf_token = get_token(request)
    repo_options = ""
    for repo in repos:
        full_name = repo['full_name']
        private_badge = "🔒" if repo['private'] else "🌍"
        repo_options += f"""
        <div style="margin-bottom: 8px; padding: 8px; border: 1px solid #eee; border-radius: 4px;">
            <label style="display: flex; align-items: center; cursor: pointer;">
                <input type="radio" name="github_repo" value="{full_name}" style="margin-right: 10px;">
                <span style="font-weight: bold;">{full_name}</span>
                <span style="font-size: 0.8em; color: #666; margin-left: auto;">{private_badge}</span>
            </label>
        </div>
        """

    html = f"""
    <html>
    <body style="font-family: sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2>Select Repository</h2>
        <p>GitHub connected successfully as <strong>{github_login}</strong>.</p>
        <p>Please select the repository you want to link to this project:</p>
        
        <form action="/integrations/connect/github/select" method="POST">
            <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
            <input type="hidden" name="slack_user_id" value="{slack_user_id}">
            
            <div style="max-height: 400px; overflow-y: auto; margin-bottom: 20px;">
                {repo_options}
            </div>
            
            <button type="submit" style="background: #007bff; color: white; border: none; padding: 10px 20px; font-size: 16px; border-radius: 4px; cursor: pointer;">
                Link Repository
            </button>
        </form>
    </body>
    </html>
    """
    
    return HttpResponse(html)


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
