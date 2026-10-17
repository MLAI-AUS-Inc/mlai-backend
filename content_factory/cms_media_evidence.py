"""Pinned public raster observations; provider acknowledgement is not evidence."""

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .channel_artifact_contract import digest
from .cms_api_transport import CmsApiError
from .cms_connection_contract import validate_binding, CmsContractError
from .cms_media_transport import prepare_image, _media_url, MAX_IMAGE_BYTES, UploadedImage
from .website_contract import WebsiteAuthorityError
from .website_live_fetch import public_addresses, PinnedHTTPSConnection

OBSERVATION_FIELDS = {"schema_version", "kind", "status", "scope_sha256", "handoff_digest", "upload_id", "component_id",
    "source_url_sha256", "source_sha256", "provider_id", "public_url", "public_sha256", "media_type", "width", "height", "checked_at", "digest"}


def _source_url(asset):
    if (not isinstance(asset, dict) or set(asset) != {"component_id", "src", "alt", "sha256", "width", "height"} or
            not isinstance(asset["src"], str) or not 0 < len(asset["src"]) <= 8192 or
            not isinstance(asset["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]) is None or
            not isinstance(asset["component_id"], str) or not 0 < len(asset["component_id"]) <= 200 or
            not isinstance(asset["alt"], str) or len(asset["alt"]) > 4000 or
            any(type(asset[key]) is not int or not 24 <= asset[key] <= 4096 for key in ("width", "height"))):
        raise CmsApiError("invalid_image")
    parsed = urlsplit(asset["src"])
    if (parsed.scheme != "https" or parsed.netloc != "firebasestorage.googleapis.com" or parsed.fragment or
            not parsed.path.startswith("/v0/b/") or "/o/" not in parsed.path or "\\" in asset["src"] or
            any(ord(c) < 33 or 127 <= ord(c) < 160 for c in asset["src"])):
        raise CmsApiError("invalid_image")
    return asset["src"]


def _read_image(url):
    """Fetch once without any service/provider credential or redirect."""
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    parsed = urlsplit(url)
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        _validated, addresses = public_addresses(base, parsed.hostname)
    except (ValueError, TypeError, OSError, WebsiteAuthorityError):
        raise CmsApiError("origin_unavailable") from None
    client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
    response = None
    try:
        client.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
            "Host": parsed.hostname, "Connection": "close", "Accept": "image/png,image/jpeg,image/webp",
            "Accept-Encoding": "identity"})
        response = client.getresponse()
        if response.status == 429 or response.status >= 500:
            raise CmsApiError("transient")
        if response.status != 200:
            raise CmsApiError("invalid_image")
        media_type = response.getheader("Content-Type", "").split(";", 1)[0].lower().strip()
        if (media_type not in {"image/png", "image/jpeg", "image/webp"} or
                response.getheader("Content-Encoding", "identity").lower() != "identity"):
            raise CmsApiError("invalid_image")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 10 or int(length) > MAX_IMAGE_BYTES):
            raise CmsApiError("invalid_image")
        image = prepare_image(response.read(MAX_IMAGE_BYTES + 1))
        if image.media_type != media_type:
            raise CmsApiError("invalid_image")
        return image
    except CmsApiError:
        raise
    except (OSError, http.client.HTTPException):
        raise CmsApiError("transient") from None
    except (ValueError, TypeError, UnicodeError):
        raise CmsApiError("invalid_image") from None
    finally:
        if response is not None:
            response.close()
        client.close()


def fetch_source_image(asset):
    """Require the exact generated original pixels, dimensions and source hash."""
    try:
        image = _read_image(_source_url(asset))
        if (image.sha256 != asset["sha256"] or image.width != asset["width"] or image.height != asset["height"]):
            raise CmsApiError("invalid_image")
        return image
    except CmsApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CmsApiError("invalid_image") from None


