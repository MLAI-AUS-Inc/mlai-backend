import os
import logging
import uuid
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from customerio import APIClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
signer = TimestampSigner()
MAGIC_LINK_KIND_USER = "user"

# Load API Key from environment
CUSTOMER_IO_API_KEY = os.getenv('CUSTOMERIO_API_KEY')

def _generate_magic_link_from_payload(data, base_url="https://www.med-hack.com"):
    token = signer.sign_object(data)
    return f"{base_url}/verify-email?token={token}"


def generate_magic_link(user, base_url="https://www.med-hack.com"):
    """
    Generates a signed magic link for an existing user.
    """
    return _generate_magic_link_from_payload(
        {
            'kind': MAGIC_LINK_KIND_USER,
            'email': user.email,
        },
        base_url=base_url,
    )

def verify_magic_link(token, max_age=3600):
    """
    Verifies the magic link token and returns the payload if valid.
    """
    try:
        data = signer.unsign_object(token, max_age=max_age)
        email = data.get('email')
        logger.info("Magic link token verified successfully")
        return data
    except SignatureExpired:
        logger.warning("Magic link token has expired.")
        return None
    except BadSignature:
        logger.warning("Magic link token is invalid.")
        return None

def send_magic_link_email_to_address(
    email,
    magic_link,
    *,
    identifier,
    first_name="",
    full_name="",
    message_id="2",
):
    """
    Sends the magic link email using Customer.io.
    """
    if not CUSTOMER_IO_API_KEY:
        logger.error("CUSTOMER_IO_API_KEY not set in environment.")
        return

    client = APIClient(CUSTOMER_IO_API_KEY)

    # Prepare display name
    # Prepare display name
    display_name = full_name or first_name or email

    # Build request body as dictionary (not SendEmailRequest object)
    request_body = {
        "transactional_message_id": message_id,
        "message_data": {
            "magic_link": magic_link,
            "first_name": first_name or display_name,
            "full_name": display_name,
        },
        "to": email,
        "identifiers": {
            "id": str(identifier),
        },
    }

    try:
        response = client.send_email(request_body)
        logger.info("Magic link email sent using message_id=%s", message_id)
        return response
    except Exception as exc:
        logger.error("Magic link email delivery failed error_type=%s", exc.__class__.__name__)
        raise


APP_NAMES = {
    "hospital": "Med-Hack",
    "esafety": "the eSafety hackathon",
    "watt-the-hack": "Watt the Hack",
    "founder-tools": "MLAI Founder Tools",
    "content-factory": "Content Factory",
    "community-chat": "MLAI Chat",
    "admin": "MLAI operations",
}


def app_display_name(app_context):
    """Human name of the app a sign-in link opens, for the email copy."""
    return APP_NAMES.get(str(app_context or ""), "MLAI")


def _send_via_system_email(message_key, user, context, idempotency_key):
    """Send through our own Resend sender (system_email). Raises on failure."""
    from system_email import api as system_email

    context = {"first_name": user.first_name or "", **context}
    return system_email.send(
        message_key,
        to=user.email,
        idempotency_key=idempotency_key,
        context=context,
        user_id=user.id,
    )


def send_magic_link_email(user, magic_link, message_id="2", app_context=None):
    """
    Sends the magic link email for an existing user: through system_email when
    ``magic_link`` is switched to Resend, otherwise through Customer.io.
    """
    from system_email.api import routes_to_resend

    if routes_to_resend("magic_link"):
        return _send_via_system_email(
            "magic_link",
            user,
            {"magic_link": magic_link, "app_name": app_display_name(app_context), "expires_minutes": 60},
            idempotency_key=f"magic/{uuid.uuid4()}",
        )
    return send_magic_link_email_to_address(
        user.email,
        magic_link,
        identifier=user.id,
        first_name=user.first_name,
        full_name=user.full_name,
        message_id=message_id,
    )


def send_password_reset_email(user, reset_link, delivery_id=None):
    """Send the one-use MLAI password setup/reset link without logging it.

    ``delivery_id`` (the outbox row) keeps Resend retries idempotent.
    """
    from django.conf import settings
    from system_email.api import routes_to_resend

    if routes_to_resend("password_reset"):
        ttl_minutes = max(1, int(getattr(settings, "PASSWORD_RESET_TTL_SECONDS", 3600)) // 60)
        return _send_via_system_email(
            "password_reset",
            user,
            {"reset_link": reset_link, "expires_minutes": ttl_minutes},
            idempotency_key=f"pwreset/{delivery_id}" if delivery_id else f"pwreset/{uuid.uuid4()}",
        )
    message_id = os.getenv('CUSTOMERIO_PASSWORD_RESET_MESSAGE_ID', '2')
    return send_magic_link_email_to_address(
        user.email,
        reset_link,
        identifier=user.id,
        first_name=user.first_name,
        full_name=user.full_name,
        message_id=message_id,
    )
