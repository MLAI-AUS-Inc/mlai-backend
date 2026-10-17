"""Bounded public-post capture; provider text is data, never execution or consent."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from .cms_api_transport import CmsApiError
from .cms_connection_contract import validate_binding, CmsContractError

PAGE_SIZE = 100
MAX_PAGES = 100
MAX_RECORDS = 10000
MAX_CONTENT_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def new_inventory(binding):
    """Bind this read-only observation to the exact provider consent generation."""
    return {"schema_version": 1, "kind": "cms_public_post_inventory", "binding": validate_binding(binding),
        "coverage": "pending", "query": "public_published_posts_by_id_v1", "next_page": 1,
        "observed_pages": 0, "total_pages": None, "last_provider_id": None, "excluded_count": 0,
        "records": [], "digest": None, "limit_reason": None}


def page_options(binding, inventory):
    """Produce only fixed, bounded pagination parameters; no page-supplied URLs."""
    validate_inventory(inventory, binding=binding)
    if inventory["coverage"] != "pending":
        raise CmsApiError("invalid_request")
    if binding["platform"] == "wordpress":
        return {"method": "GET", "resource": "posts", "query": {"page": inventory["next_page"],
            "per_page": PAGE_SIZE, "context": "edit", "status": "publish", "orderby": "id", "order": "asc"}}
    return {"method": "GET", "resource": "posts", "query": {"page": inventory["next_page"], "limit": PAGE_SIZE,
        "formats": "html", "filter": "status:published+visibility:public", "order": "id asc"}}


def _identifier(value, platform):
    if platform == "wordpress":
        if type(value) is not int or not 1 <= value < 10**19:
            raise CmsApiError("invalid_response")
    elif not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{24}", value) is None:
        raise CmsApiError("invalid_response")
    return value


def _references(values, platform):
    if not isinstance(values, list) or len(values) > 100:
        raise CmsApiError("invalid_response")
    result = []
    for value in values:
        identifier = _identifier(value if platform == "wordpress" else value.get("id") if isinstance(value, dict) else None, platform)
        if identifier in result:
            raise CmsApiError("invalid_response")
        result.append(identifier)
    return result


def _public_url(value, binding, identifier):
    if not isinstance(value, str) or not 0 < len(value) <= 2048 or any(ord(c) < 33 for c in value):
        raise CmsApiError("invalid_response")
    try:
        parsed, origin = urlsplit(value), urlsplit(binding["public_origin"])
        if (parsed.scheme != "https" or parsed.hostname != origin.hostname or parsed.port not in {None, 443} or
                parsed.username is not None or parsed.password is not None or parsed.fragment or
                not parsed.path.startswith(origin.path) or "\\" in value or
                re.search(r"%2f|%5c|%25", parsed.path, re.I) or
                any(part in {".", ".."} for part in unquote(parsed.path).split("/")) or
                any(ord(c) < 32 for c in unquote(parsed.path)) or
                parsed.query and (binding["platform"] != "wordpress" or parse_qsl(parsed.query, keep_blank_values=True) != [("p", str(identifier))])):
            raise CmsApiError("invalid_response")
    except ValueError:
        raise CmsApiError("invalid_response") from None
    return value


def _timestamp(value):
    if not isinstance(value, str) or not 0 < len(value) <= 40 or re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value) is None:
        raise CmsApiError("invalid_response")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise CmsApiError("invalid_response") from None


def _post(row, binding):
    platform = binding["platform"]
    if not isinstance(row, dict):
        raise CmsApiError("invalid_response")
    identifier = _identifier(row.get("id"), platform)
    if platform == "wordpress":
        if row.get("status") != "publish" or row.get("type", "post") != "post":
            raise CmsApiError("invalid_response")
        if row.get("password") or isinstance(row.get("content"), dict) and row["content"].get("protected") is True:
            return identifier, None
        title, content = row.get("title"), row.get("content")
        if not isinstance(title, dict) or not isinstance(content, dict):
            raise CmsApiError("invalid_response")
        if "protected" in content and type(content["protected"]) is not bool:
            raise CmsApiError("invalid_response")
        title, content = title.get("raw", title.get("rendered")), content.get("raw", content.get("rendered"))
        authors = [_identifier(row.get("author"), platform)]
        categories, tags = _references(row.get("categories", []), platform), _references(row.get("tags", []), platform)
        media = row.get("featured_media", 0)
        if type(media) is not int or not 0 <= media < 10**19:
            raise CmsApiError("invalid_response")
        date, url = row.get("date_gmt"), row.get("link")
    else:
        if row.get("status") != "published":
            raise CmsApiError("invalid_response")
        if row.get("visibility") != "public":
            return identifier, None
        title, content = row.get("title"), row.get("html")
        authors, tags = _references(row.get("authors", []), platform), _references(row.get("tags", []), platform)
        categories, media = [], None
        date, url = row.get("published_at"), row.get("url")
    slug = row.get("slug")
    if (not isinstance(slug, str) or not 0 < len(slug) <= 255 or any(ord(c) < 32 for c in slug) or
            not isinstance(title, str) or len(title) > 4096 or not isinstance(content, str) or
            len(content.encode()) > MAX_CONTENT_BYTES):
        raise CmsApiError("invalid_response")
    title = html.unescape(re.sub(r"<[^>]*>", "", title))
    if len(title) > 500 or any(ord(c) < 32 and c not in "\t\n\r" for c in title):
        raise CmsApiError("invalid_response")
    return identifier, {"provider_id": identifier, "slug": slug, "title": title,
        "html": content, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "public_url": _public_url(url, binding, identifier), "published_at": _timestamp(date),
        "author_ids": authors, "category_ids": categories, "tag_ids": tags, "featured_media_id": media}


def _validate_inventory(value, *, binding):
    expected = new_inventory(binding)
    if (not isinstance(value, dict) or set(value) != set(expected) or
            type(value.get("schema_version")) is not int or value["schema_version"] != 1 or
            value.get("kind") != expected["kind"] or value.get("query") != expected["query"] or
            validate_binding(value.get("binding")) != expected["binding"] or
            value.get("coverage") not in {"pending", "complete", "partial"} or
            type(value.get("observed_pages")) is not int or not 0 <= value["observed_pages"] <= MAX_PAGES or
            type(value.get("excluded_count")) is not int or not 0 <= value["excluded_count"] <= MAX_RECORDS or
            not isinstance(value.get("records"), list) or len(value["records"]) + value["excluded_count"] > MAX_RECORDS or
            value.get("total_pages") is not None and (type(value["total_pages"]) is not int or not 0 <= value["total_pages"] <= 10**8) or
            value["coverage"] == "pending" and (type(value.get("next_page")) is not int or value["next_page"] != value["observed_pages"] + 1 or value["next_page"] > MAX_PAGES) or
            value["coverage"] != "pending" and value.get("next_page") is not None or
            value.get("limit_reason") not in {None, "page_limit", "byte_limit", "record_limit"} or
            (value["coverage"] == "partial") != (value.get("limit_reason") is not None)):
        raise CmsApiError("invalid_request")
    try:
        if value["observed_pages"] == 0 and (value["records"] or value["excluded_count"] or value["last_provider_id"] is not None or value["total_pages"] is not None):
            raise CmsApiError("invalid_request")
        previous = None
        for record in value["records"]:
            keys = {"provider_id", "slug", "title", "html", "content_sha256", "public_url", "published_at",
                "author_ids", "category_ids", "tag_ids", "featured_media_id"}
            if not isinstance(record, dict) or set(record) != keys:
                raise CmsApiError("invalid_request")
            identifier = _identifier(record["provider_id"], binding["platform"])
            if previous is not None and identifier <= previous:
                raise CmsApiError("invalid_request")
            previous = identifier
            if (not isinstance(record["slug"], str) or not 0 < len(record["slug"]) <= 255 or
                    any(ord(c) < 32 for c in record["slug"]) or
                    not isinstance(record["title"], str) or len(record["title"]) > 500 or
                    any(ord(c) < 32 and c not in "\t\n\r" for c in record["title"]) or
                    not isinstance(record["html"], str) or len(record["html"].encode()) > MAX_CONTENT_BYTES or
                    hashlib.sha256(record["html"].encode()).hexdigest() != record["content_sha256"] or
                    _timestamp(record["published_at"]) != record["published_at"]):
                raise CmsApiError("invalid_request")
            _public_url(record["public_url"], binding, identifier)
            for field in ("author_ids", "category_ids", "tag_ids"):
                if not isinstance(record[field], list):
                    raise CmsApiError("invalid_request")
                references = record[field] if binding["platform"] == "wordpress" else [{"id": item} for item in record[field]]
                _references(references, binding["platform"])
            media = record["featured_media_id"]
            if binding["platform"] == "wordpress":
                if type(media) is not int or not 0 <= media < 10**19:
                    raise CmsApiError("invalid_request")
            elif media is not None or record["category_ids"]:
                raise CmsApiError("invalid_request")
        if (value["last_provider_id"] is None) != (not value["records"] and value["excluded_count"] == 0):
            raise CmsApiError("invalid_request")
        if value["last_provider_id"] is not None:
            _identifier(value["last_provider_id"], binding["platform"])
            if previous is not None and previous > value["last_provider_id"]:
                raise CmsApiError("invalid_request")
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_MANIFEST_BYTES:
            raise CmsApiError("invalid_request")
        if value["digest"] != (None if value["observed_pages"] == 0 and value["coverage"] == "pending" else _digest({key: item for key, item in value.items() if key != "digest"})):
            raise CmsApiError("invalid_request")
    except (ValueError, TypeError, RecursionError):
        raise CmsApiError("invalid_request") from None
    return value


def validate_inventory(value, *, binding):
    """Reject corrupted or foreign durable input before another provider read."""
    try:
        return _validate_inventory(value, binding=binding)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, CmsContractError):
        raise CmsApiError("invalid_request") from None


def _apply_page(inventory, binding, response, pagination):
    """Accept one ordered page, recording explicit completion or a bounded limit.

    A complete observation describes pagination at that time. It is not an
    immutable CMS snapshot and never certifies a post, build or publication.
    """
    validate_inventory(inventory, binding=binding)
    if inventory["coverage"] != "pending":
        raise CmsApiError("invalid_request")
    page = inventory["next_page"]
    if binding["platform"] == "wordpress":
        rows = response
        pages = pagination.get("pages") if isinstance(pagination, dict) else None
    else:
        if not isinstance(response, dict) or not isinstance(response.get("meta"), dict) or not isinstance(response["meta"].get("pagination"), dict):
            raise CmsApiError("invalid_response")
        meta = response["meta"]["pagination"]
        if type(meta.get("page")) is not int or meta["page"] != page or type(meta.get("limit")) is not int or meta["limit"] != PAGE_SIZE:
            raise CmsApiError("invalid_response")
        rows, pages = response.get("posts"), meta.get("pages")
        expected_next = page + 1 if type(pages) is int and page < pages else None
        if (type(pages) is not int or meta.get("next") != expected_next or
                expected_next is not None and type(meta.get("next")) is not int):
            raise CmsApiError("invalid_response")
    if (not isinstance(rows, list) or len(rows) > PAGE_SIZE or
            pages is not None and (type(pages) is not int or not 0 <= pages <= 10**8 or page > max(pages, 1)) or
            pages == 0 and rows or
            pages is not None and page < pages and not rows):
        raise CmsApiError("invalid_response")
    if inventory["observed_pages"] and inventory["total_pages"] != pages:
        raise CmsApiError("inventory_changed")
    result = deepcopy(inventory)
    result.update(observed_pages=page, next_page=page + 1, total_pages=pages)
    for row in rows:
        identifier, record = _post(row, binding)
        if result["last_provider_id"] is not None and identifier <= result["last_provider_id"]:
            raise CmsApiError("inventory_changed")
        result["last_provider_id"] = identifier
        if record is None:
            result["excluded_count"] += 1
        else:
            result["records"].append(record)
    complete = (page >= pages if pages is not None else len(rows) < PAGE_SIZE)
    if complete:
        result.update(coverage="complete", next_page=None)
    elif page >= MAX_PAGES:
        result.update(coverage="partial", next_page=None, limit_reason="page_limit")
    elif len(result["records"]) + result["excluded_count"] >= MAX_RECORDS:
        result.update(coverage="partial", next_page=None, limit_reason="record_limit")
    if len(json.dumps(result, ensure_ascii=False).encode()) + 128 > MAX_MANIFEST_BYTES:
        # Do not persist a truncated page or advance past uncaptured content.
        result = deepcopy(inventory)
        result.update(coverage="partial", next_page=None, limit_reason="byte_limit")
    result["digest"] = _digest({key: item for key, item in result.items() if key != "digest"})
    return result


def apply_page(inventory, binding, response, pagination):
    """Accept one typed public page or raise a bounded provider diagnostic."""
    try:
        return _apply_page(inventory, binding, response, pagination)
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_response") from None
