import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from django.conf import settings
from integrations.models import GoogleConnection

def get_refreshed_credentials(connection: GoogleConnection):
    """
    Constructs a Credentials object from the stored refresh_token.
    The library handles the refresh flow automatically when requests are made
    if we provide the token_uri, client_id, and client_secret.
    """
    creds = Credentials(
        token=None,  # We don't have a valid access token right now
        refresh_token=connection.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=connection.scope.split(" ") if connection.scope else [],
    )
    return creds

def fetch_last_month_emails(connection: GoogleConnection):
    """
    Crawls Gmail (or other scopes) for the last 30 days of messages.
    Returns a list of simplified message objects.
    """
    creds = get_refreshed_credentials(connection)
    service = build('gmail', 'v1', credentials=creds)

    # Calculate date range
    # "newer_than:30d" is a helpful Gmail search operator.
    # We also exclude TRASH and SPAM by default in the query if we want, or just query normally.
    query = "newer_than:30d -label:TRASH -label:SPAM"

    messages = []
    page_token = None

    # 1. List messages
    while True:
        results = service.users().messages().list(
            userId='me',
            q=query,
            pageToken=page_token,
            maxResults=500  # max allowed is usually around 500
        ).execute()

        msg_list = results.get('messages', [])
        messages.extend(msg_list)

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    # 2. Get details for each message
    # Optimally, we could batch this or just do it sequentially for now.
    # For a few hundred emails, sequential is often acceptable in a background job.
    # We might want 'format=full' (default) or 'format=metadata' depending on needs.
    # The user asked for "format=full if you need content", effectively implies full is okay.

    detailed_messages = []
    
    # Limit to e.g. 50 most recent for the "summary" to avoid hitting limits too hard if mailbox is huge?
    # User said "Crawl the last month... without scraping the whole mailbox".
    # Relying on newer_than:30d is good.
    
    for msg in messages:
        # Avoid try/except block if possible, but network calls can fail.
        try:
            full_msg = service.users().messages().get(
                userId='me',
                id=msg['id'],
                format='full'
            ).execute()
            detailed_messages.append(full_msg)
        except Exception as e:
            print(f"Failed to fetch message {msg['id']}: {e}")

    return detailed_messages
