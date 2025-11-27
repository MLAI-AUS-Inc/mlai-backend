import os
import logging
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from customerio import APIClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
signer = TimestampSigner()

# Load API Key from environment
CUSTOMER_IO_API_KEY = os.getenv('CUSTOMERIO_API_KEY')

def generate_magic_link(user, base_url="https://www.med-hack.com"):
    """
    Generates a signed magic link for the user.
    """
    data = {'email': user.email}
    token = signer.sign_object(data)
    return f"{base_url}/verify-email?token={token}"

def verify_magic_link(token, max_age=3600):
    """
    Verifies the magic link token and returns the email if valid.
    """
    try:
        data = signer.unsign_object(token, max_age=max_age)
        email = data.get('email')
        logger.info(f"Magic link token verified successfully for email {email}")
        return email
    except SignatureExpired:
        logger.warning("Magic link token has expired.")
        return None
    except BadSignature:
        logger.warning("Magic link token is invalid.")
        return None

def send_magic_link_email(user, magic_link, message_id="2"):
    """
    Sends the magic link email using Customer.io.
    """
    if not CUSTOMER_IO_API_KEY:
        logger.error("CUSTOMER_IO_API_KEY not set in environment.")
        return

    client = APIClient(CUSTOMER_IO_API_KEY)

    # Prepare display name
    # Prepare display name
    full_name = user.full_name
    first_name = user.first_name
    display_name = full_name or user.email

    # Build request body as dictionary (not SendEmailRequest object)
    request_body = {
        "transactional_message_id": message_id,
        "message_data": {
            "magic_link": magic_link,
            "first_name": first_name or display_name,
            "full_name": display_name,
        },
        "to": user.email,
        "identifiers": {
            "id": str(user.id),
        },
    }

    try:
        response = client.send_email(request_body)
        logger.info(f"Magic link email sent to {user.email} using message_id {message_id}: {response}")
        return response
    except Exception as e:
        logger.error(f"Error sending email to {user.email}: {e}")
        raise e
