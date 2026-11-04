"""Common credential redaction for artifacts, web logs and Celery log handlers."""
from __future__ import annotations

import re

_QUERY = re.compile(r'(?i)([?&](?:token|access_token|refresh_token|id_token|api[_-]?key|key|signature|x-amz-signature|x-amz-credential|x-amz-security-token|sig|sas)=)[^&\s\"\'<>]+')
_BASIC = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*://[^:/\s]*:)[^@/\s]+@')
# Preserve the existing rotation event identifier; credential assignments are
# redacted separately even if an opaque value happens to equal this identifier.
_TOKEN = re.compile(r'\b(?:gh[spou]_[A-Za-z0-9_]+|github_pat_(?!expiring_soon\b)[A-Za-z0-9_]+)\b')
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')
_AUTH = re.compile(r'(?i)(authorization[\"\']?\s*[:=]\s*[\"\']?)(?:(?:bearer|token|basic)\s+)?[^\s,\"\'<>]+')
_FIELD = re.compile(r'''(?i)(\b(?:github[_-]?token|token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|authorization)["']?\s*[:=]\s*)(?:(["'])(.*?)\2|([^\s,;"'<>]+))''')
_URL_QUERY = re.compile(r'(https?://[^?\s\"\'<>]+)\?[^\s\"\'<>]+')


def redact_tokenized_url(value: str) -> str:
    """Redact credential query values while preserving useful route context."""
    return _QUERY.sub(r'\1[redacted]', str(value or ''))


def redact_secret_text(value: str) -> str:
    """Remove URL, header, GitHub and JWT credentials from arbitrary text."""
    result = redact_tokenized_url(value)
    result = _BASIC.sub(r'\1***@', result)
    result = _AUTH.sub(r'\1***', result)
    result = _FIELD.sub(lambda match: match[1] + (match[2] or '') + '***' + (match[2] or ''), result)
    return _JWT.sub('[redacted]', _TOKEN.sub('[redacted]', result))
