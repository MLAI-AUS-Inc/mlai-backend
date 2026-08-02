from django.conf import settings


ACCESS_COOKIE = "mlai_chat_access"
REFRESH_COOKIE = "mlai_chat_refresh"
PRODUCTION_COOKIE_DOMAIN = ".mlai.au"


def _cookie_kwargs():
    production = not settings.DEBUG
    return {
        "httponly": True,
        "path": "/",
        "domain": PRODUCTION_COOKIE_DOMAIN if production else None,
        "secure": production,
        "samesite": "None" if production else "Lax",
    }


def set_account_session_cookies(response, credentials):
    kwargs = _cookie_kwargs()
    response.set_cookie(
        ACCESS_COOKIE,
        credentials.access_token,
        max_age=settings.COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS,
        **kwargs,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        credentials.refresh_token,
        max_age=settings.COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS * 24 * 60 * 60,
        **kwargs,
    )
    return response


def clear_account_session_cookies(response):
    kwargs = _cookie_kwargs()
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(
            name,
            path=kwargs["path"],
            domain=kwargs["domain"],
            samesite=kwargs["samesite"],
        )
    return response
