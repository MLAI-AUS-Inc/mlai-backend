import os
import django
from django.conf import settings
import sys

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

print("--- Database Configuration ---")
db = settings.DATABASES['default']
print(f"Engine: {db.get('ENGINE')}")
print(f"Name: {db.get('NAME')}")
print(f"User: {db.get('USER')}")
print(f"Host: {db.get('HOST')}")
print(f"Port: {db.get('PORT')}")
