import os
import django
import sys

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from django.contrib.auth import get_user_model
from esafety.serializers import AuthorSerializer
from rest_framework.test import APIClient

User = get_user_model()

def verify():
    print("Verifying User Updates...")

    # 1. Check User count
    count = User.objects.count()
    print(f"Total users: {count}")
    
    # 2. Check sam@mlai.au
    try:
        sam = User.objects.get(email='sam@mlai.au')
        print(f"Found sam@mlai.au")
        print(f"Avatar URL: {sam.avatar_url}")
        
        expected_url = 'https://firebasestorage.googleapis.com/v0/b/mlai-main-website.firebasestorage.app/o/Screenshot%202025-11-19%20at%205.26.31%E2%80%AFpm.jpg?alt=media&token=bcab59dd-63bc-4198-8066-d12b4cf613fe'
        if sam.avatar_url == expected_url:
            print("Avatar URL matches expected.")
        else:
            print("Avatar URL does NOT match.")

        # 3. Check Serializer
        serializer = AuthorSerializer(sam)
        print(f"Serialized imageUrl: {serializer.data['imageUrl']}")
        
        if serializer.data['imageUrl'] == expected_url:
            print("Serializer returns correct imageUrl.")
            print("VERIFICATION SUCCESSFUL")
        else:
            print("Serializer returns INCORRECT imageUrl.")

        # 4. Verify /api/v1/auth/me/
        print("Verifying /api/v1/auth/me/ endpoint...")
        client = APIClient(HTTP_HOST='localhost')
        client.force_authenticate(user=sam)
        response = client.get('/api/v1/auth/me/')
        
        if response.status_code == 200:
            data = response.json()
            print(f"API Response avatar_url: {data.get('avatar_url')}")
            if data.get('avatar_url') == expected_url:
                print("API returns correct avatar_url.")
            else:
                print("API returns INCORRECT avatar_url.")
        else:
            print(f"API Request Failed: {response.status_code}")
            
    except User.DoesNotExist:
        print("VERIFICATION FAILED: sam@mlai.au not found.")

if __name__ == '__main__':
    verify()
