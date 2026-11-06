"""Public-link inspection with address pinning and per-hop redirect validation."""

import http.client
import ipaddress
import re
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .. import policy
from ..hygiene import DATA

SHORTENERS = frozenset(line.strip() for line in (DATA / 'shorteners.txt').read_text().splitlines()
    if line.strip() and not line.startswith('#'))
REDIRECTS = (301, 302, 303, 307, 308)


@dataclass(frozen=True)
class Observation:
    url: str
    final_url: str
    status: int | None
    error: str = ''


def link_targets(body):
    """Include Markdown, HTML and autolinks, preserving link text for mismatch checks."""
    result = [(match[1], match[1]) for match in re.finditer(r'<(https?://[^<>\s]+)>', body or '', re.I)]
    soup = BeautifulSoup('<section>' + (body or '') + '</section>', 'html.parser')
    for anchor in soup.find_all('a', href=True):
        if str(anchor['href']).lower().startswith(('http://', 'https://')):
            result.append((str(anchor['href']), anchor.get_text(' ', strip=True)))
    for image in soup.find_all('img', src=True):
        if str(image['src']).lower().startswith(('http://', 'https://')):
            result.append((str(image['src']), str(image.get('alt') or '')))
    text = soup.get_text(' ')
    for match in re.finditer(r'(?<!!)\[([^\]]*)\]\(\s*<?(https?://)', text, re.I):
        start = match.end() - len(match[2])
        end, depth = start, 0
        while end < len(text):
            character = text[end]
            if character.isspace() or character == '>' or (character == ')' and depth == 0):
                break
            depth += (character == '(') - (character == ')')
            end += 1
        result.append((text[start:end], match[1]))
    known = {url for url, _ in result}
    for match in re.finditer(r'https?://[^\s<>"{}]+', text, re.I):
        url = match[0].rstrip('.,;!?')
        while url.endswith(')') and url.count(')') > url.count('('):
            url = url[:-1]
        if url not in known:
            result.append((url, url)); known.add(url)
    return list(dict.fromkeys(result))


def parsed_url(url):
    """Reject credentials, controls, non-web ports and IP-literal destinations."""
    if not isinstance(url, str) or len(url) > policy.value('CRM_LINK_MAX_LENGTH') or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError('invalid_link')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
            raise ValueError('invalid_link')
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('ip_address_link')
        if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})', host):
            raise ValueError('invalid_link')
        return parsed, host
    except (UnicodeError, ValueError) as exc:
        raise ValueError('ip_address_link' if str(exc) == 'ip_address_link' else 'invalid_link') from None


def public_target(url):
    parsed, host = parsed_url(url)
    port = 443 if parsed.scheme == 'https' else 80
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)},
        key=lambda value: (ipaddress.ip_address(value).version, value))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError('nonpublic_link')
    return parsed, host, addresses[0], port


class PinnedConnection(http.client.HTTPConnection):
    """Use the already-validated address, retaining the TLS name and HTTP Host."""

    def __init__(self, host, address, port, *, encrypted):
        super().__init__(host, port=port, timeout=policy.value('CRM_LINK_TIMEOUT_SECONDS'))
        self.address, self.encrypted = address, encrypted

    def connect(self):
        raw = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.encrypted else raw
            self.sock.settimeout(self.timeout)
        except Exception:
            raw.close()
            raise


def inspect(url):
    current, seen = url, set()
    for hop in range(policy.value('CRM_LINK_MAX_REDIRECTS') + 1):
        client, response = None, None
        try:
            if current in seen:
                return Observation(url, current, None, 'redirect_loop')
            seen.add(current)
            parsed, host, address, port = public_target(current)
            client = PinnedConnection(host, address, port, encrypted=parsed.scheme == 'https')
            path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
            if parsed.query:
                path += '?' + quote(parsed.query, safe="%=&;/:?@!$'()*+,-._~")
            client.request('GET', path, headers={'Host': host, 'User-Agent': 'MLAI link check',
                'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = client.getresponse()
            if response.status in REDIRECTS:
                location = response.getheader('Location')
                if not location or hop == policy.value('CRM_LINK_MAX_REDIRECTS'):
                    return Observation(url, current, response.status, 'redirect_limit')
                current = urljoin(current, location)
                continue
            return Observation(url, current, response.status, 'unreachable' if response.status >= 400 else '')
        except ValueError as exc:
            return Observation(url, current, None, str(exc))
        except (OSError, http.client.HTTPException):
            return Observation(url, current, None, 'unreachable')
        finally:
            if response is not None:
                response.close()
            if client is not None:
                client.close()
    return Observation(url, current, None, 'redirect_limit')


def inspect_links(body):
    urls = list(dict.fromkeys(url for url, _ in link_targets(body)))[:policy.value('CRM_LINKS_BLOCK')]
    with ThreadPoolExecutor(max_workers=policy.value('CRM_LINK_WORKERS')) as pool:
        return list(pool.map(inspect, urls))
