import os
import django
import sys

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from core.models import Hackathon, User
from hackathons.models import Announcement
from django.utils import timezone

def verify():
    print("Verifying Announcements Feature...")

    # 1. Ensure Hackathon exists
    hackathon, created = Hackathon.objects.get_or_create(
        slug='esafety',
        defaults={
            'name': 'eSafety Hackathon',
            'description': 'Test Hackathon',
            'start_date': timezone.now().date(),
            'end_date': timezone.now().date(),
        }
    )
    if created:
        print("Created 'esafety' hackathon.")
    else:
        print("Found 'esafety' hackathon.")

    # 2. Ensure User exists
    user, created = User.objects.get_or_create(
        email='admin@example.com',
        defaults={'full_name': 'Admin User', 'role': 'admin'}
    )
    if created:
        print("Created admin user.")
    else:
        print("Found admin user.")

    # 3. Create Announcement
    announcement = Announcement.objects.create(
        title='Test Announcement',
        body='<p>This is a test announcement.</p>',
        hackathon=hackathon,
        author=user
    )
    print(f"Created announcement: {announcement.title}")

    # 4. Verify via API (using Django test client for simplicity inside the script)
    from rest_framework.test import APIClient
    client = APIClient(HTTP_HOST='localhost')
    client.force_authenticate(user=user)
    response = client.get('/api/v1/hackathons/esafety/announcements/')
    
    if response.status_code == 200:
        print("API Response 200 OK")
        data = response.json()
        print(f"Announcements found: {len(data)}")
        if len(data) > 0:
            first = data[0]
            print(f"First announcement title: {first['title']}")
            print(f"Author name: {first['author']['name']}")
            print(f"Author imageUrl: {first['author']['imageUrl']}")
            
            if first['title'] == 'Test Announcement' and first['author']['name'] == 'Admin User':
                print("VERIFICATION SUCCESSFUL")
            else:
                print("VERIFICATION FAILED: Data mismatch")
        else:
            print("VERIFICATION FAILED: No announcements returned")
    else:
        print(f"VERIFICATION FAILED: API Status {response.status_code}")
        print(response.content)

if __name__ == '__main__':
    verify()
