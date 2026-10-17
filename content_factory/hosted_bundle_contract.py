"""Canonical inert hosted file contract; checksums never grant publication rights.

Only public static files enter the export. Private manifests stay outside it.
No provider, model, repository, database or HTTP client is imported here.
"""

from dataclasses import dataclass, field
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
from io import BytesIO
import ipaddress
import re
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID
import warnings
import zipfile
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from PIL import Image

if __package__:
    from .channel_artifact_contract import digest
    from .cms_native_html import native_fragment, NativeHtmlError
else:
    from channel_artifact_contract import digest
    from cms_native_html import native_fragment, NativeHtmlError


MAX_ARTICLES = 500
MAX_ASSETS = 256
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = MAX_BUNDLE_BYTES + 1024 * 1024
PAGE_SIZE = 24
_ARTICLE_FIELDS = {"article_id", "slug", "title", "description", "body_html", "body_sha256", "created_at"}
_ENTRY_FIELDS = {"article_id", "title_sha256", "body_sha256", "body_offset", "body_bytes", "path"}
_MANIFEST_FIELDS = {"schema_version", "kind", "renderer_version", "scope_sha256", "public_origin", "articles", "files", "digest"}
_IMAGES = {"png": ("PNG", "image/png"), "jpg": ("JPEG", "image/jpeg"), "webp": ("WEBP", "image/webp")}
_CSP_META = "default-src 'none'; img-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'"
HEADER_POLICY = MappingProxyType({"Content-Security-Policy": _CSP_META + "; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
_COLOR = r"(?:\#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|rgba?\([0-9.,% ]{3,60}\))"
_FAMILY = r'''(?:[a-zA-Z][a-zA-Z0-9 -]*|'[a-zA-Z0-9 -]+'|"[a-zA-Z0-9 -]+")'''
_FONT = _FAMILY + r"(?:, *" + _FAMILY + r")*"
_CSS_TEMPLATE = (
    ":root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:%s;color:%s;font:1rem/1.7 %s}"
    "header,main,footer{max-width:70rem;margin:auto;padding:1.5rem}a{color:%s}h1,h2,h3{color:%s;font-family:%s;line-height:1.2}"
    ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%%,18rem),1fr));gap:1.5rem}.card{padding:1.5rem;border:1px solid #d6dce4;border-radius:1rem;background:%s}"
    "article{overflow-wrap:anywhere}img{max-width:100%%;height:auto}pre,table{display:block;max-width:100%%;overflow:auto}pre{white-space:pre}nav{display:flex;gap:1rem;flex-wrap:wrap}time,footer{color:%s}@media(max-width:40rem){h1{font-size:2rem}header,main,footer{padding:1rem}}"
)
_CSS_PARTS = _CSS_TEMPLATE.replace("%%", "%").split("%s")
_CSS_PATTERN = re.compile("".join(re.escape(part) + (pattern if index < 8 else "") for index, (part, pattern) in
    enumerate(zip(_CSS_PARTS, [_COLOR, _COLOR, _FONT, _COLOR, _COLOR, _FONT, _COLOR, _COLOR, ""]))))


class HostedBundleError(ValueError):
    """Fixed diagnostics exclude article content, URLs and private reservations."""


@dataclass(frozen=True)
class BundleFile:
    """One public immutable file, separate from the private candidate manifest."""
    path: str
    media_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class HostedBundle:
    """Prepared bytes and identities, without approval, delivery or live claims."""
    manifest: dict = field(repr=False)
    files: tuple[BundleFile, ...] = field(repr=False)

    def export_zip(self):
        """Export only public files; private reservation metadata never enters ZIP."""
        validate_bundle(self)
        result = BytesIO()
        with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as archive:
            for item in self.files:
                info = zipfile.ZipInfo(item.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, item.content)
        return result.getvalue()


