import os
import django
import sys

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from core.email_utils import verify_magic_link
from django.contrib.auth import get_user_model

User = get_user_model()

def check_token():
    token = "eyJlbWFpbCI6ImhpQG1sYWkuYXUifQ:1vOQCf:_tCCa67kF0tKCcLOETSmonitMXLCDtlhuJb3RFe5X7o"
    print(f"Verifying token: {token}")
    
    email = verify_magic_link(token)
    if email:
        print(f"Token is VALID for email: {email}")
        try:
            user = User.objects.get(email=email)
            print(f"User found: {user.email}, Active: {user.is_active}")
        except User.DoesNotExist:
            print("User NOT found in database.")
    else:
        print("Token is INVALID or EXPIRED.")

if __name__ == '__main__':
    check_token()
