"""Keep public-bot and user-OAuth app authority and request budgets distinct."""
import hashlib
import hmac
import json
import time

from django.conf import settings


def user_app_id():
    """Resolve the app that issued owner OAuth tokens (may be the bot app)."""
    return str(getattr(settings, 'MESSAGE_SYNC_SLACK_USER_APP_ID', '') or
               getattr(settings, 'MESSAGE_SYNC_SLACK_APP_ID', '') or '')


def authorization_token(app_id):
    """Never use one app's app-level credential for another app's event context."""
    public_id = str(getattr(settings, 'MESSAGE_SYNC_SLACK_APP_ID', '') or '')
    if app_id and app_id == public_id:
        return str(getattr(settings, 'MESSAGE_SYNC_SLACK_APP_TOKEN', '') or '')
    if app_id and app_id == user_app_id():
        return str(getattr(settings, 'MESSAGE_SYNC_SLACK_USER_APP_TOKEN', '') or '')
    return ''


def valid_callback_signature(body, timestamp, signature):
    """Bind each event's claimed app to that app's verified signing secret."""
    if not timestamp or not signature:
        return False
    try:
        source_time = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - source_time) > 300:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    app_id = str(payload.get('api_app_id') or '')
    public_id = str(getattr(settings, 'MESSAGE_SYNC_SLACK_APP_ID', '') or '')
    public_secret = str(getattr(settings, 'SLACK_BRIDGE_SIGNING_SECRET', '') or '')
    private_id = str(getattr(settings, 'MESSAGE_SYNC_SLACK_USER_APP_ID', '') or '')
    private_secret = str(getattr(settings, 'MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET', '') or '')
    # Slack's signed URL challenge has no app ID and creates no message state.
    if payload.get('type') == 'url_verification':
        secrets = [public_secret, private_secret if private_id else '']
    elif private_id and private_id != public_id and app_id == private_id:
        secrets = [private_secret]
    elif not public_id or app_id == public_id:
        secrets = [public_secret]
    else:
        return False
    signed = b'v0:' + str(source_time).encode() + b':' + body
    return any(secret and hmac.compare_digest(
        'v0=' + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest(), str(signature).strip(),
    ) for secret in secrets)