def load_export(manifest, archive_bytes):
    """Read canonical bounded public ZIP bytes without extracting or trusting paths.

    Callers must still compare the private scope and exact approved revision to
    their own original reservation before any delivery. This is only a decoder.
    """
    try:
        if (not isinstance(archive_bytes, bytes) or not 0 < len(archive_bytes) <= MAX_EXPORT_BYTES or
                not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS or not isinstance(manifest.get("files"), list) or
                not 0 < len(manifest["files"]) <= 1024):
            _fail()
        expected = manifest["files"]
        for item in expected:
            if (not isinstance(item, dict) or set(item) != {"path", "media_type", "sha256", "bytes"} or
                    not isinstance(item["path"], str) or not isinstance(item["media_type"], str) or
                    not isinstance(item["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None or
                    type(item["bytes"]) is not int or not 0 < item["bytes"] <= MAX_BUNDLE_BYTES):
                _fail()
        if sum(item["bytes"] for item in expected) > MAX_BUNDLE_BYTES:
            _fail()
        files = []
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (archive.comment or len(infos) != len(expected) or len(set(names)) != len(names) or
                    names != [item["path"] for item in expected]):
                _fail()
            for info, item in zip(infos, expected):
                if (info.compress_type != zipfile.ZIP_STORED or info.flag_bits or info.extra or info.comment or
                        info.is_dir() or info.file_size != item["bytes"] or info.compress_size != info.file_size or
                        info.create_system != 3 or info.external_attr != 0o100644 << 16):
                    _fail()
                files.append(BundleFile(info.filename, item["media_type"], archive.read(info)))
        result = HostedBundle(deepcopy(manifest), tuple(files))
        if result.export_zip() != archive_bytes:
            _fail()
        return result
    except HostedBundleError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, OSError,
            ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        _fail()


def _fail():
    raise HostedBundleError("Invalid hosted static bundle") from None


def _plain(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or not empty and not value.strip() or
            any(ord(c) < 32 or 127 <= ord(c) < 160 or c in "<>" for c in value)):
        _fail()
    return value


def _origin(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        _fail()
    parsed = urlsplit(value) if isinstance(value, str) else None
    host = parsed.hostname if parsed else None
    if (not parsed or parsed.scheme != "https" or not host or len(host) > 253 or "." not in host or host != host.lower() or
            parsed.username is not None or parsed.password is not None or parsed.port is not None or
            parsed.path != "/" or parsed.query or parsed.fragment or value != "https://" + host + "/" or
            not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))):
        _fail()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _fail()
    return value


def _article(value):
    if not isinstance(value, dict) or set(value) != _ARTICLE_FIELDS:
        _fail()
    if (not isinstance(value["article_id"], str) or str(UUID(value["article_id"])) != value["article_id"] or
            not isinstance(value["slug"], str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value["slug"]) is None or
            not 0 < len(value["slug"]) <= 150):
        _fail()
    title = _plain(value["title"], 200)
    _plain(value["description"], 2000, empty=True)
    body = value["body_html"]
    if (native_fragment(body, title=title) != body or not isinstance(value["body_sha256"], str) or
            hashlib.sha256(body.encode()).hexdigest() != value["body_sha256"]):
        _fail()
    parsed = BeautifulSoup(body, "html.parser")
    if len(parsed.get_text(" ", strip=True).split()) < 30 or any(
            str(tag.get("aria-hidden", "")).lower() == "true" for tag in parsed.find_all(True)):
        _fail()
    raw_date = value["created_at"]
    if not isinstance(raw_date, str) or not 0 < len(raw_date) <= 64:
        _fail()
    created = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    if created.tzinfo is None:
        _fail()
    date = created.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    path = "articles/" + value["slug"] + "-" + value["article_id"].replace("-", "") + "/"
    return {**value, "created_at": date, "path": path}, parsed


def _image(path, content):
    match = re.fullmatch(r"assets/([a-f0-9]{64})\.(png|jpg|webp)", path) if isinstance(path, str) else None
    if (not match or not isinstance(content, bytes) or not 0 < len(content) <= 4 * 1024 * 1024 or
            hashlib.sha256(content).hexdigest() != match[1]):
        _fail()
    expected, media_type = _IMAGES[match[2]]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format != expected or not 0 < image.width * image.height <= 16_000_000:
                    _fail()
                image.verify()
    except (OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        _fail()
    return BundleFile(path, media_type, content)


def _page(*, title, description, canonical, brand, css_path, main):
    escape = html.escape
    head = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    head += '<meta http-equiv="Content-Security-Policy" content="' + escape(_CSP_META, quote=True) + '">'
    head += '<title>' + escape(title) + '</title><meta name="description" content="' + escape(description, quote=True) + '">'
    head += '<link rel="canonical" href="' + escape(canonical, quote=True) + '"><link rel="stylesheet" href="/' + css_path + '"></head>'
    return (head + '<body><header><a href="/">' + escape(brand) + '</a></header><main>' + main + '</main><footer>' +
        escape(brand) + '</footer></body></html>\n').encode()


def _detail_main(title, created_at, body):
    return '<h1>' + html.escape(title) + '</h1><time datetime="' + created_at + '">' + created_at[:10] + '</time>\n' + body


def _listing_main(brand, records, index, page_count):
    cards = ''.join('<article class="card"><h2><a href="/' + item["path"] + '">' + html.escape(item["title"]) + '</a></h2><p>' + html.escape(item["description"]) + '</p></article>' for item in records)
    navigation = ""
    if index:
        previous = "/" if index == 1 else f"/page/{index}/"
        navigation += '<a rel="prev" href="' + previous + '">Newer articles</a>'
    if index + 1 < page_count:
        navigation += '<a rel="next" href="/page/' + str(index + 2) + '/">Older articles</a>'
    return '<h1>Articles from ' + html.escape(brand) + '</h1><section class="cards" aria-label="Articles">' + cards + '</section><nav aria-label="Archive pages">' + navigation + '</nav>'


def _document(item, origin, css_path):
    """Require the exact inert shell, including every metadata and chrome byte."""
    source = item.content.decode()
    page = BeautifulSoup(source, "html.parser")
    if (source.count("<main>") != 1 or source.count("</main>") != 1 or
            any(len(page.select(selector)) != 1 for selector in ('head > title', 'head > meta[name="description"]', 'body > header > a'))):
        _fail()
    title = _plain(page.select_one("head > title").get_text(), 200)
    description = _plain(page.select_one('head > meta[name="description"]').get("content"), 2000, empty=True)
    brand = _plain(page.select_one("body > header > a").get_text(), 120)
    main = source.split("<main>", 1)[1].split("</main>", 1)[0]
    if item.content != _page(title=title, description=description, canonical=origin + item.path.removesuffix("index.html"), brand=brand, css_path=css_path, main=main):
        _fail()
    return page, title, description, brand, main


def validate_bundle(value):
    """Check prepared file identities and export bounds, never publishing rights."""
    try:
        if not isinstance(value, HostedBundle) or not isinstance(value.manifest, dict) or not isinstance(value.files, tuple):
            _fail()
        manifest = value.manifest
        if (set(manifest) != _MANIFEST_FIELDS or
                type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["kind"] != "hosted_static_bundle" or
                type(manifest["renderer_version"]) is not int or manifest["renderer_version"] != 1 or
                not isinstance(manifest["scope_sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", manifest["scope_sha256"]) is None or
                not 1 <= len(value.files) <= 1024 or sum(len(item.content) for item in value.files) > MAX_BUNDLE_BYTES):
            _fail()
        origin = _origin(manifest["public_origin"])
        actual = []
        paths = set()
        for item in value.files:
            if (not isinstance(item, BundleFile) or not isinstance(item.content, bytes) or not isinstance(item.path, str) or
                    item.path in paths or re.fullmatch(r"(?:index\.html|robots\.txt|sitemap\.xml|page/[1-9][0-9]*/index\.html|articles/[a-z0-9-]{1,190}/index\.html|assets/[a-f0-9]{64}\.(?:css|png|jpg|webp))", item.path) is None):
                _fail()
            paths.add(item.path)
            suffix = item.path.rsplit(".", 1)[-1]
            expected_type = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8", "xml": "application/xml; charset=utf-8",
                "txt": "text/plain; charset=utf-8", **{key: values[1] for key, values in _IMAGES.items()}}[suffix]
            if item.media_type != expected_type:
                _fail()
            if item.path.startswith("assets/"):
                if item.path.split("/", 1)[1].split(".", 1)[0] != hashlib.sha256(item.content).hexdigest():
                    _fail()
                if suffix != "css":
                    _image(item.path, item.content)
            if suffix == "html":
                page = BeautifulSoup(item.content.decode(), "html.parser")
                canonical = page.select('head > link[rel="canonical"]')
                if len(canonical) != 1 or canonical[0].get("href") != origin + item.path.removesuffix("index.html"):
                    _fail()
            actual.append({"path": item.path, "media_type": item.media_type, "sha256": hashlib.sha256(item.content).hexdigest(), "bytes": len(item.content)})
        if actual != manifest["files"] or [item.path for item in value.files] != sorted(paths) or not {"index.html", "robots.txt", "sitemap.xml"} <= paths:
            _fail()
        if any(not isinstance(item, dict) or set(item) != {"path", "media_type", "sha256", "bytes"} or
                type(item["bytes"]) is not int for item in manifest["files"]):
            _fail()
        if not isinstance(manifest["articles"], list) or not 1 <= len(manifest["articles"]) <= MAX_ARTICLES:
            _fail()
        by_path = {item.path: item for item in value.files}
        sheets = [item for item in value.files if item.path.endswith(".css")]
        if (len(sheets) != 1 or len(sheets[0].content) > 4096 or not _CSS_PATTERN.fullmatch(sheets[0].content.decode()) or
                sum(item.media_type in {kind[1] for kind in _IMAGES.values()} for item in value.files) > MAX_ASSETS):
            _fail()
        documents = {}
        brands = set()
        for public in value.files:
            if not public.path.endswith(".html"):
                continue
            document = _document(public, origin, sheets[0].path)
            documents[public.path] = document
            page = document[0]
            brands.add(document[3])
            if (any(len(page.find_all(tag)) != 1 for tag in ("html", "head", "body", "title")) or
                    len(page.select('link[rel="canonical"]')) != 1 or
                    page.find(["script", "iframe", "object", "embed", "form", "input", "button", "select", "textarea", "base"]) or
                    any(str(name).startswith("on") or name == "style" for tag in page.find_all(True) for name in tag.attrs)):
                _fail()
            policies = page.select("head > meta[http-equiv]")
            styles = page.select('head > link[rel="stylesheet"]')
            if (len(policies) != 1 or str(policies[0].get("http-equiv", "")).lower() != "content-security-policy" or
                    policies[0].get("content") != _CSP_META or len(styles) != 1):
                _fail()
            sheet = styles[0].get("href", "")
            if not sheet.startswith("/") or sheet[1:] not in by_path or not sheet.endswith(".css"):
                _fail()
            for image in page.find_all("img"):
                source = image.get("src", "")
                path = source[len(origin):] if source.startswith(origin) else source[1:] if source.startswith("/") else None
                if path not in by_path or by_path[path].media_type not in {item[1] for item in _IMAGES.values()}:
                    _fail()
            for link in page.select("a[href]"):
                destination = link["href"]
                address = urlsplit(destination)
                if (not destination or destination.startswith("//") or "\\" in destination or
                        address.username is not None or address.password is not None or
                        address.scheme and (address.scheme != "https" or not address.hostname) or
                        any(ord(char) < 33 or 127 <= ord(char) < 160 for char in destination)):
                    _fail()
        if len(brands) != 1:
            _fail()
        article_ids, detail_paths = set(), set()
        records, dates = [], {}
        for item in manifest["articles"]:
            if (not isinstance(item, dict) or set(item) != _ENTRY_FIELDS or not isinstance(item["article_id"], str) or
                    str(UUID(item["article_id"])) != item["article_id"] or item["article_id"] in article_ids or
                    not isinstance(item["path"], str) or re.fullmatch(r"articles/[a-z0-9-]{1,190}/", item["path"]) is None or
                    not item["path"].endswith("-" + item["article_id"].replace("-", "") + "/") or
                    any(not isinstance(item[key], str) or re.fullmatch(r"[a-f0-9]{64}", item[key]) is None for key in ("body_sha256", "title_sha256")) or
                    type(item["body_offset"]) is not int or type(item["body_bytes"]) is not int or item["body_offset"] < 0 or item["body_bytes"] <= 0):
                _fail()
            path = item["path"] + "index.html"
            public = by_path[path].content
            body = public[item["body_offset"]:item["body_offset"] + item["body_bytes"]]
            page, title, description, brand, main = documents[path]
            titles = page.select("h1")
            if (len(body) != item["body_bytes"] or hashlib.sha256(body).hexdigest() != item["body_sha256"] or
                    len(titles) != 1 or len(page.select("article[data-cf-article-body]")) != 1 or
                    hashlib.sha256(titles[0].get_text().encode()).hexdigest() != item["title_sha256"] or
                    native_fragment(body.decode(), title=titles[0].get_text()) != body.decode()):
                _fail()
            times = page.select("main > time")
            if len(times) != 1:
                _fail()
            created = times[0].get("datetime", "")
            candidate, _ = _article({"article_id": item["article_id"], "slug": item["path"][9:].rsplit("-", 1)[0],
                "title": title, "description": description, "body_html": body.decode(), "body_sha256": item["body_sha256"], "created_at": created})
            if created != candidate["created_at"] or main != _detail_main(title, created, body.decode()) or candidate["path"] != item["path"]:
                _fail()
            records.append(candidate)
            dates[origin + item["path"]] = created
            article_ids.add(item["article_id"])
            detail_paths.add(path)
        if records != sorted(records, key=lambda item: (item["created_at"], item["article_id"]), reverse=True):
            _fail()
        if detail_paths != {path for path in paths if path.startswith("articles/")}:
            _fail()
        page_count = (len(article_ids) + PAGE_SIZE - 1) // PAGE_SIZE
        listings = {"index.html", *(f"page/{index}/index.html" for index in range(2, page_count + 1))}
        if {path for path in paths if path.startswith("page/")} != listings - {"index.html"}:
            _fail()
        linked = []
        for index in range(1, page_count + 1):
            path = "index.html" if index == 1 else f"page/{index}/index.html"
            listing, title, description, brand, main = documents[path]
            if (title != "Articles from " + brand or description != title or
                    main != _listing_main(brand, records[(index - 1) * PAGE_SIZE:index * PAGE_SIZE], index - 1, page_count)):
                _fail()
            dates[origin + path.removesuffix("index.html")] = records[0]["created_at"]
            linked.extend(link.get("href") for link in listing.select(".cards > article.card h2 > a"))
        if linked != ["/" + item["path"] for item in manifest["articles"]]:
            _fail()
        sitemap = by_path["sitemap.xml"].content
        if b"<!DOCTYPE" in sitemap.upper() or b"<!ENTITY" in sitemap.upper():
            _fail()
        tree = ElementTree.fromstring(sitemap)
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        locations = [node.text for node in tree.findall(namespace + "url/" + namespace + "loc")]
        expected = {origin + path.removesuffix("index.html") for path in detail_paths | listings}
        if tree.tag != namespace + "urlset" or len(locations) != len(expected) or set(locations) != expected:
            _fail()
        if tree.attrib or len(tree) != len(locations):
            _fail()
        for url in tree:
            if url.tag != namespace + "url" or url.attrib or [child.tag for child in url] != [namespace + "loc", namespace + "lastmod"]:
                _fail()
            changed = datetime.fromisoformat(str(url[1].text).replace("Z", "+00:00"))
            if changed.tzinfo is None or url[1].text != dates.get(url[0].text) or any(child.attrib or len(child) for child in url):
                _fail()
        if (by_path["robots.txt"].content != ("User-agent: *\nAllow: /\nSitemap: " + origin + "sitemap.xml\n").encode() or
                not isinstance(manifest["digest"], str) or re.fullmatch(r"[a-f0-9]{64}", manifest["digest"]) is None or
                digest({key: item for key, item in manifest.items() if key != "digest"}) != manifest["digest"]):
            _fail()
        return value
    except HostedBundleError:
        raise
    except (NativeHtmlError, ElementTree.ParseError, ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        raise HostedBundleError("Invalid hosted static bundle") from None
