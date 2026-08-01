import os
import logging
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


def send_magic_link_email(user, magic_link, message_id="2"):
    """
    Sends the magic link email using Customer.io for an existing user.
    """
    return send_magic_link_email_to_address(
        user.email,
        magic_link,
        identifier=user.id,
        first_name=user.first_name,
        full_name=user.full_name,
        message_id=message_id,
    )
