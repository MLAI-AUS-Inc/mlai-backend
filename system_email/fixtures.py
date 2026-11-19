"""Sample context for previews, tests and ``send_system_email_test``. No real data."""

_STARTUPS = [{
    "name": "Example Co",
    "domain": "example.com",
    "expires_at_display": "31 July 2026 at 12:00 PM AEST",
    "update_url": "https://mlai.au/platform/login?next=/updates/new",
}]

SAMPLE_CONTEXT = {
    "chat_sign_in_code": {
        "verification_code": "123456", "expires_minutes": 10, "first_name": "Sam",
        "product_name": "MLAI Chat", "support_url": "https://mlai.au/support",
    },
    "magic_link": {
        "magic_link": "https://chat.mlai.au/verify-email?token=sample", "app_name": "MLAI Chat",
        "first_name": "Sam",
    },
    "password_reset": {
        "reset_link": "https://mlai.au/reset-password?token=sample", "first_name": "Sam", "expires_minutes": 60,
    },
    "monthly_update_7d": {"first_name": "Sam", "startups": _STARTUPS},
    "monthly_update_1d": {"first_name": "Sam", "startups": _STARTUPS},
    "event_perk_code": {
        "first_name": "Sam", "event_name": "Example Meetup", "perk_label": "Half-price ticket",
        "coupon_code": "ROO-ABCDEFGHJKLMNPQR", "checkout_url": "https://luma.com/example?coupon=ROO-ABCDEFGHJKLMNPQR",
        "points_cost": 20, "expires_display": "Thu 19 Nov 2026, 5:30 PM AEDT", "requires_approval": False,
    },
    "startup_team_invite": {
        "startup_name": "Acme Trees", "startup_logo": "", "inviter_name": "Alex",
        "invite_email": "jo@example.com", "expires_on": "25 November 2026, 5:30 PM AEDT",
        "accept_url": "https://chat.mlai.au/my-startup/join?invitation=00000000-0000-4000-8000-000000000000",
        "asset_base": "https://api.mlai.au/static/crm/email", "shared_access": True,
    },
    "mentor_invite": {
        "first_name": "Sam", "inviter_name": "Sam Donegan", "inviter_first_name": "Sam",
        "note": "You helped three founders at the last demo night. We'd love more people to get that.",
        "accept_url": "https://chat.mlai.au/mentoring/apply?mentor_invite=sample",
        "explainer_url": "https://mlai.au/mentors", "expires_on": "30 November 2026",
        "invited_email": "sam@example.com", "asset_base": "https://api.mlai.au/static/crm/email",
    },
    "mentor_session_feedback": {
        "founder_first": "Alex", "mentor_name": "Priya Raman", "mentor_first": "Priya",
        "date_long": "Thursday 19 November", "date_short": "Thu 19 Nov", "time_range": "2:00 to 2:30 pm",
        "time_range_detail": "2:00 to 2:30 pm, Melbourne time", "place": "Google Meet", "is_video": True,
        "brief_line": "Pricing your first paid plan",
        "url_sad": "https://api.mlai.au/api/v1/mentoring/feedback/sample/?r=sad&src=email",
        "url_neutral": "https://api.mlai.au/api/v1/mentoring/feedback/sample/?r=neutral&src=email",
        "url_happy": "https://api.mlai.au/api/v1/mentoring/feedback/sample/?r=happy&src=email",
        "url_no_show": "https://api.mlai.au/api/v1/mentoring/feedback/sample/?r=no_show&src=email",
        "unsubscribe_url": "https://api.mlai.au/api/v1/mentoring/feedback/sample/unsubscribe/",
        "asset_base": "https://api.mlai.au/static/crm/email",
    },
}
