"""Typed bounded CMS image uploads; callers own consent and durable recovery."""

from dataclasses import dataclass, field
import hashlib
import html
from io import BytesIO
import re
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError

from .cms_api_transport import CmsApiError, _send_json_response
from .cms_connection_contract import validate_binding, validate_private_credential, CmsContractError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 16 * 1024 * 1024
FORMATS = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp")}


@dataclass(frozen=True)
class CmsImage:
    """Verified original raster bytes; this value is not upload authorization."""

    raw: bytes = field(repr=False)
    sha256: str
    media_type: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class UploadedImage:
    """Provider acknowledgement only; public bytes still require verification."""

    platform: str
    provider_id: int | None
    upload_id: str
    source_sha256: str
    url: str = field(repr=False)
    public_bytes_verified: bool = False


def prepare_image(raw):
    """Decode a bounded static raster; active formats and animation are refused."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_IMAGE_BYTES:
        raise CmsApiError("invalid_image")
    try:
        with Image.open(BytesIO(raw)) as image:
            if (image.format not in FORMATS or not 0 < image.width <= MAX_DIMENSION or
                    not 0 < image.height <= MAX_DIMENSION or image.width * image.height > MAX_PIXELS or
                    getattr(image, "n_frames", 1) != 1):
                raise CmsApiError("invalid_image")
            media_type, extension = FORMATS[image.format]
            width, height = image.size
            image.verify()
        with Image.open(BytesIO(raw)) as decoded:
            decoded.load()
        return CmsImage(raw, hashlib.sha256(raw).hexdigest(), media_type, extension, width, height)
    except CmsApiError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise CmsApiError("invalid_image") from None


def _identity(upload_id, image):
    try:
        if not isinstance(upload_id, str) or str(UUID(upload_id)) != upload_id or not isinstance(image, CmsImage) or prepare_image(image.raw) != image:
            raise ValueError("Invalid media identity")
    except (ValueError, TypeError, AttributeError):
        raise CmsApiError("invalid_request") from None
    return "mlai-" + UUID(upload_id).hex + "-" + image.sha256[:16]


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise CmsApiError("invalid_request")
    return value


def _marker(upload_id, image):
    return "mlai-image:" + upload_id + ":" + image.sha256


def _multipart(fields, *, image, filename):
    boundary = "mlai-image-" + uuid4().hex
    delimiter = boundary.encode()
    if delimiter in image.raw:
        raise CmsApiError("invalid_request")
    parts = []
    for name, value in fields.items():
        parts.append(b"--" + delimiter + b'\r\nContent-Disposition: form-data; name="' + name.encode("ascii") + b'"\r\n\r\n' + value.encode() + b"\r\n")
    parts.append(b"--" + delimiter + b'\r\nContent-Disposition: form-data; name="file"; filename="' + filename.encode("ascii") +
        b'"\r\nContent-Type: ' + image.media_type.encode("ascii") + b"\r\n\r\n" + image.raw + b"\r\n")
    parts.append(b"--" + delimiter + b"--\r\n")
    body = b"".join(parts)
    if len(body) > MAX_IMAGE_BYTES + 16384:
        raise CmsApiError("invalid_request")
    return body, "multipart/form-data; boundary=" + boundary


def _media_url(value, binding):
    """Accept only this explicitly bound site's/admin's HTTPS storage path.

    Custom external storage needs separate owner origin confirmation; a provider
    JSON URL never grants permission to fetch arbitrary hosts or credentials.
    """
    if not isinstance(value, str) or not 0 < len(value) <= 2048 or any(ord(c) < 33 or 127 <= ord(c) < 160 for c in value):
        raise CmsApiError("invalid_response")
    try:
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        origins = [urlsplit(binding[key]) for key in ("public_origin", "api_origin")]
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or
                parsed.port not in {None, 443} or parsed.query or parsed.fragment or "\\" in value or
                re.search(r"%2f|%5c|%25", parsed.path, re.I) or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in path) or
                any(part in {".", ".."} for part in path.split("/")) or
                not any(parsed.hostname == origin.hostname and parsed.path.startswith(origin.path) for origin in origins)):
            raise CmsApiError("invalid_response")
    except ValueError:
        raise CmsApiError("invalid_response") from None
    return value


def upload_image(binding, credential, *, upload_id, image, title="", alt_text="", caption=""):
    """Send one fixed multipart upload after the caller persists its intent.

    Recheck the private original operation and current CMS scope immediately
    before calling, and fence the result afterward. This transport neither
    retries uncertain effects nor stores/creates publication approval.
    """
    try:
        binding = validate_binding(binding)
        credential = validate_private_credential(credential, binding=binding)
    except CmsContractError:
        raise CmsApiError("credential_context_changed") from None
    slug = _identity(upload_id, image)
    title, alt_text, caption = _text(title, 200), _text(alt_text, 1000), _text(caption, 2000)
    if binding["platform"] == "wordpress":
        fields = {"slug": slug, "title": html.escape(title), "alt_text": alt_text, "caption": html.escape(caption),
            "description": _marker(upload_id, image)}
        base = binding["api_origin"] + "index.php"
        query = "rest_route=%2Fwp%2Fv2%2Fmedia&context=edit"
    else:
        fields = {"purpose": "image", "ref": _marker(upload_id, image)}
        base = binding["api_origin"] + "ghost/api/admin/images/upload/"
        query = ""
    body, content_type = _multipart(fields, image=image, filename=slug + "." + image.extension)
    value, _pagination = _send_json_response(binding, credential, method="POST", base=base, encoded_query=query,
        body=body, content_type=content_type)
    return validate_uploaded_image(value, binding=binding, upload_id=upload_id, image=image)


def validate_uploaded_image(value, *, binding, upload_id, image):
    """Require an exact operation reference; acknowledgement is not live proof."""
    try:
        binding = validate_binding(binding)
        slug = _identity(upload_id, image)
        provider_id = None
        if binding["platform"] == "wordpress":
            if (not isinstance(value, dict) or type(value.get("id")) is not int or not 0 < value["id"] < 10**19 or
                    value.get("type") != "attachment" or value.get("media_type") != "image" or
                    value.get("mime_type") != image.media_type or value.get("slug") != slug or
                    not isinstance(value.get("description"), dict) or value["description"].get("raw") != _marker(upload_id, image)):
                raise CmsApiError("invalid_response")
            provider_id, url = value["id"], value.get("source_url")
        else:
            rows = value.get("images") if isinstance(value, dict) else None
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("ref") != _marker(upload_id, image):
                raise CmsApiError("invalid_response")
            url = rows[0].get("url")
        return UploadedImage(binding["platform"], provider_id, upload_id, image.sha256, _media_url(url, binding))
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, AttributeError, KeyError):
        raise CmsApiError("invalid_response") from None
