import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from core.models import Organization

print(f"Checking Organizations...")
orgs = Organization.objects.all()
if not orgs.exists():
    print("No organizations found.")
else:
    for org in orgs:
        print(f"ID: {org.id}, Name: {org.name}, Domain: '{org.domain}'")
