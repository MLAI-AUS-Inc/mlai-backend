"""Read-only IMAP placement and receiver-authentication evidence for a probe token."""

import imaplib
import os
import re
import ssl
from email import message_from_bytes, policy as email_policy
from email.utils import parseaddr

from .. import policy

_FOLDER = re.compile(rb'^\(([^)]*)\) (?:NIL|"(?:\\.|[^"])*") (.+)$')


def folder_list(lines):
    """Use server-advertised special-use flags, including localized junk folders."""
    found = [(b'INBOX', 'inbox')]
    for line in lines or ():
        if not isinstance(line, bytes):
            continue
        match = _FOLDER.match(line)
        if not match or b'\\noselect' in match[1].lower() or b'\r' in match[2] or b'\n' in match[2]:
            continue
        flags, name = match[1].lower(), match[2]
        if b'\\junk' in flags:
            found.append((name, 'spam'))
        elif b'\\all' in flags:
            found.append((name, 'missing'))
        elif name.strip(b'"').lower() in (b'junk', b'junk email', b'spam'):
            found.append((name, 'spam'))
    return list(dict.fromkeys(found))


def header_result(raw, *, provider, folder, stream_domain, now):
    """Only receiver-added authentication and an aligned signature count as proof."""
    message = message_from_bytes(raw, policy=email_policy.default)
    headers = [str(value) for value in message.get_all('Authentication-Results', [])]
    header = headers[0] if headers else ''
    authority = header.split(';', 1)[0].strip().lower()
    suffixes = {'gmail': ('google.com',), 'google_workspace': ('google.com',),
        'outlook': ('outlook.com',), 'microsoft365': ('outlook.com',),
        'yahoo': ('yahoo.com', 'yahoo.net'), 'icloud': ('icloud.com', 'apple.com')}.get(provider, ())
    trusted = any(authority == suffix or authority.endswith('.' + suffix) for suffix in suffixes)
    if provider in ('gmail', 'google_workspace'):
        trusted = authority == 'mx.google.com'
    # Exchange Online's documented format can omit the authserv-id. Trust that
    # format only on a configured Microsoft inbox, never an arbitrary mailbox.
    trusted |= provider in ('outlook', 'microsoft365') and bool(re.match(r'^(spf|dkim|dmarc)\s*=', authority))
    if not trusted:
        header = ''
    auth = {name: (match[1].lower() if (match := re.search(r'(?:^|[;\s])' + name + r'\s*=\s*([a-z]+)', header, re.I)) else 'unknown')
        for name in ('spf', 'dkim', 'dmarc')}
    signatures = []
    for signature in message.get_all('DKIM-Signature', []):
        parts = {key.strip().lower(): value.strip() for fragment in str(signature).split(';')
            if '=' in fragment for key, value in [fragment.split('=', 1)]}
        if parts.get('d', '').lower() == stream_domain.lower():
            names = {value.strip().lower() for value in parts.get('h', '').split(':') if value.strip()}
            signatures.append((parts.get('s', ''), names))
    passed = []
    for clause in re.findall(r'(?:^|;)\s*dkim\s*=\s*pass\b([^;]*)', header, re.I):
        identity = re.search(r'header\.(?:d|i)\s*=\s*([^\s;]+)', clause, re.I)
        selector = re.search(r'header\.s\s*=\s*([^\s;]+)', clause, re.I)
        if identity and identity[1].strip('"').rpartition('@')[2].lower() == stream_domain.lower():
            passed.append(selector[1].strip('"') if selector else None)
    valid_signatures = [names for selector, names in signatures if selector in passed or (None in passed and len(signatures) == 1)]
    signed = set().union(*valid_signatures) if valid_signatures else set()
    if valid_signatures:
        auth['dkim'] = 'pass'
    elif auth['dkim'] == 'pass':
        auth['dkim'] = 'unknown'
    unsubscribe = str(message.get('List-Unsubscribe') or '')
    one_click = bool(re.fullmatch(r'list-unsubscribe\s*=\s*one-click', str(message.get('List-Unsubscribe-Post') or '').strip(), re.I))
    return {'provider': provider, 'folder': folder, **auth, 'list_unsubscribe': bool(re.search(r'<https://[^<>\s]+>', unsubscribe, re.I)),
        'list_unsubscribe_post': one_click, 'dkim_signed_headers': sorted(signed),
        'one_click_signed': any({'list-unsubscribe', 'list-unsubscribe-post'} <= names for names in valid_signatures),
        'feedback_id_signed': any('feedback-id' in names for names in valid_signatures), 'arrived_at': now.isoformat()}


def placement(client, uid, metadata, fallback, *, gmail):
    if fallback == 'spam' or re.search(rb'\\(?:Spam|Junk)\b', metadata, re.I):
        return 'spam'
    if fallback == 'inbox' or re.search(rb'\\Inbox\b', metadata, re.I):
        if gmail:
            # Gmail documents X-GM-RAW as web search syntax. Category labels
            # are not assumed to appear in X-GM-LABELS on every IMAP server.
            for category in ('promotions', 'updates'):
                status, rows = client.uid('SEARCH', None, 'UID', uid, 'X-GM-RAW', f'"category:{category}"')
                if status == 'OK' and any(uid.encode() in (row or b'').split() for row in rows):
                    return category
        return 'inbox'
    return fallback


