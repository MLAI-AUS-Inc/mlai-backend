"""Warn when a saved author bio says things the founder's own site does not (plan workstream 5).

La Vie's author bio said its author "leads our editorial team" and has "over a
decade of research"; neither was on her site, and the article's final check
then held the article for those phrases. This checks a bio when the founder
saves an author profile, against the text of the site's About page, and returns
each bio sentence whose specific facts (its distinctive words, every number and
every length of time) are not found there. It is a warning only: the save is
never refused and nothing is changed.

The check is deterministic word evidence, not a model judgment: a sentence is
"found" when its numbers and durations all appear on the page and most of its
distinctive words do. Off unless ``ARTICLES_AUTHOR_BIO_CHECK_ENABLED`` is true.
"""
from __future__ import annotations

from html.parser import HTMLParser
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

ABOUT_PATHS = ("/about", "/about-us", "/our-story")
MIN_SUPPORTED_SHARE = 0.7
MAX_PHRASES = 5
MAX_PHRASE_CHARS = 300
STOPWORDS = frozenset("""
a about above after again all also an and any are as at be been being both but by can could did do does
for from had has have having he her here hers him his how i if in into is it its itself just me more most
my no nor not of off on once only or other our ours out over own same she should so some such than that
the their theirs them then there these they this those through to too under until up very was we were
what when where which while who whom why will with would you your yours
""".split())
# Lengths of time and other quantities a bio states as fact.
DURATION = re.compile(r"\b(?:decade|decades|years?|months?|century|centuries)\b", re.I)
NUMBER = re.compile(r"\b\d[\d,.]*\+?|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twenty|hundred|thousand)\b", re.I)
WORD = re.compile(r"[a-z][a-z'\-]+")
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def bio_check_enabled() -> bool:
    return bool(getattr(settings, "ARTICLES_AUTHOR_BIO_CHECK_ENABLED", False))


def _stem(word: str) -> str:
    word = word.strip("'-")
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _words(text: str) -> set[str]:
    return {_stem(word) for word in WORD.findall(text.lower()) if word not in STOPWORDS and len(word) > 2}


def unsupported_bio_phrases(bio: str, page_text: str) -> list[str]:
    """Bio sentences whose facts the page text does not state, in bio order (at most five)."""
    page = " ".join(str(page_text or "").split()).lower()
    page_words = _words(page)
    found = []
    for sentence in SENTENCE.split(" ".join(str(bio or "").split())):
        sentence = sentence.strip()
        if not sentence:
            continue
        lowered = sentence.lower()
        quantities = [match.group(0).lower() for pattern in (NUMBER, DURATION) for match in pattern.finditer(lowered)]
        if any(quantity not in page for quantity in quantities):
            found.append(sentence)
            continue
        words = _words(sentence)
        if words and len(words & page_words) / len(words) < MIN_SUPPORTED_SHARE:
            found.append(sentence)
    return [text[:MAX_PHRASE_CHARS] for text in found][:MAX_PHRASES]


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html or "")
    return " ".join(" ".join(parser.parts).split())


def about_page_text(domain: str, *, fetch=None) -> str | None:
    """The visible text of the site's About page, or None when no About page could be read."""
    if fetch is None:
        from .website_live_fetch import fetch_live_route as fetch
    apex = str(domain or "").strip().lower().removeprefix("www.")
    if not apex:
        return None
    for path in ABOUT_PATHS:
        try:
            body = fetch(f"https://{apex}{path}", apex)
        except Exception as exc:  # noqa: BLE001 - a missing page is "unchecked", never an error
            logger.info("author_bio_check_about_unavailable path=%s error_type=%s", path, type(exc).__name__)
            continue
        if isinstance(body, tuple):
            body = body[0]  # fetch_live_route returns (body, headers).
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        if isinstance(body, dict):
            body = body.get("html") or body.get("body") or ""
        text = html_text(str(body or ""))
        if text:
            return text
    return None


def author_bio_warnings(authors, domain, *, fetch=None) -> dict | None:
    """The save response's warning block, or None while the setting is off or no author has a bio."""
    if not bio_check_enabled():
        return None
    with_bio = [author for author in authors or [] if isinstance(author, dict) and str(author.get("bio") or "").strip()]
    if not with_bio:
        return None
    page = about_page_text(domain, fetch=fetch)
    if page is None:
        return {"status": "unchecked",
                "message": "We couldn't read your About page, so we couldn't check your author bio against it."}
    items = []
    for author in with_bio:
        phrases = unsupported_bio_phrases(author["bio"], page)
        if phrases:
            items.append({"authorId": author.get("id"), "phrases": phrases})
    if not items:
        return {"status": "checked", "items": []}
    return {"status": "unsupported", "items": items,
            "message": "Some of your author bio isn't on your About page. Articles can only say what your site "
                       "backs up, so check these sentences or add them to your About page."}
