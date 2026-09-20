"""Content identity for a live HTTP observation, independent of merge history."""
import hashlib
import ipaddress
import socket
import unicodedata
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, UnicodeDammit

VERSION = "2026-09-14.1"
MAX_BYTES = 2 * 1024 * 1024


def public_article_url(url, domain):
    parsed = urlsplit(url)
    host = str(domain).removeprefix("https://").removeprefix("http://").strip("/").lower()
    if (parsed.scheme != "https" or parsed.hostname != host or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise ValueError("Live observation must use the organization's exact HTTPS origin")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Live observation requires a public host")
    return url


def article_body(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Missing or oversized article body")
    decoded = UnicodeDammit(raw, is_html=True)
    text = decoded.unicode_markup
    if not text or decoded.contains_replacement_characters or "\ufffd" in text or any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise ValueError("Article encoding is not lossless")
    soup = BeautifulSoup(text, "html.parser")
    candidates = soup.select("[data-cf-article-body], [data-article-content]") or soup.select("article")
    if len(candidates) != 1 or len(candidates[0].select("h1")) != 1:
        raise ValueError("A unique article body and title are required")
    body = candidates[0]
    for ignored in body.select("script, style, template, noscript, [hidden], [aria-hidden=true]"):
        ignored.decompose()
    normalized = " ".join(unicodedata.normalize("NFC", body.get_text(" ", strip=True)).split())
    if len(normalized.split()) < 30:
        raise ValueError("Article body is incomplete")
    if len(normalized) > 200_000:
        raise ValueError("Article body exceeds receipt storage limits")
    return normalized, soup


def compare_live_body(expected_html, observed_bytes, *, canonical_url):
    expected, _ = article_body(expected_html)
    observed, soup = article_body(observed_bytes)
    canonicals = soup.select('link[rel="canonical"][href]')
    if len(canonicals) != 1 or canonicals[0]["href"].rstrip("/") != canonical_url.rstrip("/"):
        raise ValueError("Live canonical identity does not match the article")
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()
    observed_hash = hashlib.sha256(observed.encode()).hexdigest()
    return {"version": VERSION, "state": "verified" if observed_hash == expected_hash else "content_mismatch",
            "method": "http_article_body", "expected_body_sha256": expected_hash,
            "observed_body_sha256": observed_hash, "response_sha256": hashlib.sha256(observed_bytes).hexdigest(),
            "observed_body_text": observed,
            "canonical_url": canonical_url}
