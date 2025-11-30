import os
import django
import sys

sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.models import Submission
from django.contrib.auth import get_user_model

User = get_user_model()

def verify_fix():
    try:
        # Get a user
        user = User.objects.first()
        if not user:
            print("No user found to test with.")
            return

        print(f"Testing submission with user: {user.email}")
        
        # Create a dummy submission
        # We don't need a real file or team for this DB constraint test
        sub = Submission(
            user=user,
            score=0.0,
            coarse_score=0.0,
            fine_score=0.0,
            # logs field should default to ''
        )
        sub.save()
        print(f"Successfully created submission: {sub.id}")
        
        # Clean up
        sub.delete()
        print("Cleaned up test submission.")
        
    except Exception as e:
        print(f"Failed to create submission: {e}")
        # Re-raise to ensure non-zero exit code on failure
        raise e

if __name__ == '__main__':
    verify_fix()
