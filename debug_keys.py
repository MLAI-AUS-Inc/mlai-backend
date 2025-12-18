
import os
from django.conf import settings
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

print(f"INTERNAL_API_KEY in settings: '{settings.INTERNAL_API_KEY}'")
print(f"ROO_API_KEY in env: '{os.environ.get('ROO_API_KEY')}'")
