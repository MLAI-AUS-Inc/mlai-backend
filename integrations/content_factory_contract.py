CONTENT_FACTORY_REQUEST_SOURCE = "roo_slackbot"
INVALID_REQUEST_SOURCE_ERROR = "request_source must be roo_slackbot"


def require_roo_request_source(request_source, *, error_message: str = INVALID_REQUEST_SOURCE_ERROR) -> str:
    normalized = str(request_source or "").strip()
    if normalized != CONTENT_FACTORY_REQUEST_SOURCE:
        raise ValueError(error_message)
    return normalized
