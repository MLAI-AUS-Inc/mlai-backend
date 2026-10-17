"""Closed native CMS artifact handoff; checksums never grant publishing consent.

The consumer must resolve the original paid run and current connection privately,
then record its own exact owner approval before any provider effect. This object
contains no provider key, Git capability, worker approval or filesystem paths.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import urlsplit

if __package__:
    from .channel_artifact_contract import canonical_bytes, digest, validate_envelope, ArtifactContractError
    from .cms_native_html import validate_profile_receipt, NativeHtmlError
else:
    from channel_artifact_contract import canonical_bytes, digest, validate_envelope, ArtifactContractError
    from cms_native_html import validate_profile_receipt, NativeHtmlError

FIELDS = frozenset({"schema_version", "kind", "run_id", "run_generation", "envelope", "review_revision",
    "article", "preview_html", "preview_sha256", "images", "editorial_acceptance", "digest"})
ACCEPTANCE_FIELDS = frozenset({"passed", "review_version", "fingerprint", "reviewed_at", "valid_until",
    "transport_sha256", "canonical_article_sha256"})
MAX_HANDOFF_BYTES = 1024 * 1024


class CmsArtifactError(ValueError):
    """Fixed diagnostics exclude private source, tokens and provider responses."""


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _date(value):
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        raise CmsArtifactError("Invalid native CMS artifact handoff")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CmsArtifactError("Invalid native CMS artifact handoff")
    return parsed


def validate_handoff(value, *, now=None):
    """Validate bounded exact bytes and recorded evidence, never its authority."""
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(value, dict) or set(value) != FIELDS or
                len(canonical_bytes(value)) > MAX_HANDOFF_BYTES or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or
                value["kind"] != "cms_article_artifact" or not isinstance(value["run_id"], str) or
                re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["run_id"]) is None or
                type(value["run_generation"]) is not int or not 0 <= value["run_generation"] < 10**12 or
                any(not _hash(value[key]) for key in ("review_revision", "preview_sha256", "digest")) or
                validate_envelope(value["envelope"])["channel"] != "cms"):
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        article = value["article"]
        if not isinstance(article, dict) or set(article) != {"title", "slug", "html", "profile", "meta_title", "meta_description"}:
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        validate_profile_receipt(article["profile"], fragment=article["html"], title=article["title"])
        if (not isinstance(article["slug"], str) or not 0 < len(article["slug"]) <= 150 or
                re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article["slug"]) is None or
                any(not isinstance(article[key], str) or len(article[key]) > bound or
                    any(ord(c) < 32 or c in "<>" for c in article[key])
                    for key, bound in (("meta_title", 200), ("meta_description", 2000)))):
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        preview = value["preview_html"]
        if (not isinstance(preview, str) or not 0 < len(preview.encode()) <= 512 * 1024 or
                hashlib.sha256(preview.encode()).hexdigest() != value["preview_sha256"]):
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        acceptance = value["editorial_acceptance"]
        if (not isinstance(acceptance, dict) or set(acceptance) != ACCEPTANCE_FIELDS or acceptance["passed"] is not True or
                not isinstance(acceptance["review_version"], str) or not 0 < len(acceptance["review_version"]) <= 64 or
                any(not _hash(acceptance[key]) for key in ("fingerprint", "transport_sha256", "canonical_article_sha256")) or
                acceptance["transport_sha256"] != article["profile"]["html_sha256"] or
                _date(acceptance["reviewed_at"]) > now or
                acceptance["valid_until"] is not None and _date(acceptance["valid_until"]) <= now):
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        images = value["images"]
        if not isinstance(images, list) or len(images) > 8:
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        identities = set()
        for image in images:
            if (not isinstance(image, dict) or set(image) != {"component_id", "src", "alt", "sha256", "width", "height"} or
                    not isinstance(image["component_id"], str) or not 0 < len(image["component_id"]) <= 200 or
                    image["component_id"] in identities or not _hash(image["sha256"]) or
                    not isinstance(image["src"], str) or not 0 < len(image["src"]) <= 8192 or
                    not isinstance(image["alt"], str) or len(image["alt"]) > 4000 or
                    any(type(image[key]) is not int or not 24 <= image[key] <= 16384 for key in ("width", "height")) or
                    image["width"] * image["height"] > 30_000_000):
                raise CmsArtifactError("Invalid native CMS artifact handoff")
            parsed = urlsplit(image["src"])
            if (parsed.scheme != "https" or parsed.netloc != "firebasestorage.googleapis.com" or parsed.fragment or
                    not parsed.path.startswith("/v0/b/") or "/o/" not in parsed.path or
                    any(ord(c) < 33 for c in image["src"]) or "\\" in image["src"]):
                raise CmsArtifactError("Invalid native CMS artifact handoff")
            identities.add(image["component_id"])
        if digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]:
            raise CmsArtifactError("Invalid native CMS artifact handoff")
        return deepcopy(value)
    except CmsArtifactError:
        raise
    except (ArtifactContractError, NativeHtmlError, ValueError, TypeError, AttributeError, KeyError, RecursionError, UnicodeError):
        raise CmsArtifactError("Invalid native CMS artifact handoff") from None


def make_handoff(**fields):
    """Seal a locally checked snapshot; consumers still need fresh authority."""
    value = {"schema_version": 1, "kind": "cms_article_artifact", **deepcopy(fields)}
    value["digest"] = digest(value)
    return validate_handoff(value)
