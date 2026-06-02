# core/firebase_utils.py
import firebase_admin
from firebase_admin import auth, credentials, db as rtdb, firestore, storage
from google.api_core.exceptions import NotFound
import logging
import os
import uuid
import urllib.parse
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
# Realtime Database URL — needed for the Watt smart-home device-command bus (RTDB reads/writes).
FIREBASE_DATABASE_URL = os.getenv(
    'FIREBASE_DATABASE_URL',
    'https://mlai-main-website-default-rtdb.asia-southeast1.firebasedatabase.app',
)

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
        
        # Try to decode base64 if it doesn't look like a PEM key yet
        if private_key and not private_key.strip().startswith('-----BEGIN PRIVATE KEY-----'):
            try:
                import base64
                decoded = base64.b64decode(private_key).decode('utf-8')
                if '-----BEGIN PRIVATE KEY-----' in decoded:
                    private_key = decoded
            except Exception:
                pass

        if private_key:
            # Replace literal \n with actual newline
            if '\\n' in private_key:
                private_key = private_key.replace('\\n', '\n')
            
            # Fix potential double newlines from user copy-paste or echo
            private_key = private_key.replace('\n\n', '\n')

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
            'storageBucket': FIREBASE_STORAGE_BUCKET,
            'databaseURL': FIREBASE_DATABASE_URL,
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


def get_rtdb_reference(path):
    """Return a Realtime Database reference at ``path`` (ensures Firebase is initialized).

    The default Firebase app must have been initialized with ``databaseURL``
    (see ``initialize_firebase``). Used by the Watt smart-home command bus to read
    observations and write device commands under ``classes/{classId}/hackathon/...``.
    """
    initialize_firebase()
    return rtdb.reference(path)


def rtdb_get(path):
    """Read and return the value at a Realtime Database path (``None`` if absent)."""
    return get_rtdb_reference(path).get()


def rtdb_set(path, value):
    """Overwrite the value at a Realtime Database path."""
    get_rtdb_reference(path).set(value)


def rtdb_update(path, value):
    """Shallow-merge child keys at a Realtime Database path."""
    get_rtdb_reference(path).update(value)


def rtdb_delete(path):
    """Delete the node at a Realtime Database path."""
    get_rtdb_reference(path).delete()


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

        # Generate a download token for the file
        # This allows access via the Firebase Storage URL format without making the bucket public
        token = str(uuid.uuid4())
        metadata = {"firebaseStorageDownloadTokens": token}
        blob.metadata = metadata
        blob.patch()
        
        # Construct the public URL (Firebase format)
        # We must URL-encode the path (including slashes)
        encoded_name = urllib.parse.quote(destination_path, safe='')
        public_url = f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{encoded_name}?alt=media&token={token}"

        return public_url

    except Exception as e:
        logger.error(f'Failed to upload file to Firebase Storage: {str(e)}', exc_info=True)
        raise


def create_firebase_custom_token(uid, claims):
    """Mint a Firebase custom token with developer claims."""
    initialize_firebase()
    token = auth.create_custom_token(uid, developer_claims=claims)
    return token.decode("utf-8") if isinstance(token, bytes) else str(token)


def firebase_storage_media_url(destination_path, token=None):
    """Build a Firebase Storage media URL for a token-protected object."""
    bucket = get_storage_bucket()
    encoded_name = urllib.parse.quote(destination_path, safe='')
    token_param = f"&token={token}" if token else ""
    return f"https://firebasestorage.googleapis.com/v0/b/{bucket.name}/o/{encoded_name}?alt=media{token_param}"


def create_signed_upload_url(destination_path, content_type, expires_in=timedelta(minutes=15)):
    """Create a signed PUT URL for direct browser uploads to Firebase/GCS."""
    bucket = get_storage_bucket()
    blob = bucket.blob(destination_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=expires_in,
        method="PUT",
        content_type=content_type,
    )


def finalize_uploaded_storage_object(destination_path, content_type=None):
    """Verify an uploaded object exists and attach a Firebase download token."""
    bucket = get_storage_bucket()
    blob = bucket.blob(destination_path)
    try:
        blob.reload()
    except NotFound as exc:
        raise FileNotFoundError(destination_path) from exc

    token = str(uuid.uuid4())
    metadata = dict(blob.metadata or {})
    metadata["firebaseStorageDownloadTokens"] = token
    blob.metadata = metadata
    if content_type:
        blob.content_type = content_type
    blob.patch()

    return {
        "url": firebase_storage_media_url(destination_path, token=token),
        "contentType": blob.content_type or content_type or "",
        "fileSizeBytes": int(blob.size or 0),
    }


def finalize_private_uploaded_storage_object(destination_path, content_type=None):
    """Verify a private uploaded object exists without creating a download token."""
    bucket = get_storage_bucket()
    blob = bucket.blob(destination_path)
    try:
        blob.reload()
    except NotFound as exc:
        raise FileNotFoundError(destination_path) from exc

    metadata = dict(blob.metadata or {})
    metadata.pop("firebaseStorageDownloadTokens", None)
    blob.metadata = metadata
    if content_type:
        blob.content_type = content_type
    blob.patch()

    return {
        "contentType": blob.content_type or content_type or "",
        "fileSizeBytes": int(blob.size or 0),
        "updated": blob.updated.isoformat() if getattr(blob, "updated", None) else None,
    }


def create_signed_read_url(destination_path, expires_in=timedelta(minutes=5)):
    """Create a short-lived signed GET URL for a private Firebase/GCS object."""
    bucket = get_storage_bucket()
    blob = bucket.blob(destination_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=expires_in,
        method="GET",
    )


def download_storage_object_bytes(destination_path):
    """Download a private Firebase/GCS object as bytes."""
    bucket = get_storage_bucket()
    blob = bucket.blob(destination_path)
    try:
        return blob.download_as_bytes()
    except NotFound as exc:
        raise FileNotFoundError(destination_path) from exc


def delete_storage_object(destination_path):
    """Delete a Firebase/GCS object if it exists."""
    bucket = get_storage_bucket()
    blob = bucket.blob(destination_path)
    try:
        blob.delete()
        return True
    except NotFound:
        return False


def configure_storage_cors(origins=None):
    """Configure CORS for direct browser uploads to Firebase/GCS."""
    bucket = get_storage_bucket()
    allowed_origins = origins or [
        "https://mlai.au",
        "https://www.mlai.au",
        "http://localhost:3000",
        "http://localhost:5173",
    ]
    bucket.cors = [
        {
            "origin": allowed_origins,
            "method": ["PUT", "OPTIONS"],
            "responseHeader": ["Content-Type", "x-goog-resumable"],
            "maxAgeSeconds": 3600,
        },
        {
            "origin": allowed_origins,
            "method": ["GET", "HEAD", "OPTIONS"],
            "responseHeader": ["Content-Type"],
            "maxAgeSeconds": 3600,
        },
    ]
    bucket.patch()
    return bucket.cors