def observe_owned_image(binding, *, handoff_digest, asset, acknowledgement, checked_at=None):
    """Observe the exact acknowledged CMS URL serving the original raster.

    The caller must own and recheck the private upload intent around this read.
    No provider capability, publication consent or live-article claim is minted.
    """
    try:
        binding = validate_binding(binding)
        _source_url(asset)
        if (not isinstance(acknowledgement, UploadedImage) or acknowledgement.public_bytes_verified is not False or
                acknowledgement.platform != binding["platform"] or acknowledgement.source_sha256 != asset["sha256"] or
                not isinstance(acknowledgement.upload_id, str) or str(UUID(acknowledgement.upload_id)) != acknowledgement.upload_id or
                not isinstance(handoff_digest, str) or re.fullmatch(r"[a-f0-9]{64}", handoff_digest) is None or
                binding["platform"] == "wordpress" and (type(acknowledgement.provider_id) is not int or not 0 < acknowledgement.provider_id < 10**19) or
                binding["platform"] == "ghost" and acknowledgement.provider_id is not None):
            raise CmsApiError("invalid_image")
        url = _media_url(acknowledgement.url, binding)
        image = _read_image(url)
        if (image.sha256 != asset["sha256"] or image.width != asset["width"] or image.height != asset["height"]):
            raise CmsApiError("invalid_image")
        now = checked_at or datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise CmsApiError("invalid_image")
        receipt = {"schema_version": 1, "kind": "cms_public_image_observation", "status": "bytes_verified",
            "scope_sha256": digest(binding), "handoff_digest": handoff_digest, "upload_id": acknowledgement.upload_id,
            "component_id": asset["component_id"], "source_url_sha256": hashlib.sha256(asset["src"].encode()).hexdigest(),
            "source_sha256": asset["sha256"], "provider_id": acknowledgement.provider_id, "public_url": url,
            "public_sha256": image.sha256, "media_type": image.media_type, "width": image.width, "height": image.height,
            "checked_at": now.isoformat()}
        return {**receipt, "digest": digest(receipt)}
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError):
        raise CmsApiError("invalid_image") from None


def validate_owned_observation(value, *, binding, handoff_digest, asset, acknowledgement, now=None):
    """Require fresh exact evidence from the caller's private original upload.

    This checks consistency and age only. A caller must load the receipt from
    its own durable operation; owner/worker JSON cannot establish its origin.
    """
    try:
        binding = validate_binding(binding)
        _source_url(asset)
        now = now or datetime.now(timezone.utc)
        if (not isinstance(value, dict) or set(value) != OBSERVATION_FIELDS or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                value["kind"] != "cms_public_image_observation" or value["status"] != "bytes_verified" or
                not isinstance(acknowledgement, UploadedImage) or acknowledgement.public_bytes_verified is not False or
                acknowledgement.platform != binding["platform"] or acknowledgement.source_sha256 != asset["sha256"] or
                not isinstance(acknowledgement.upload_id, str) or str(UUID(acknowledgement.upload_id)) != acknowledgement.upload_id or
                not isinstance(handoff_digest, str) or re.fullmatch(r"[a-f0-9]{64}", handoff_digest) is None or
                binding["platform"] == "wordpress" and (type(acknowledgement.provider_id) is not int or not 0 < acknowledgement.provider_id < 10**19) or
                binding["platform"] == "ghost" and acknowledgement.provider_id is not None or
                value["scope_sha256"] != digest(binding) or value["handoff_digest"] != handoff_digest or
                value["upload_id"] != acknowledgement.upload_id or value["component_id"] != asset["component_id"] or
                value["source_url_sha256"] != hashlib.sha256(asset["src"].encode()).hexdigest() or
                value["source_sha256"] != asset["sha256"] or value["public_sha256"] != asset["sha256"] or
                type(value["provider_id"]) is not type(acknowledgement.provider_id) or value["provider_id"] != acknowledgement.provider_id or
                value["public_url"] != _media_url(acknowledgement.url, binding) or
                value["media_type"] not in {"image/png", "image/jpeg", "image/webp"} or
                any(type(value[key]) is not int or value[key] != asset[key] for key in ("width", "height")) or
                not isinstance(value["checked_at"], str) or len(value["checked_at"]) > 64 or
                value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise CmsApiError("invalid_image")
        checked = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
        if (checked.tzinfo is None or now.tzinfo is None or now - checked > timedelta(minutes=15) or checked - now > timedelta(seconds=30)):
            raise CmsApiError("invalid_image")
        return value
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError):
        raise CmsApiError("invalid_image") from None
