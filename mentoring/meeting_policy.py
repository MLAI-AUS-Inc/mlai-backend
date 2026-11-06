"""Versioned capture promises frozen with the member's reviewed booking terms."""
REQUIRED_POLICY = {'version': 1, 'transcription': 'required', 'smart_notes': 'required'}


def validate_policy(value):
    """Only supported complete policies may be persisted by profile settings."""
    from .service import MentoringError
    if not isinstance(value, dict) or (value != {} and (value != REQUIRED_POLICY or type(value.get('version')) is not int)):
        raise MentoringError('invalid_field', 'Choose automatic transcription and Gemini notes, or disable both.', 400)
    return dict(value)


def required(session):
    """Historical sessions with no captured policy retain their original contract."""
    policy = getattr(session, 'booking_snapshot', {}).get('meeting_artifact_policy', {})
    if policy not in ({}, REQUIRED_POLICY):
        from .calendars.errors import CalendarError
        raise CalendarError('invalid_meeting_policy')
    return session.location_type == 'video' and policy == REQUIRED_POLICY
