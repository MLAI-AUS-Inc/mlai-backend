from django.db import models
from django.conf import settings
from cryptography.fernet import Fernet
import base64
import hashlib

def get_fernet():
    # Derive a 32-byte url-safe base64-encoded key from the Django SECRET_KEY
    hasher = hashlib.sha256(settings.SECRET_KEY.encode())
    digest = hasher.digest() # 32 bytes
    b64_key = base64.urlsafe_b64encode(digest)
    return Fernet(b64_key)

class EncryptedTextField(models.TextField):
    description = "A TextField that is encrypted at rest"

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        try:
            f = get_fernet()
            return f.decrypt(value.encode()).decode()
        except Exception:
            # If decryption fails, return as is (might be plaintext or corrupted)
            return value

    def get_prep_value(self, value):
        if value is None:
            return value
        # Ensure we don't double encrypt if somehow called twice (though get_prep_value is for DB prep)
        # But we need to handle if the value is already encrypted? 
        # Usually get_prep_value assumes incoming value is python object.
        f = get_fernet()
        return f.encrypt(str(value).encode()).decode()
