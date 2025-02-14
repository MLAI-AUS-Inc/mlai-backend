from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
import os
from customerio import APIClient
from dotenv import load_dotenv
import logging


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)   

signer = TimestampSigner()

load_dotenv()
customer_io_api_key = os.getenv('CUSTOMER_IO_API_KEY')

def generate_magic_link(user, practice_id=None):
    data = {'email': user.email}
    if practice_id:
        data['practice_id'] = practice_id
    token = signer.sign_object(data)
    magic_link = f"https://www.med-hack.com/verify-email?token={token}"
    return magic_link

def verify_magic_link(token, max_age=3600):
    try:
        data = signer.unsign_object(token, max_age=max_age)
        email = data.get('email')
        logger.info(f"Magic link token verified successfully for email {email}")
        return email
    except SignatureExpired:
        logger.warning("Magic link token has expired.")
        return None, None
    except BadSignature:
        logger.warning("Magic link token is invalid.")
        return None, None

def send_magic_link_email(user, magic_link):
    client = APIClient(customer_io_api_key)
    
    request_body = {
        "transactional_message_id": "6",  
        "message_data": {
            "magic_link": magic_link,
            "full_name": user.full_name,
        },
        "to": user.email, 
        "identifiers": {
            "id": str(user.id),
        },
    }
    
    try:
        response = client.send_email(request_body) 
        logging.info(f"Email sent successfully: {response}")
        return response
    except Exception as e:
        logging.error(f"Error sending email: {str(e)}")
        raise