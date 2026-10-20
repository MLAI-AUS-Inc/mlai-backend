"""Copy founder-supplied author photos into MLAI storage.

Founders usually paste a LinkedIn photo link (``media.licdn.com``). Those links
are signed and can expire, and chat.mlai.au's CSP only renders images from MLAI
hosts, so a byline that points at them breaks in the editor and later on the
published article. Saving an author profile therefore fetches each new photo
link once over a DNS-pinned HTTPS connection, re-encodes it, and stores the
derivative in Firebase Storage. The same fetch backs an editor preview that is
returned as a ``data:`` URL and never stored.
"""

import base64
import http.client
import logging
import socket
import ssl
import threading
import time
import warnings
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from agent_runtime.webhook_transport import CallbackEndpointError, resolve_callback

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_PIXELS = 40_000_000
MAX_PHOTO_DIMENSION = 16_384
STORED_PHOTO_SIZE = 800
PREVIEW_PHOTO_SIZE = 256
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 10
STORAGE_PREFIX = "author-photos"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
UNAVAILABLE_STATUSES = {401, 403, 404, 410}

NOT_AN_IMAGE = "That link doesn't open an image. Paste a direct link to a JPG, PNG or WebP photo."
UNREACHABLE = "That photo link couldn't be reached. Check it opens in your browser, then try again."


class AuthorPhotoError(ValueError):
    """A reason a photo link could not be copied, worded for the founder."""

    def __init__(self, code: str, message: str, *, status: int = 400, author_id: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.author_id = author_id


def _storage_url_prefix() -> str:
    from core.firebase_utils import FIREBASE_STORAGE_BUCKET

    return f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_STORAGE_BUCKET}/o/"


def needs_copy(url: Any) -> bool:
    """True for an external http(s) photo link; MLAI-stored and site-relative photos are kept."""
    value = str(url or "").strip()
    if not value.lower().startswith(("http://", "https://")):
        return False
    return not value.startswith(_storage_url_prefix())


def _https_url(url: str) -> str:
    """Reject page links early and upgrade cleartext links; the fetch never sends HTTP."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise AuthorPhotoError("author_photo_invalid", "Use a full https:// link to the photo.") from None
    host = (parsed.hostname or "").lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        raise AuthorPhotoError(
            "author_photo_not_image",
            "That's a LinkedIn page, not the photo itself. On LinkedIn, right-click the profile photo, "
            "choose Copy image address, and paste that link.",
        )
    if parsed.scheme == "http" and port in {None, 80} and host:
        netloc = f"[{host}]" if ":" in host else host
        return parsed._replace(scheme="https", netloc=netloc).geturl()
    return url


def _get(hostname: str, address: str, target: str, deadline: float) -> Tuple[int, Optional[str], bytes]:
    """One GET to the validated address, bounded by the overall deadline and photo size."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AuthorPhotoError("author_photo_unreachable", UNREACHABLE)
    # TLS SNI and certificate checks use the original host; only the TCP
    # destination is replaced by the address resolve_callback validated.
    connection = http.client.HTTPSConnection(hostname, 443, timeout=remaining, context=ssl.create_default_context())

    def pinned_connection(_destination, socket_timeout, source_address=None):
        available = max(0.001, deadline - time.monotonic())
        return socket.create_connection((address, 443), min(socket_timeout, available), source_address)

    connection._create_connection = pinned_connection

    def expire():
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    # A peer that trickles bytes must not reset the per-read timeout forever.
    timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", target, headers={
            "Accept": "image/jpeg,image/png,image/webp;q=0.9,image/*;q=0.5",
            "User-Agent": "MLAI-AuthorPhoto/1.0 (+https://mlai.au)",
        })
        response = connection.getresponse()
        if response.status != 200:
            return response.status, response.getheader("Location"), b""
        declared = response.getheader("Content-Length") or ""
        if declared.isdigit() and int(declared) > MAX_PHOTO_BYTES:
            raise AuthorPhotoError("author_photo_too_large", "That photo is larger than 10 MB. Use a smaller image.")
        body = response.read(MAX_PHOTO_BYTES + 1)
        if len(body) > MAX_PHOTO_BYTES:
            raise AuthorPhotoError("author_photo_too_large", "That photo is larger than 10 MB. Use a smaller image.")
        return 200, None, body
    except (OSError, http.client.HTTPException):
        raise AuthorPhotoError("author_photo_unreachable", UNREACHABLE) from None
    finally:
        timer.cancel()
        connection.close()


