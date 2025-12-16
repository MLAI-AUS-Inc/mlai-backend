import secrets
import urllib.parse
import requests
from django.conf import settings
from django.shortcuts import redirect
from django.http import HttpResponseBadRequest, HttpResponse
from django.contrib.auth.decorators import login_required
from .models import GoogleConnection

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

    # Redirect to some settings page or success page
    # For now, just a simple response
    return HttpResponse(f"Successfully connected Google account: {google_email}")

from django.http import JsonResponse
from django.contrib.auth import get_user_model
from .services import fetch_recent_subject_lines

User = get_user_model()

def test_gmail_fetch(request):
    """
    Test endpoint to fetch recent Gmail subjects.
    Allows passing user_id param for easy curl testing without auth cookies.
    """
    user_id = request.GET.get("user_id")
    if user_id:
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return JsonResponse({"error": "User not found"}, status=404)
    else:
        if not request.user.is_authenticated:
             return JsonResponse({"error": "Not authenticated or missing user_id"}, status=401)
        user = request.user

    subjects = fetch_recent_subject_lines(user)
    return JsonResponse({"subjects": subjects})
