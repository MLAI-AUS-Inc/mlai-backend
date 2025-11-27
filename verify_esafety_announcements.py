import os
import django
import sys

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.models import Announcement
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()

def verify():
    print("Verifying Esafety Announcements Feature...")

    # 1. Ensure User exists
    user, created = User.objects.get_or_create(
        email='admin@example.com',
        defaults={'full_name': 'Admin User', 'role': 'admin'}
    )
    if created:
        print("Created admin user.")
    else:
        print("Found admin user.")

    # 2. Create Announcement in Esafety app
    announcement = Announcement.objects.create(
        title='Esafety Specific Announcement',
        body='<p>This is for esafety only.</p>',
        author=user
    )
    print(f"Created esafety announcement: {announcement.title}")

    # 3. Verify via API
    client = APIClient(HTTP_HOST='localhost')
    client.force_authenticate(user=user)
    
    # Note: The URL is now handled by esafety.urls included at /api/v1/hackathons/esafety/
    # So the path is /api/v1/hackathons/esafety/announcements/
    response = client.get('/api/v1/hackathons/esafety/announcements/')
    
    if response.status_code == 200:
        print("API Response 200 OK")
        data = response.json()
        print(f"Announcements found: {len(data)}")
        if len(data) > 0:
            # Check if we got the esafety specific one
            titles = [a['title'] for a in data]
            if 'Esafety Specific Announcement' in titles:
                print("VERIFICATION SUCCESSFUL: Found esafety announcement")
            else:
                print(f"VERIFICATION FAILED: Esafety announcement not found. Got: {titles}")
        else:
            print("VERIFICATION FAILED: No announcements returned")
    else:
        print(f"VERIFICATION FAILED: API Status {response.status_code}")
        print(response.content)

if __name__ == '__main__':
    verify()
