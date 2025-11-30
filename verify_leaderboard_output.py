import os
import django
import json

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mlai.settings')
django.setup()

from esafety.models import Submission

def check_leaderboard():
    submissions = Submission.objects.all().order_by('-score')
    print(f"Total submissions found: {submissions.count()}")
    
    data = []
    for sub in submissions:
        team_data = None
        if sub.team:
            team_data = {
                "team_id": sub.team.team_id,
                "team_name": sub.team.team_name,
                "team_avatar": sub.team.avatar_url,
                "members": [
                    {
                        "full_name": member.full_name,
                        "avatar_url": member.avatar_url
                    } for member in sub.team.members.all()
                ]
            }
        
        data.append({
            "id": sub.id,
            "score": sub.score,
            "accuracy": sub.fine_score, 
            "submitted_at": str(sub.submitted_at),
            "team": team_data,
            "participant_name": sub.participant_name,
            "user_email": sub.user.email,
            "file_url": sub.file_url,
            "user_avatar": sub.user.avatar_url,
            "user_name": sub.user.full_name
        })
    
    print(json.dumps(data, indent=2))

if __name__ == "__main__":
    check_leaderboard()
