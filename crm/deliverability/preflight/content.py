"""Deterministic message checks with staff copy separate from owner diagnostics."""

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from crm import merge_tags

from .. import policy
from ..hygiene import DATA
from . import links

PHRASES = tuple(line.strip() for line in (DATA / 'spam_phrases.txt').read_text().splitlines() if line.strip())
SYMBOLS = json.loads((DATA / 'rspamd_symbols.json').read_text())
PREFIX = re.compile(r'^\s*(?:re|fw|fwd)\s*:\s*', re.I)
EMOJI = re.compile(r'[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF](?:\ufe0f|[\U0001F3FB-\U0001F3FF])?(?:\u200d[\U0001F300-\U0001FAFF](?:\ufe0f|[\U0001F3FB-\U0001F3FF])?)*')


def item(key, status, title, detail='', *, group='message', owner_detail=None, fix=None):
    result = {'key': key, 'group': group, 'status': status, 'title': title, 'detail': detail,
        'owner_detail': owner_detail or {}}
    if fix:
        result['fix'] = {'kind': fix[0], 'label': fix[1]}
    return result


def plain_text(body):
    soup = BeautifulSoup('<section>' + (body or '') + '</section>', 'html.parser')
    for node in soup.find_all(('script', 'style')):
        node.decompose()
    text = re.sub(r'!?(\[([^\]]*)\])\([^)]*\)', r'\2', soup.get_text(' '))
    return re.sub(r'\s+', ' ', re.sub(r'[#*_`>]', '', text)).strip()