def fetch_photo(url: str) -> bytes:
    """Fetch a public HTTPS photo, re-validating and re-pinning every redirect hop."""
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    current = _https_url(str(url or "").strip())
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            hostname, address, target = resolve_callback(
                current, dns_timeout=min(3, max(0.001, deadline - time.monotonic())))
        except CallbackEndpointError as exc:
            if exc.reason == "private_destination":
                raise AuthorPhotoError("author_photo_private", "Use a public photo link.") from None
            if exc.reason == "invalid_url":
                raise AuthorPhotoError("author_photo_invalid", "Use a full https:// link to the photo.") from None
            raise AuthorPhotoError("author_photo_unreachable", UNREACHABLE) from None
        status, location, body = _get(hostname, address, target, deadline)
        if status == 200:
            return body
        if status in REDIRECT_STATUSES and location:
            current = _https_url(urljoin(current, location))
            continue
        if status in UNAVAILABLE_STATUSES:
            raise AuthorPhotoError(
                "author_photo_unavailable",
                "This photo link has expired or isn't public. Copy the image address again and paste the new link.",
            )
        raise AuthorPhotoError("author_photo_unreachable", UNREACHABLE)
    raise AuthorPhotoError("author_photo_unreachable", "That photo link redirected too many times.")


def encode_photo(data: bytes, size: int) -> Tuple[bytes, str]:
    """Validate a still raster photo and return an orientation-correct derivative at most ``size`` px."""
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        logger.exception("Pillow is unavailable for author photo processing")
        raise AuthorPhotoError("author_photo_service_unavailable",
                               "Photo copying is temporarily unavailable.", status=503) from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(data))
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise AuthorPhotoError("author_photo_not_image", NOT_AN_IMAGE)
            if image.width * image.height > MAX_PHOTO_PIXELS or max(image.size) > MAX_PHOTO_DIMENSION:
                raise AuthorPhotoError("author_photo_too_large", "That photo's dimensions are too large. Use a smaller image.")
            if getattr(image, "n_frames", 1) != 1:
                raise AuthorPhotoError("author_photo_not_image", "Use a still photo, not an animation.")
            image.load()
            image = ImageOps.exif_transpose(image)
            has_alpha = image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            result = BytesIO()
            if has_alpha:
                image.save(result, format="PNG", optimize=True)
                return result.getvalue(), "image/png"
            image.save(result, format="JPEG", quality=88, optimize=True, progressive=True)
            return result.getvalue(), "image/jpeg"
    except AuthorPhotoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise AuthorPhotoError("author_photo_not_image", NOT_AN_IMAGE) from None


def preview_photo(url: str) -> str:
    """A small ``data:`` URL the editor can show before saving; nothing is stored."""
    data, content_type = encode_photo(fetch_photo(url), PREVIEW_PHOTO_SIZE)
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def copy_photo(url: str, *, company_id: Any) -> str:
    """Store a re-encoded copy of the photo under the company's prefix and return its URL."""
    from core.firebase_utils import upload_file_to_storage

    data, content_type = encode_photo(fetch_photo(url), STORED_PHOTO_SIZE)
    extension = "png" if content_type == "image/png" else "jpg"
    destination = f"{STORAGE_PREFIX}/{company_id}/{uuid4().hex}.{extension}"
    try:
        stored = upload_file_to_storage(BytesIO(data), destination, content_type=content_type)
    except Exception:
        stored = None
    if not stored:
        logger.error("author_photo_storage_failed company_id=%s", company_id)
        raise AuthorPhotoError("author_photo_storage_failed", "The photo couldn't be saved. Please try again.", status=502)
    return stored


def store_author_photos(
    authors: Iterable[Dict[str, Any]],
    *,
    previous: Iterable[Dict[str, Any]],
    company_id: Any,
    skipped: Optional[List[AuthorPhotoError]] = None,
) -> List[Dict[str, Any]]:
    """Replace each external photo link with an MLAI-stored copy before the profiles are saved.

    A link that is new in this save must copy or the save is rejected, so a founder
    learns about a dead link straight away. A link that was already saved (before
    photos were copied) is retried but kept on failure, so one stale photo never
    blocks editing the other authors. Each kept link's error is appended to
    ``skipped`` when a list is passed.
    """
    earlier = {author.get("id"): author.get("avatarUrl", "") for author in previous}
    copies: Dict[str, str] = {}
    result = []
    for author in authors:
        url = author.get("avatarUrl", "")
        if not needs_copy(url):
            result.append(author)
            continue
        try:
            stored = copies.get(url) or copy_photo(url, company_id=company_id)
        except AuthorPhotoError as exc:
            if earlier.get(author.get("id")) == url:
                logger.info("author_photo_copy_skipped company_id=%s author_id=%s code=%s",
                            company_id, author.get("id"), exc.code)
                if skipped is not None:
                    exc.author_id = str(author.get("id") or "")
                    skipped.append(exc)
                result.append(author)
                continue
            raise AuthorPhotoError(
                exc.code,
                f"{author.get('name') or 'This author'}'s photo couldn't be copied. {exc}",
                status=exc.status,
                author_id=str(author.get("id") or ""),
            ) from None
        copies[url] = stored
        result.append({**author, "avatarUrl": stored})
    return result
