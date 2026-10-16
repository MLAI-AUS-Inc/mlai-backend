"""Bounded private Nostr and idempotent Resend transport for fixed goal notices."""
import base64
import hashlib
import json
import secrets
from uuid import UUID
from urllib.parse import urlsplit
from django.conf import settings
from django.utils import timezone
from agent_runtime import native_crypto
from agent_runtime.webhook_transport import post_webhook, CallbackEndpointError
from .website_connections import require_unlocked_remote_call


class NotificationTransportError(RuntimeError):
    """A fixed transport outcome; provider bodies and credentials are never retained."""
    def __init__(self, outcome, *, retryable=True):
        self.outcome,self.retryable = outcome,retryable
        super().__init__(outcome)


def notifier_key():
    """Use a dedicated registered Chat member, never a customer or bridge signing key."""
    try:return native_crypto.signing_key(getattr(settings,"ARTICLES_GOAL_NOTIFIER_PRIVATE_KEY",""))
    except Exception as exc:raise NotificationTransportError("chat_unconfigured",retryable=False) from exc


def relay_origin():
    """Restrict notification publishing to the configured first-party Chat origin."""
    try:
        parts=urlsplit(getattr(settings,"COMMUNITY_CHAT_RELAY_URL",""))
        if parts.scheme not in {"wss","https"} or parts.hostname not in {"chat.mlai.au","www.chat.mlai.au"} or parts.port not in {None,443} or parts.path not in {"","/"} or parts.username or parts.password or parts.query or parts.fragment:raise ValueError()
        return "https://"+parts.netloc.rstrip("/")
    except ValueError as exc:raise NotificationTransportError("chat_unconfigured",retryable=False) from exc


def post_event(event):
    """Publish a saved event ID with fresh payload-bound NIP-98 transport authentication."""
    key=notifier_key()
    if event.get("pubkey") != native_crypto.public_key(key):raise NotificationTransportError("chat_signer_changed",retryable=False)
    url=relay_origin()+"/events";body=native_crypto.canonical_json(event).encode()
    auth=native_crypto.sign_event(key,kind=27235,created_at=timezone.now().timestamp(),content="",
        tags=[["u",url],["method","POST"],["payload",hashlib.sha256(body).hexdigest()],["nonce",secrets.token_hex(16)]])
    require_unlocked_remote_call()
    try:
        response=post_webhook(url,body,{"Authorization":"Nostr "+base64.b64encode(native_crypto.canonical_json(auth).encode()).decode()},timeout=10,max_response_bytes=16384)
        value=json.loads(response.body)
    except (CallbackEndpointError,ValueError) as exc:raise NotificationTransportError("chat_response_unavailable") from exc
    if response.status in {408,429} or response.status>=500:raise NotificationTransportError("chat_temporarily_unavailable")
    if response.status!=200 or not isinstance(value,dict) or value.get("accepted") is not True or value.get("event_id")!=event["id"]:
        raise NotificationTransportError("chat_rejected",retryable=False)
    return value


def open_dm(pubkeys, notice_id):
    """Fresh open commands recover a lost response while the relay reuses participants' DM."""
    key=notifier_key()
    event=native_crypto.sign_event(key,kind=41010,created_at=timezone.now().timestamp(),
        tags=[["p",pubkey] for pubkey in sorted(pubkeys)]+[["mlai-goal-notice",notice_id],["nonce",secrets.token_hex(16)]],content="")
    result=post_event(event)
    try:
        message=result.get("message")
        if not isinstance(message,str) or not message.startswith("response:"):raise ValueError()
        return str(UUID(json.loads(message[len("response:"):])["channel_id"]))
    except (ValueError,KeyError,TypeError) as exc:raise NotificationTransportError("chat_channel_unavailable") from exc


def message_event(channel,pubkeys,notice,created_at):
    """Create one immutable channel message; replay always uses this saved signed event."""
    return native_crypto.sign_event(notifier_key(),kind=9,created_at=created_at,
        tags=[["h",str(UUID(channel))],["mlai-goal-notice",notice["id"]]]+[["p",key] for key in sorted(pubkeys)],
        content=notice["text"]+"\n\n["+("View articles" if notice["state"]=="done" else "Continue setup")+"]("+notice["link"]+")")


def email_payload(channel,notice):
    """Freeze the entire provider request so a later config change cannot alter retries."""
    sender=str(getattr(settings,"RESEND_FROM_EMAIL","") or "Roo <notifications@mlai.au>")
    return {"from":sender,"to":[channel.route_id],"subject":notice["subject"],"text":notice["text"]+"\n\n"+notice["link"]}


def post_email(payload,notice_id):
    """Resend alone provides the verified deduplication contract needed by this outbox."""
    api_key=str(getattr(settings,"RESEND_API_KEY","") or "")
    if not api_key:raise NotificationTransportError("email_unconfigured",retryable=False)
    require_unlocked_remote_call()
    try:
        response=post_webhook("https://api.resend.com/emails",native_crypto.canonical_json(payload).encode(),
            {"Authorization":"Bearer "+api_key,"Idempotency-Key":"articles-goal/"+notice_id},timeout=10,max_response_bytes=16384)
        value=json.loads(response.body)
    except (CallbackEndpointError,ValueError) as exc:raise NotificationTransportError("email_response_unavailable") from exc
    if response.status in {408,429} or response.status>=500 or response.status==409 and isinstance(value,dict) and value.get("name")=="concurrent_idempotent_requests":
        raise NotificationTransportError("email_temporarily_unavailable")
    if response.status not in {200,201} or not isinstance(value,dict):raise NotificationTransportError("email_rejected",retryable=False)
    try:return str(UUID(value["id"]))
    except (ValueError,TypeError,KeyError) as exc:raise NotificationTransportError("email_response_unavailable") from exc
