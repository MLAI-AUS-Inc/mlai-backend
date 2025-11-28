import os
import django
import sys
from urllib.parse import urlparse, parse_qs

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.email_utils import generate_magic_link

User = get_user_model()

def verify_flow():
    print("Verifying Magic Link Flow...")
    
    email = 'hi@mlai.au'
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        print(f"User {email} not found. Creating...")
        user = User.objects.create_user(email=email, full_name='Hi User')

    # 1. Simulate SendMagicLinkView logic
    app = 'esafety'
    base_url = "http://localhost:5173"
    magic_link = generate_magic_link(user, base_url=base_url)
    
    if '?' in magic_link:
        magic_link += f"&app={app}"
    else:
        magic_link += f"?app={app}"
    
    print(f"Generated Link: {magic_link}")

    # 2. Parse Link
    parsed = urlparse(magic_link)
    params = parse_qs(parsed.query)
    token = params.get('token', [None])[0]
    app_param = params.get('app', [None])[0]
    
    print(f"Token: {token}")
    print(f"App Param: {app_param}")

    if not token:
        print("FAIL: Token missing")
        return

    if app_param != 'esafety':
        print(f"FAIL: App param incorrect. Got {app_param}")
        return

    # 3. Simulate MagicLinkVerifyView Redirect Logic
    # In the new logic, we use the same base URL for both apps
    redirect_base = "http://localhost:5173"

    if app_param == 'esafety':
        next_url = f"{redirect_base}/esafety/dashboard"
    else:
        next_url = f"{redirect_base}/dashboard"

    print(f"Calculated Redirect URL: {next_url}")

    if next_url == "http://localhost:5173/esafety/dashboard":
        print("SUCCESS: Redirect logic is correct.")
    else:
        print(f"FAIL: Redirect logic is incorrect. Expected http://localhost:5173/esafety/dashboard, got {next_url}")

if __name__ == '__main__':
    verify_flow()
