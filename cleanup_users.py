import os
import django
import sys

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

def cleanup():
    print("Starting user cleanup...")

    # 1. Delete all users except sam@mlai.au and admin@example.com (just in case)
    users_to_delete = User.objects.exclude(email__in=['sam@mlai.au', 'admin@example.com'])
    count = users_to_delete.count()
    users_to_delete.delete()
    print(f"Deleted {count} test users.")

    # 2. Update sam@mlai.au
    try:
        sam = User.objects.get(email='sam@mlai.au')
        sam.avatar_url = 'https://firebasestorage.googleapis.com/v0/b/mlai-main-website.firebasestorage.app/o/Screenshot%202025-11-19%20at%205.26.31%E2%80%AFpm.jpg?alt=media&token=bcab59dd-63bc-4198-8066-d12b4cf613fe'
        sam.is_superuser = True
        sam.is_staff = True
        sam.save()
        print("Updated sam@mlai.au with avatar and superuser status.")
    except User.DoesNotExist:
        print("User sam@mlai.au not found. Creating it...")
        sam = User.objects.create_superuser(
            email='sam@mlai.au',
            password='password123', # Temporary password, user should change or use magic link
            role='admin'
        )
        sam.avatar_url = 'https://firebasestorage.googleapis.com/v0/b/mlai-main-website.firebasestorage.app/o/Screenshot%202025-11-19%20at%205.26.31%E2%80%AFpm.jpg?alt=media&token=bcab59dd-63bc-4198-8066-d12b4cf613fe'
        sam.save()
        print("Created sam@mlai.au.")

    print("Cleanup complete.")

if __name__ == '__main__':
    cleanup()