def read_mailbox(mailbox, probe, *, now, token=None):
    """Never marks a message read, moves it, or changes mailbox contents."""
    if not re.fullmatch(r'CRM_SEED_[A-Z0-9_]+', mailbox.secret_env):
        return {'provider': mailbox.provider, 'folder': 'missing', 'error': 'credential_reference_invalid'}
    credential = token if token is not None else os.environ.get(mailbox.secret_env, '')
    if not credential:
        return {'provider': mailbox.provider, 'folder': 'missing', 'error': 'credential_missing'}
    client = None
    try:
        client = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, ssl_context=ssl.create_default_context(),
            timeout=policy.value('CRM_SEED_IMAP_TIMEOUT_SECONDS'))
        if mailbox.auth == 'xoauth2':
            client.authenticate('XOAUTH2', lambda _: f'user={mailbox.address}\x01auth=Bearer {credential}\x01\x01'.encode())
        else:
            client.login(mailbox.address, credential)
        status, folders = client.list()
        if status != 'OK':
            return {'provider': mailbox.provider, 'folder': 'missing', 'error': 'folders_unavailable'}
        gmail = any(value in (b'X-GM-EXT-1', 'X-GM-EXT-1') for value in client.capabilities)
        for folder, fallback in folder_list(folders):
            status, _ = client.select(folder, readonly=True)
            if status != 'OK':
                continue
            status, data = client.uid('SEARCH', None, 'HEADER', 'X-MLAI-Seed', '"' + str(probe.token) + '"')
            if status != 'OK':
                continue
            ids = b' '.join(part for part in data if isinstance(part, bytes)).split()
            for raw_uid in ids[-policy.value('CRM_SEED_MAX_MATCHES'):]:
                if not raw_uid.isdigit():
                    continue
                uid = raw_uid.decode('ascii')
                fetch = f'(BODY.PEEK[HEADER]<0.{policy.value("CRM_SEED_MAX_MESSAGE_BYTES") + 1}>' + (' X-GM-LABELS)' if gmail else ')')
                status, records = client.uid('FETCH', uid, fetch)
                if status != 'OK':
                    continue
                for record in records:
                    if not isinstance(record, tuple) or len(record) != 2:
                        continue
                    metadata, raw = record
                    if not isinstance(raw, bytes) or len(raw) > policy.value('CRM_SEED_MAX_MESSAGE_BYTES'):
                        continue
                    message = message_from_bytes(raw, policy=email_policy.default)
                    if str(message.get('X-MLAI-Seed') or '').strip() != str(probe.token):
                        continue
                    expected = ((getattr(probe, 'results', None) or {}).get('message') or {}).get('identity', {}).get('email') or probe.stream.mailbox.email
                    if parseaddr(str(message.get('From') or ''))[1].lower() != expected.lower():
                        continue
                    return header_result(raw, provider=mailbox.provider,
                        folder=placement(client, uid, metadata, fallback, gmail=gmail), stream_domain=probe.stream.domain, now=now)
        return {'provider': mailbox.provider, 'folder': 'missing'}
    except (OSError, imaplib.IMAP4.error, ValueError):
        return {'provider': mailbox.provider, 'folder': 'missing', 'error': 'inbox_unreachable'}
    finally:
        if client is not None:
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def grade(results, *, timed_out=False):
    """Missing probes warn; observed identity/one-click failures always block."""
    observed = [row for row in results if row.get('arrived_at')]
    for row in observed:
        signed = set(row.get('dkim_signed_headers') or ())
        if (any(row.get(key) != 'pass' for key in ('spf', 'dkim', 'dmarc'))
                or not row.get('list_unsubscribe') or not row.get('list_unsubscribe_post')
                or not {'list-unsubscribe', 'list-unsubscribe-post'} <= signed or not row.get('one_click_signed')):
            return 'block', 'identity_or_unsubscribe'
    spam = {('google' if row['provider'] in ('gmail', 'google_workspace') else 'microsoft' if row['provider'] in ('outlook', 'microsoft365') else row['provider'])
        for row in observed if row.get('folder') == 'spam'}
    if len(spam) >= policy.value('CRM_SEED_SPAM_BLOCK_PROVIDERS'):
        return 'block', 'multiple_spam_folders'
    if spam:
        return 'warn', 'one_spam_folder'
    if timed_out or any(row.get('folder') == 'missing' for row in results):
        return 'warn', 'incomplete_check'
    coverage = {('google' if row['provider'] in ('gmail', 'google_workspace') else 'microsoft' if row['provider'] in ('outlook', 'microsoft365') else row['provider'])
        for row in observed if row.get('folder') in ('inbox', 'promotions', 'updates')}
    if not {'google', 'microsoft', 'yahoo'} <= coverage:
        return 'warn', 'coverage_incomplete'
    if any(not row.get('feedback_id_signed') for row in observed):
        return 'warn', 'feedback_header_unsigned'
    return 'pass', 'inboxes_reached'
