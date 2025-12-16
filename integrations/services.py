import requests
from django.conf import settings
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from .models import GoogleConnection

def get_gmail_service(google_conn: GoogleConnection):
    """
    Returns an authenticated Gmail API service instance.
    Refreshes the access token if necessary.
    """
    # Create credentials with the stored refresh token
    creds = Credentials(
        token=None,  # We don't store access_token persistently, or we can fetch a fresh one
        refresh_token=google_conn.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=settings.GOOGLE_OAUTH_SCOPES,
    )
    
    # The library handles refreshing automatically when we make a request, 
    # IF we provided a valid refresh token and client config.
    
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def fetch_recent_subject_lines(user, days=30):
    try:
        conn = user.google_connection
    except GoogleConnection.DoesNotExist:
        return []

    service = get_gmail_service(conn)
    query = f"newer_than:{days}d -in:spam -in:trash"
    
    results = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = results.get("messages", [])
    
    subjects = []
    if messages:
        for msg in messages:
            # We can use batching here for optimization, but doing one-by-one for MVP
            txt = service.users().messages().get(userId="me", id=msg['id'], format='metadata').execute()
            headers = txt.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "(No Subject)")
            subjects.append(subject)
            
    return subjects
