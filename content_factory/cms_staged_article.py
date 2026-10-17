"""Exact image-only substitution before native owner review, never at publish."""

from copy import deepcopy
import hashlib
import html
from html.parser import HTMLParser
import re

from .channel_artifact_contract import canonical_bytes, digest
from .cms_native_html import profile_receipt, preview_document, NativeHtmlError
from .cms_media_contract import validate_state, acknowledgement
from .cms_artifact_review import validate_review_surface, _signature
from .cms_api_transport import CmsApiError
from bs4 import BeautifulSoup

FIELDS = {"schema_version", "kind", "substitution_profile", "scope_sha256", "source_run_id", "source_generation",
    "envelope_digest", "source_handoff_digest", "source_review_revision", "billing_digest", "original_editorial_acceptance",
    "article", "preview_html", "preview_sha256", "media", "revision"}


class _ImageDestinations(HTMLParser):
    def __init__(self, source, assets, destinations):
        super().__init__(convert_charrefs=True)
        self.source, self.assets, self.destinations = source, assets, destinations
        self.offsets = [0] + [match.end() for match in re.finditer("\n", source)]
        self.replacements, self.index = [], 0

    def handle_starttag(self, tag, attrs):
        if tag != "img": return
        if self.index >= len(self.assets):
            raise CmsApiError("invalid_request")
        asset = self.assets[self.index]
        values = dict(attrs)
        if values.get("src") != asset["src"] or values.get("alt", "") != asset["alt"]:
            raise CmsApiError("invalid_request")
        raw = self.get_starttag_text()
        matches = list(re.finditer(r'(?<= src=")[^"]*', raw))
        if len(matches) != 1:
            raise CmsApiError("invalid_request")
        line, column = self.getpos()
        offset = self.offsets[line - 1] + column
        match = matches[0]
        self.replacements.append((offset + match.start(), offset + match.end(), html.escape(self.destinations[self.index], quote=True)))
        self.index += 1

    handle_startendtag = handle_starttag


def replace_image_destinations(fragment, *, title, assets, destinations):
    """Change only canonical src attribute ranges, preserving all other bytes."""
    try:
        profile_receipt(fragment, title=title)
        if not isinstance(assets, list) or not isinstance(destinations, list) or len(assets) != len(destinations):
            raise CmsApiError("invalid_request")
        parser = _ImageDestinations(fragment, assets, destinations)
        parser.feed(fragment); parser.close()
        if parser.index != len(assets):
            raise CmsApiError("invalid_request")
        result = fragment
        for start, end, value in reversed(parser.replacements):
            result = result[:start] + value + result[end:]
        profile_receipt(result, title=title)
        return result
    except CmsApiError:
        raise
    except (NativeHtmlError, ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise CmsApiError("invalid_request") from None


def build_staged_article(source, *, media_intents, media_states, allow_rechecking=False):
    """Derive from an original private CurrentCmsArticle and owned media set.

    Historical public observations identify exact unchanged rasters. The final
    approval/publisher must separately require fresh observations; this artifact
    never grants approval. Re-observing identical images leaves its revision
    unchanged, avoiding an unnecessary new owner action for a timestamp refresh.
    """
    try:
        handoff = validate_review_surface(source.handoff)
        if (not isinstance(media_intents, list) or not isinstance(media_states, list) or
                len(media_intents) != len(handoff["images"]) or len(media_states) != len(media_intents)):
            raise CmsApiError("invalid_request")
        identities, destinations = [], []
        for intent, state, asset in zip(media_intents, media_states, handoff["images"]):
            validate_state(state, intent)
            if (state["phase"] not in ({"public_verified", "public_recheck"} if allow_rechecking else {"public_verified"}) or intent["binding"] != source.admitted.scope.binding or
                    intent["review_operation_id"] != source.review_operation_id or intent["title"] != handoff["article"]["title"] or
                    intent["handoff_digest"] != handoff["digest"] or intent["asset"] != asset or
                    intent["source_run_id"] != handoff["run_id"] or intent["source_generation"] != handoff["run_generation"]):
                raise CmsApiError("invalid_request")
            ack = acknowledgement(state["acknowledgement"], intent)
            evidence = state["evidence"]
            destinations.append(ack.url)
            identities.append({"component_id": asset["component_id"], "upload_id": ack.upload_id,
                "provider_id": ack.provider_id, "source_url_sha256": hashlib.sha256(asset["src"].encode()).hexdigest(),
                "source_sha256": asset["sha256"], "public_url": ack.url, "public_sha256": evidence["public_sha256"],
                "media_type": evidence["media_type"], "width": evidence["width"], "height": evidence["height"]})
        article = deepcopy(handoff["article"])
        article["html"] = replace_image_destinations(article["html"], title=article["title"], assets=handoff["images"], destinations=destinations)
        article["profile"] = profile_receipt(article["html"], title=article["title"])
        # Independent semantic consistency, in addition to the exact lexical
        # change list. No prose/code/href/table/list boundary may be rewritten.
        before = _signature(BeautifulSoup(handoff["article"]["html"], "html.parser").article)
        after = _signature(BeautifulSoup(article["html"], "html.parser").article)
        if any(before[key] != after[key] for key in before if key != "images"):
            raise CmsApiError("invalid_request")
        preview = preview_document(article["html"], title=article["title"])
        value = {"schema_version": 1, "kind": "cms_staged_article", "substitution_profile": "cms_exact_media_substitution_v1",
            "scope_sha256": digest(source.admitted.scope.binding), "source_run_id": handoff["run_id"],
            "source_generation": handoff["run_generation"], "envelope_digest": handoff["envelope"]["digest"],
            "source_handoff_digest": handoff["digest"], "source_review_revision": handoff["review_revision"],
            "billing_digest": source.billing["digest"], "original_editorial_acceptance": deepcopy(handoff["editorial_acceptance"]),
            "article": article, "preview_html": preview, "preview_sha256": hashlib.sha256(preview.encode()).hexdigest(), "media": identities}
        return {**value, "revision": digest(value)}
    except CmsApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise CmsApiError("invalid_request") from None


def validate_staged_article(value, source, *, media_intents, media_states, allow_rechecking=False):
    """Reconstruct exact expected bytes from private inputs, never sender claims."""
    expected = build_staged_article(source, media_intents=media_intents, media_states=media_states, allow_rechecking=allow_rechecking)
    try:
        if not isinstance(value, dict) or set(value) != FIELDS or canonical_bytes(value) != canonical_bytes(expected):
            raise CmsApiError("invalid_request")
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None
    return expected
