"""Deterministic native CMS fragments, before review; never publishing authority."""

from dataclasses import dataclass, field
import hashlib
import html
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

PROFILE = "cms_native_html_v1"
MAX_BYTES = 256 * 1024
TAGS = frozenset("article aside section div span p h1 h2 h3 h4 h5 h6 a img figure figcaption strong em b i u s del ins br hr ul ol li blockquote pre code table thead tbody tfoot tr th td caption sup sub small details summary time dl dt dd".split())
VOID = frozenset({"img", "br", "hr"})
ATTRIBUTES = frozenset({"id", "class", "title", "alt", "href", "src", "width", "height", "loading", "decoding", "rel", "target",
    "role", "scope", "datetime", "colspan", "rowspan", "open", "start", "reversed"})


class NativeHtmlError(ValueError):
    """Fixed diagnostics contain no content, asset URLs, credentials or commands."""


@dataclass
class _Node:
    tag: str
    attrs: list = field(default_factory=list, repr=False)
    children: list = field(default_factory=list, repr=False)


class _Fragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag not in TAGS or len(attrs) > 64:
            raise NativeHtmlError("Unsupported native CMS markup")
        clean, seen = [], set()
        for name, value in attrs:
            if name in seen or len(name) > 100:
                raise NativeHtmlError("Ambiguous native CMS attributes")
            seen.add(name)
            # The CMS theme supplies presentation. Remove CSS before the first
            # artifact review, rather than changing approved markup at publish.
            if name == "style":
                continue
            if (name.startswith("on") or name not in ATTRIBUTES and not name.startswith(("data-", "aria-")) or
                    value is not None and (len(value) > 8192 or any(ord(c) < 32 for c in value))):
                raise NativeHtmlError("Unsupported native CMS attributes")
            if name in {"src", "href"}:
                if not isinstance(value, str) or not value or "\\" in value or value.startswith("//"):
                    raise NativeHtmlError("Unsupported native CMS destination")
                parsed = urlsplit(value)
                if (parsed.username is not None or parsed.password is not None or
                        parsed.scheme and (parsed.scheme != "https" or not parsed.hostname) or
                        not parsed.scheme and not value.startswith(("/", "#")) and ":" in value):
                    raise NativeHtmlError("Unsupported native CMS destination")
            clean.append((name, value))
        node = _Node(tag, clean)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID or len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise NativeHtmlError("Unbalanced native CMS markup")
        self.stack.pop()

    def handle_data(self, data):
        if any(ord(c) < 32 and c not in "\t\n\r" for c in data):
            raise NativeHtmlError("Unsupported native CMS text")
        self.stack[-1].children.append(data)

    def handle_comment(self, data):
        if "mlai-post:" in data or "kg-card-" in data:
            raise NativeHtmlError("Reserved native CMS marker")

    def handle_decl(self, decl):
        raise NativeHtmlError("Unsupported native CMS document")

    def unknown_decl(self, data):
        raise NativeHtmlError("Unsupported native CMS document")


def _text(node):
    return node if isinstance(node, str) else "".join(_text(child) for child in node.children)


def _emit(node):
    if isinstance(node, str):
        return html.escape(node, quote=False)
    attributes = "".join(" " + name + ("" if value is None else '="' + html.escape(value, quote=True) + '"') for name, value in sorted(node.attrs))
    return "<" + node.tag + attributes + ">" + ("" if node.tag in VOID else "".join(_emit(child) for child in node.children) + "</" + node.tag + ">")


def native_fragment(source, *, title):
    """Normalize inert generated HTML once, with the headline owned by the CMS.

    Copy, table/code whitespace, links, image slots and section boundaries are
    preserved. No URL is fetched. A valid output is neither consent nor approval.
    """
    try:
        if (not isinstance(source, str) or not 0 < len(source.encode()) <= MAX_BYTES or not isinstance(title, str) or
                not 0 < len(title) <= 200 or any(ord(c) < 32 or c in "<>" for c in title)):
            raise NativeHtmlError("Invalid native CMS artifact")
        parser = _Fragment()
        parser.feed(source)
        parser.close()
        roots = [node for node in parser.root.children if not isinstance(node, str) or node.strip()]
        if len(parser.stack) != 1 or len(roots) != 1 or not isinstance(roots[0], _Node) or roots[0].tag != "article":
            raise NativeHtmlError("Native CMS requires one article fragment")
        article = roots[0]
        headlines = []
        def collect(node, parent=None):
            if not isinstance(node, _Node): return
            if node.tag == "article" and node is not article:
                raise NativeHtmlError("Ambiguous native CMS body")
            if node.tag == "h1": headlines.append((node, parent))
            if node is not article and any(name == "data-cf-article-body" for name, _ in node.attrs):
                raise NativeHtmlError("Ambiguous native CMS body")
            for child in node.children: collect(child, node)
        collect(article)
        if len(headlines) > 1:
            raise NativeHtmlError("Ambiguous native CMS headline")
        if headlines:
            headline, parent = headlines[0]
            if re.sub(r"\s+", " ", _text(headline)).strip() != re.sub(r"\s+", " ", title).strip():
                raise NativeHtmlError("Native CMS headline changed")
            parent.children.remove(headline)
        article.attrs = [(name, value) for name, value in article.attrs if name != "data-cf-article-body"] + [("data-cf-article-body", "")]
        result = _emit(article) + "\n"
        if len(result.encode()) > MAX_BYTES:
            raise NativeHtmlError("Native CMS artifact exceeds its bound")
        return result
    except NativeHtmlError:
        raise
    except (ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
        raise NativeHtmlError("Invalid native CMS artifact") from None


def preview_document(fragment, *, title):
    """Show the future theme headline outside the exact publishable fragment."""
    normalized = native_fragment(fragment, title=title)
    if fragment != normalized:
        raise NativeHtmlError("Native CMS preview source changed")
    return '<header data-cf-cms-preview-title><h1>' + html.escape(title) + '</h1></header>\n' + fragment


def profile_receipt(fragment, *, title):
    """Identify exact pre-review publication bytes without a Git commit claim."""
    if native_fragment(fragment, title=title) != fragment:
        raise NativeHtmlError("Native CMS profile source changed")
    return {"schema_version": 1, "profile": PROFILE, "title_location": "cms_theme",
        "html_sha256": hashlib.sha256(fragment.encode()).hexdigest(), "title_sha256": hashlib.sha256(title.encode()).hexdigest()}


def validate_profile_receipt(value, *, fragment, title):
    """Require exact closed typed metadata; a valid profile is still not approval."""
    expected = profile_receipt(fragment, title=title)
    if (not isinstance(value, dict) or set(value) != set(expected) or
            any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items())):
        raise NativeHtmlError("Native CMS render profile changed")
    return dict(expected)

