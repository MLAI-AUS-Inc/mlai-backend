from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from .models import Team, Submission, Announcement
from .serializers import TeamSerializer, SubmissionSerializer, AnnouncementSerializer

class TeamListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        teams = Team.objects.all().order_by('team_id')
        serializer = TeamSerializer(teams, many=True)
        return Response(serializer.data)

class TeamNamesListView(APIView):
    permission_classes = [permissions.AllowAny] # Or IsAuthenticated? Prompt says "List available teams". Usually public or auth. Let's assume IsAuthenticated to be safe, or AllowAny if it's for a dropdown on a public page?
    # The prompt says "GET /api/v1/teams/".
    # Let's stick to IsAuthenticated as it's likely for a logged in user updating profile.
    # But wait, "List available teams for the dropdown."
    # If I am registering, I might need it? But profile update implies I am logged in.
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        team_names = Team.objects.values_list('team_name', flat=True)
        return Response({"team_names": list(team_names)})

class JoinTeamView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        team_id = request.data.get('team_id')
        if not team_id:
            return Response({"error": "team_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        team = get_object_or_404(Team, team_id=team_id)
        user = request.user

        # Check if user is already in a team for this hackathon
        if user.esafety_teams.exists():
             # Optional: allow switching teams or enforce one team policy
             # For now, let's assume they can switch or we just add them.
             # If strict one-team policy:
             # return Response({"error": "You are already in a team"}, status=status.HTTP_400_BAD_REQUEST)
             pass

        team.members.add(user)
        return Response({"message": f"Joined team {team.team_name}"}, status=status.HTTP_200_OK)

class SubmissionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Handle submission logic here. 
        # For simplicity, we'll just record a dummy submission or file URL.
        file_url = request.data.get('file_url')
        
        user = request.user
        team = user.esafety_teams.first()
        
        submission = Submission.objects.create(
            user=user,
            team=team,
            file_url=file_url
        )
        
        serializer = SubmissionSerializer(submission)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

class LeaderboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Return leaderboard data based on submissions
        # This is a placeholder implementation
        submissions = Submission.objects.all().order_by('-score')
        data = []
        for sub in submissions:
            data.append({
                "team": sub.team.team_name if sub.team else "Individual",
                "user": sub.user.full_name,
                "score": sub.score
            })
        return Response(data)

from rest_framework import generics

class AnnouncementListView(generics.ListAPIView):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementSerializer
    permission_classes = [permissions.IsAuthenticated]
