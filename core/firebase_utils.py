# core/firebase_utils.py
import firebase_admin
from firebase_admin import credentials, firestore, storage
import logging
import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Firebase credentials from environment variables
FIREBASE_PROJECT_ID = os.getenv('FIREBASE_PROJECT_ID')
FIREBASE_CLIENT_EMAIL = os.getenv('FIREBASE_CLIENT_EMAIL')
FIREBASE_PRIVATE_KEY = os.getenv('FIREBASE_PRIVATE_KEY')
FIREBASE_PRIVATE_KEY_ID = os.getenv('FIREBASE_PRIVATE_KEY_ID')
FIREBASE_CLIENT_ID = os.getenv('FIREBASE_CLIENT_ID')
FIREBASE_CERT_URL = os.getenv('FIREBASE_CERT_URL')
FIREBASE_STORAGE_BUCKET = os.getenv('FIREBASE_STORAGE_BUCKET', 'mlai-main-website.firebasestorage.app')

def initialize_firebase():
    """Initialize Firebase Admin SDK"""
    logger.info(f"Initializing Firebase with project ID: {FIREBASE_PROJECT_ID}, Storage bucket: {FIREBASE_STORAGE_BUCKET}")
    
    try:
        # Check if already initialized
        if firebase_admin._apps:
            logger.info("Using existing Firebase app")
            return firestore.client()

        # Construct credentials dictionary
        # Handle potential newline escaping in private key
        private_key = FIREBASE_PRIVATE_KEY
        if private_key and '\\n' in private_key:
            private_key = private_key.replace('\\n', '\n')

        firebase_credentials = {
            "type": "service_account",
            "project_id": FIREBASE_PROJECT_ID,
            "private_key_id": FIREBASE_PRIVATE_KEY_ID,
            "private_key": private_key,
            "client_email": FIREBASE_CLIENT_EMAIL,
            "client_id": FIREBASE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": FIREBASE_CERT_URL,
        }

        # Validate critical credentials
        if not all([FIREBASE_PROJECT_ID, private_key, FIREBASE_CLIENT_EMAIL]):
            logger.warning("Missing critical Firebase credentials. Firebase features may not work.")
            # We might not want to raise here to allow the app to start, 
            # but upload will fail later.
            return None

        cred = credentials.Certificate(firebase_credentials)
        
        firebase_admin.initialize_app(cred, {
            'storageBucket': FIREBASE_STORAGE_BUCKET
        })
        logger.info("Firebase app initialized successfully")
        
        firestore_client = firestore.client()
        return firestore_client
    except Exception as e:
        logger.error(f"Error initializing Firebase: {str(e)}", exc_info=True)
        # Don't crash the app on init failure, but log it
        return None

# Initialize on module load
try:
    db = initialize_firebase()
except Exception as e:
    logger.error(f"Failed to initialize Firebase on module load: {str(e)}")
    db = None

def get_storage_bucket():
    """Get Firebase storage bucket instance"""
    try:
        bucket = storage.bucket()
        return bucket
    except Exception as e:
        logger.error(f"Error getting storage bucket: {str(e)}")
        # Try to re-initialize
        initialize_firebase()
        return storage.bucket()

def upload_file_to_storage(file_obj, destination_path, content_type=None):
    """
    Upload a file to Firebase Storage
    
    Args:
        file_obj: File object to upload (file-like object)
        destination_path: Path where to store the file in Firebase Storage
        content_type: Content type of the file (optional)
    
    Returns:
        public_url: Public URL (if made public) or signed URL (if private)
    """
    logger.info(f"Starting file upload to Firebase Storage. Destination: {destination_path}")
    try:
        bucket = get_storage_bucket()
        blob = bucket.blob(destination_path)
        
        # Ensure file pointer is at start
        if hasattr(file_obj, 'tell') and hasattr(file_obj, 'seek'):
            if file_obj.tell() > 0:
                file_obj.seek(0)
        
        blob.upload_from_file(file_obj, content_type=content_type)
        logger.info(f"Upload completed successfully to {destination_path}")

        # For avatars, we generally want them to be publicly accessible or signed long-term.
        # The user's example used signed URLs for avatars.
        # Let's use signed URLs with long expiration (e.g., 7 days) or make public.
        # Making public is easier for a website avatar.
        
        # If the user wants to use signed URLs as per their snippet:
        # "For avatars, generate a signed URL"
        
        # However, for a user profile avatar on a public/semi-public site, 
        # making it public is usually better so it doesn't expire.
        # But I will follow the user's snippet logic which used signed URLs for "avatars/"
        # and public for "blurred-avatars/".
        
        # Actually, let's make it public for simplicity and reliability unless privacy is strict.
        # The user's snippet had:
        # if "avatars/" in destination_path: ... generate_signed_url ...
        
        # I'll stick to signed URL for now to match their code, but maybe increase expiration or just make public if it's easier.
        # Let's make it public for now as it's a hackathon app and expiring avatars are annoying.
        # Wait, the user specifically asked "save it in firebase storage with a url i can reference later".
        # A signed URL expires. A public URL does not.
        # I will make it public.
        
        blob.make_public()
        return blob.public_url

    except Exception as e:
        logger.error(f'Failed to upload file to Firebase Storage: {str(e)}', exc_info=True)
        raise