def message_checks(blast, *, html, template, observations, spam):
    subject, body = blast.subject.strip(), blast.body_markdown or ''
    words = re.findall(r'\b\w+\b', plain_text(body))
    targets = links.link_targets(body)
    urls = set(url for url, _ in targets)
    items = [item('subject_present', 'pass' if subject else 'block', 'Subject',
        'Add a subject so people know what this email is about.' if not subject else 'Ready.', fix=('focus_subject', 'Show me') if not subject else None)]
    short_or_long = not policy.value('CRM_SUBJECT_MIN_LENGTH') <= len(subject) <= policy.value('CRM_SUBJECT_MAX_LENGTH')
    items.append(item('subject_length', 'warn' if short_or_long else 'pass', 'Subject length',
        'A short, clear subject usually works best.' if short_or_long else 'Easy to read.', fix=('focus_subject', 'Show me') if short_or_long else None))
    allowed = {word.upper() for word in policy.value('CRM_ALLOWED_ACRONYMS')}
    lexical = re.findall(r'\b[^\W\d_]+\b', subject)
    checked = [word for word in lexical if word not in allowed]
    letters = ''.join(checked)
    caps = (bool(letters) and sum(char.isupper() for char in letters) / len(letters) > policy.value('CRM_SUBJECT_CAPS_RATE')) or any(len(word) >= 4 and word.isupper() for word in checked)
    items.append(item('subject_caps', 'warn' if caps else 'pass', 'Capital letters', 'Try sentence case.' if caps else 'Looks natural.'))
    trick = bool(PREFIX.match(subject))
    items.append(item('subject_tricks', 'block' if trick else 'pass', 'Honest subject',
        'Remove the reply or forward prefix so this reads as a new email.' if trick else 'Reads as a new email.',
        fix=('remove_subject_prefix', 'Remove the prefix') if trick else None))
    punctuation = any(value in subject for value in ('!!', '??', '$$')) or len(EMOJI.findall(subject)) > 1
    items.append(item('subject_punctuation', 'warn' if punctuation else 'pass', 'Punctuation', 'Try fewer symbols or emojis.' if punctuation else 'Easy to scan.'))
    preview = bool(blast.preview_text.strip())
    items.append(item('preview_text', 'pass' if preview else 'warn', 'Inbox preview',
        'Ready.' if preview else 'Add the line people see beside the subject.', fix=None if preview else ('add_preview_text', 'Use the first sentence')))
    length = policy.value('CRM_BODY_MIN_WORDS') <= len(words) <= policy.value('CRM_BODY_MAX_WORDS')
    items.append(item('body_length', 'pass' if length else 'warn', 'Message length', f'{len(words):,} words.'))
    size = len(html.encode('utf-8')) if html is not None else None
    size_status = 'block' if size is None or size > policy.value('CRM_HTML_BLOCK_KB') * 1024 else 'warn' if size > policy.value('CRM_HTML_WARN_KB') * 1024 else 'pass'
    items.append(item('html_size', size_status, 'Email size',
        "We couldn't prepare the email yet." if size is None else 'This email may hide its ending. Shorten it before sending.' if size_status == 'block' else 'Keep it compact so people see the whole email.' if size_status == 'warn' else 'The whole email should fit.', owner_detail={'bytes': size}))
    n = len(urls)
    items.append(item('links_count', 'block' if n > policy.value('CRM_LINKS_BLOCK') else 'warn' if n > policy.value('CRM_LINKS_WARN') else 'pass',
        'Number of links', f'{n} different links.'))
    short, insecure, unsafe, mismatch = set(), set(), set(), set()
    for url, label in targets:
        try:
            parsed, host = links.parsed_url(url)
            if host in links.SHORTENERS or any(host.endswith('.' + domain) for domain in links.SHORTENERS):
                short.add(host)
            if parsed.scheme == 'http':
                insecure.add(host)
            if re.match(r'^(https?://|www\.)', label.strip(), re.I):
                visible = urlsplit(label if '://' in label else 'https://' + label).hostname
                if visible and visible.lower().removeprefix('www.') != host.removeprefix('www.'):
                    mismatch.add(host)
        except ValueError:
            unsafe.add('invalid_destination')
    unsafe.update(row.error for row in observations if row.error in ('ip_address_link', 'nonpublic_link', 'invalid_link'))
    items.append(item('link_shorteners', 'block' if short else 'pass', 'Clear link destinations',
        'Use full web addresses so people can see where they go.' if short else 'Links show where they go.',
        owner_detail={'domains': sorted(short)}, fix=('expand_short_links', 'Expand the links') if short else None))
    items.append(item('link_insecure', 'warn' if insecure else 'pass', 'Secure links',
        'Some links use an older connection. Check whether a secure version works.' if insecure else 'Links use secure connections.',
        owner_detail={'domains': sorted(insecure)}, fix=('upgrade_https', 'Try secure links') if insecure else None))
    items.append(item('link_ip', 'block' if unsafe else 'pass', 'Public web addresses',
        'Use a public website address for every link.' if unsafe else 'Links use public website addresses.', owner_detail={'reasons': sorted(unsafe)}))
    unreachable = sum(bool(row.error) for row in observations)
    items.append(item('link_reachable', 'warn' if unreachable else 'pass', 'Links open',
        f'{unreachable} links need a look.' if unreachable else 'The checked links open.', owner_detail={'checked': len(observations)},
        fix=('focus_links', 'Show me') if unreachable else None))
    items.append(item('link_text_mismatch', 'warn' if mismatch else 'pass', 'Link wording',
        'A visible web address goes somewhere different. Use a clear label.' if mismatch else 'Link labels match their destinations.', owner_detail={'domains': sorted(mismatch)}))
    soup = BeautifulSoup('<section>' + body + '</section>', 'html.parser')
    image_alts = [str(image.get('alt') or '').strip() for image in soup.find_all('img')]
    image_alts.extend(re.findall(r'!\[([^\]]*)\]\(', body))
    image_warning = bool(image_alts) and (any(not alt for alt in image_alts) or len(words) < policy.value('CRM_BODY_MIN_WORDS'))
    items.append(item('images', 'warn' if image_warning else 'pass', 'Image descriptions',
        'Add useful descriptions and enough message text for people who cannot see the images.' if image_warning else 'Ready.'))
    found = [phrase for phrase in PHRASES if re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', plain_text(subject + ' ' + body), re.I)]
    items.append(item('spam_phrases', 'warn' if found else 'pass', 'Natural wording',
        'Consider plainer wording: ' + ', '.join(found) + '.' if found else 'Looks natural.'))
    score = spam.get('score') if isinstance(spam, dict) else None
    status = 'block' if score is None else 'block' if score >= policy.value('CRM_SPAM_BLOCK_SCORE') else 'warn' if score >= policy.value('CRM_SPAM_WARN_SCORE') else 'pass'
    explanations = [SYMBOLS.get(name, 'One part of the message needs a closer look.') for name in (spam or {}).get('top_symbols', [])[:3]]
    items.append(item('spam_score', status, 'Message quality',
        "We couldn't finish this check yet. Please try again." if score is None else
        (' '.join(explanations) or 'Review the message before sending.') if status != 'pass' else 'Ready.', owner_detail=spam))
    valid = True
    try:
        for text in (subject, body, blast.preview_text, blast.sign_off_name):
            merge_tags.validate(text)
    except merge_tags.UnsupportedMergeTag:
        valid = False
    items.append(item('merge_tags', 'pass' if valid else 'block', 'Personal details',
        'Ready.' if valid else 'A personal detail placeholder needs fixing.', fix=None if valid else ('focus_message', 'Show me')))
    footer = bool(template and re.search(r'{{\s*UnsubscribeURL\s*}}', template)
        and 'mlai' in template.lower() and policy.value('CRM_FOOTER_CONTACT_ADDRESS').lower() in template.lower()
        and re.search(r'{{\s*\.Subscriber\.Attribs\.consent_reason\s*}}', template))
    items.append(item('footer', 'pass' if footer else 'block', 'Sender details and email preferences',
        'Ready.' if footer else 'The email team needs to fix the standard email ending.', owner_detail={'template_needs_fixing': not footer}))
    return items
