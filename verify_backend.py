import os
import django
import sys
# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from esafety.models import Team

User = get_user_model()

def verify_backend():
    print("Starting verification...")
    
    # Create test user
    email = "test_esafety@example.com"
    user, created = User.objects.get_or_create(email=email, defaults={'full_name': 'Test User'})
    if created:
        user.set_password('password')
        user.save()
        print(f"Created user: {email}")
    else:
        print(f"User exists: {email}")

    client = APIClient()
    client.force_authenticate(user=user)

    # 1. Create a team
    team_name = "Test Team A"
    team, _ = Team.objects.get_or_create(team_name=team_name)
    print(f"Created/Found team: {team}")

    # 2. List teams
    response = client.get('/api/v1/hackathons/esafety/teams/')
    if response.status_code == 200:
        print("GET /teams/ SUCCESS")
        print(response.json())
    else:
        print(f"GET /teams/ FAILED: {response.status_code}")
        print(response.content)

    # 3. Join team
    response = client.post('/api/v1/hackathons/esafety/teams/join/', {'team_id': team.team_id}, format='json')
    if response.status_code == 200:
        print("POST /teams/join/ SUCCESS")
        print(response.json())
    else:
        print(f"POST /teams/join/ FAILED: {response.status_code}")
        print(response.content)

    # 4. Submit
    response = client.post('/api/v1/hackathons/esafety/submissions/', {'file_url': 'http://example.com/file.csv'}, format='json')
    if response.status_code == 201:
        print("POST /submissions/ SUCCESS")
        print(response.json())
    else:
        print(f"POST /submissions/ FAILED: {response.status_code}")
        print(response.content)

    print("Verification complete.")

if __name__ == "__main__":
    verify_backend()
