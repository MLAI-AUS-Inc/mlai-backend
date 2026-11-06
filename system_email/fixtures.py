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
}
