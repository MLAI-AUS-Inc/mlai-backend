from django.conf import settings

ACCESS_COOKIE = 'mlai_chat_access'
REFRESH_COOKIE = 'mlai_chat_refresh'
PRODUCTION_COOKIE_DOMAIN = '.mlai.au'


def access_cookie_name():
    if settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES:
        return settings.COMMUNITY_CHAT_ACCESS_COOKIE_NAME
    return ACCESS_COOKIE


def refresh_cookie_name():
    if settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES:
        return settings.COMMUNITY_CHAT_REFRESH_COOKIE_NAME
    return REFRESH_COOKIE


def _cookie_kwargs():
    isolated = settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES
    production = not settings.DEBUG
    return {
        'httponly': True,
        'path': '/',
        'domain': None if isolated else PRODUCTION_COOKIE_DOMAIN if production else None,
        'secure': isolated or production,
        'samesite': 'None' if isolated or production else 'Lax',
    }


def _clear_legacy_cookies(response):
    if settings.COMMUNITY_CHAT_HOST_ONLY_COOKIES and settings.COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES:
        for name in (ACCESS_COOKIE, REFRESH_COOKIE):
            response.delete_cookie(name, path='/', domain=PRODUCTION_COOKIE_DOMAIN, samesite='None')
    return response


def set_account_session_cookies(response, credentials):
    kwargs = _cookie_kwargs()
    response.set_cookie(access_cookie_name(), credentials.access_token,
                        max_age=settings.COMMUNITY_CHAT_SESSION_ACCESS_TTL_SECONDS, **kwargs)
    response.set_cookie(refresh_cookie_name(), credentials.refresh_token,
                        max_age=settings.COMMUNITY_CHAT_SESSION_REFRESH_TTL_DAYS * 86400, **kwargs)
    return _clear_legacy_cookies(response)


def clear_account_session_cookies(response):
    kwargs = _cookie_kwargs()
    for name in (access_cookie_name(), refresh_cookie_name()):
        response.delete_cookie(name, path=kwargs['path'], domain=kwargs['domain'], samesite=kwargs['samesite'])
    return _clear_legacy_cookies(response)
