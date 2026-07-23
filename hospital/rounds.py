from .models import Announcement, HospitalCompetitionRound, Submission, Team


def active_hospital_team_for(user):
    """Return the user's team for the currently active HealthHack round."""
    manager = getattr(user, 'hospital_teams', None)
    if manager is None:
        return None
    return manager.filter(
        round__status=HospitalCompetitionRound.STATUS_ACTIVE,
    ).first()


def active_hospital_teams():
    return Team.objects.filter(
        round__status=HospitalCompetitionRound.STATUS_ACTIVE,
    )


def active_hospital_submissions():
    return Submission.objects.filter(
        round__status=HospitalCompetitionRound.STATUS_ACTIVE,
    )


def active_hospital_announcements():
    return Announcement.objects.filter(
        round__status=HospitalCompetitionRound.STATUS_ACTIVE,
    )
