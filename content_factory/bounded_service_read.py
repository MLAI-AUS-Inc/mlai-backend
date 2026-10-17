"""One bounded, cancellable metadata read for private service coordination."""
import asyncio
from urllib.parse import urlsplit

import httpx


class ServiceUnavailable(RuntimeError):
    """A timeout or transport failure, without private URL/header/body details."""


class ServiceBodyInvalid(ValueError):
    """A bounded protocol refusal without forwarding untrusted provider text."""


async def _read(method, url, *, headers, params, body, maximum, deadline):
    try:
        async with asyncio.timeout(deadline):
            async with httpx.AsyncClient(timeout=httpx.Timeout(deadline, connect=min(3.0, deadline)),
                    follow_redirects=False, trust_env=False) as client:
                headers = httpx.Headers(headers)
                headers["Accept-Encoding"] = "identity"
                async with client.stream(method, url, headers=headers, params=params, json=body) as response:
                    if response.status_code != 200:
                        # Error bodies carry no authority and need not be read.
                        # Retain only bounded numeric rate-limit hints. Cookies,
                        # locations and arbitrary provider headers stay private.
                        safe = {key: value for key in ("retry-after", "x-ratelimit-remaining")
                            if (value := response.headers.get(key)) is not None and 0 < len(value) <= 12
                            and value.isascii() and value.isdecimal()}
                        return httpx.Response(response.status_code, headers=safe, content=b"")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type != "application/json" or response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ServiceBodyInvalid("The service metadata encoding is invalid")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        raw.extend(chunk)
                        if len(raw) > maximum:
                            raise ServiceBodyInvalid("The service metadata exceeded its byte budget")
                    return httpx.Response(200, headers={"content-type": "application/json"}, content=bytes(raw))
    except (TimeoutError, httpx.HTTPError):
        raise ServiceUnavailable("The service metadata read is temporarily unavailable") from None


def read_limited(method, url, *, headers, maximum, deadline, params=None, body=None):
    """Read finite JSON bytes with a wall-clock deadline, no retries or redirects.

    Call from synchronous worker code (or a thread pool), never an event loop.
    A completed response is detached from its closed network connection. Callers
    still validate the exact private reservation and JSON schema. POST is used
    only for metadata inspection; this helper grants no mutation authority.
    """
    try:
        target = urlsplit(url)
        _ = target.port
        if (method not in ("GET", "POST") or not isinstance(url, str) or len(url) > 4096
                or target.scheme not in ("http", "https") or not target.netloc or not target.hostname or target.username or target.password or target.fragment
                or type(maximum) is not int or not 1 <= maximum <= 1024 * 1024
                or type(deadline) not in (int, float) or not 0 < deadline <= 120):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceBodyInvalid("The service read bounds are invalid") from None
    return asyncio.run(_read(method, url, headers=headers, params=params, body=body, maximum=maximum, deadline=deadline))
