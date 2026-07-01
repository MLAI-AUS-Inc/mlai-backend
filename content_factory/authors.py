"""Article author profiles for an organization.

Authors are stored on ``OrganizationContentConfig.authors`` as a list of canonical records plus a
``default_author_id`` pointer. The same shape powers two consumers:

- the ``GET /api/content-factory/org/config`` response that content-factory re-fetches each run,
  where ``default_author_name`` / ``default_author_profile`` seed the renderer's inline author
  registry (so every generated article has a real byline); and
- the article-generation payload's per-article byline (an optional ``author_id`` override).

Keeping normalization here means the stored shape, the GET response, and the run payload never
drift apart.
"""

import re
from typing import Any, Dict, List, Optional


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _slugify_author_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "author"


def _normalize_same_as(raw: Any) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if isinstance(entry, dict):
            label = _clean(entry.get("label") or entry.get("name"))
            href = _clean(entry.get("href") or entry.get("url"))
        else:
            label = ""
            href = _clean(entry)
        if href:
            items.append({"label": label or "Profile", "href": href})
    return items


def normalize_author(raw: Any) -> Optional[Dict[str, Any]]:
    """Coerce one raw author record (from the UI or API) into the canonical stored shape.

    Returns ``None`` for records without a name — an author with no byline is not usable.
    """
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("name") or raw.get("author"))
    if not name:
        return None
    author_id = _clean(raw.get("id") or raw.get("personId") or raw.get("key")) or _slugify_author_id(name)
    author: Dict[str, Any] = {
        "id": author_id,
        "name": name,
        "role": _clean(raw.get("role") or raw.get("authorTitle")),
        "credentials": _clean(raw.get("credentials")),
        "bio": _clean(raw.get("bio") or raw.get("authorBio")),
        "avatarUrl": _clean(raw.get("avatarUrl") or raw.get("avatar")),
        "avatarAlt": _clean(raw.get("avatarAlt")),
        "url": _clean(raw.get("url") or raw.get("website")),
        "sameAs": _normalize_same_as(raw.get("sameAs") or raw.get("same_as")),
    }
    if not author["avatarAlt"] and author["avatarUrl"]:
        author["avatarAlt"] = f"{name} profile photo"
    return author


def normalize_authors(raw: Any) -> List[Dict[str, Any]]:
    """Normalize a list of raw author records, dropping blanks and de-duping by id (first wins)."""
    if not isinstance(raw, list):
        return []
    seen: set = set()
    authors: List[Dict[str, Any]] = []
    for item in raw:
        author = normalize_author(item)
        if not author or author["id"] in seen:
            continue
        seen.add(author["id"])
        authors.append(author)
    return authors


def resolve_default_author(
    authors: List[Dict[str, Any]],
    default_author_id: str = "",
) -> Optional[Dict[str, Any]]:
    """The byline used when a run names no author: the configured default, else the first author."""
    if not authors:
        return None
    wanted = _clean(default_author_id)
    if wanted:
        for author in authors:
            if author.get("id") == wanted:
                return author
    return authors[0]


def author_profile_for_renderer(author: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Project a stored author into the ``default_author_profile`` shape the renderer reads.

    Mirrors the keys ``content-factory``'s ``_resolve_default_author_profile`` consumes; empty
    fields are dropped so the renderer's own fallbacks apply.
    """
    if not author:
        return {}
    profile = {
        "personId": author.get("id", ""),
        "name": author.get("name", ""),
        "role": author.get("role", ""),
        "credentials": author.get("credentials", ""),
        "bio": author.get("bio", ""),
        "avatarUrl": author.get("avatarUrl", ""),
        "avatarAlt": author.get("avatarAlt", ""),
        "url": author.get("url", ""),
        "sameAs": author.get("sameAs", []),
    }
    return {key: value for key, value in profile.items() if value}


def org_config_author_payload(config: Any) -> Dict[str, Any]:
    """authors + default pointer + renderer-facing default profile, for the GET /org/config response."""
    authors = normalize_authors(getattr(config, "authors", None) if config else None)
    default_author_id = _clean(getattr(config, "default_author_id", "") if config else "")
    default_author = resolve_default_author(authors, default_author_id)
    return {
        "authors": authors,
        "default_author_id": default_author.get("id", "") if default_author else "",
        "default_author_name": default_author.get("name", "") if default_author else "",
        "default_author_profile": author_profile_for_renderer(default_author),
    }
