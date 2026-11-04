"""Versioned read-only site facts; these never grant write or live authority."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

FIELDS = frozenset({'framework', 'adapter', 'router', 'app_roots', 'runtime',
    'package_manager', 'lockfiles', 'build', 'hosting', 'blog', 'styling', 'routing',
    'private_build_inputs', 'sitemap', 'robots'})
PROVIDERS = frozenset({'vercel', 'netlify', 'cloudflare', 'github_pages'})
SHA = re.compile(r'^[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?$')
DIGEST = re.compile(r'^[a-f0-9]{64}$')


def enabled():
    """Fingerprint consumers have a separate default-off rollout."""
    from django.conf import settings
    return getattr(settings, 'ARTICLES_SITE_FINGERPRINT_ENABLED', False)


def _bounded(value, *, depth=0, remaining=None):
    remaining = [2500] if remaining is None else remaining
    remaining[0] -= 1
    if depth > 8 or remaining[0] < 0:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return abs(value) <= 10**12
    if type(value) is float:
        return math.isfinite(value) and abs(value) <= 10**12
    if isinstance(value, str):
        return len(value) <= 1000 and not any(ord(char) < 32 for char in value)
    if isinstance(value, list):
        return len(value) <= 64 and all(_bounded(item, depth=depth + 1, remaining=remaining) for item in value)
    if isinstance(value, dict):
        return len(value) <= 64 and all(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', key)
            and key.casefold() not in {'token', 'access_token', 'password', 'private_key', 'secret', 'authorization'}
            and _bounded(item, depth=depth + 1, remaining=remaining) for key, item in value.items())
    return False


def validate(value, *, github_repo, source_sha, branch, app_root):
    """Reject future, changed, unbounded or foreign facts without guessing."""
    keys = {'schema_version', 'github_repo', 'branch', 'app_root', 'source_sha', 'fields',
        'capture_status', 'issues', 'public_status', 'captured_at', 'digest'}
    if (not isinstance(github_repo, str) or len(github_repo) > 255
        or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', github_repo)
        or any(part in {'.', '..'} for part in github_repo.split('/'))
        or not isinstance(branch, str) or not 0 < len(branch) <= 255
        or any(ord(char) < 33 or char in '\\?*:[]^~' for char in branch)
        or any(part in branch for part in ('..', '//', '@{')) or branch.startswith('/') or branch.endswith('/')
        or not isinstance(app_root, str) or len(app_root) > 500
        or app_root and (app_root.startswith('/') or '\\' in app_root
            or any(part in {'', '.', '..'} for part in app_root.split('/'))
            or any(ord(char) < 32 for char in app_root))
        or not isinstance(value, dict) or set(value) != keys or type(value.get('schema_version')) is not int
        or value['schema_version'] != 1 or not isinstance(source_sha, str) or not SHA.fullmatch(source_sha)
        or value.get('source_sha') != source_sha or value.get('github_repo') != github_repo
        or value.get('branch') != branch or value.get('app_root') != app_root
        or not isinstance(value.get('digest'), str) or not DIGEST.fullmatch(value['digest'])
        or not isinstance(value.get('capture_status'), str) or value['capture_status'] not in {'complete', 'partial'}
        or not isinstance(value.get('public_status'), str) or value['public_status'] not in {'complete', 'partial', 'not_observed', 'not_public', 'unavailable'}
        or not isinstance(value.get('issues'), list) or len(value['issues']) > 32 or not _bounded(value)):
        return None
    fields = value.get('fields')
    if not isinstance(fields, dict) or set(fields) != FIELDS:
        return None
    for field in fields.values():
        if (not isinstance(field, dict) or set(field) != {'value', 'confidence', 'evidence'}
            or not isinstance(field.get('confidence'), str) or field['confidence'] not in {'high', 'medium', 'unknown'}
            or not isinstance(field.get('evidence'), list) or len(field['evidence']) > 16
            or any(not isinstance(item, str) or not 0 < len(item) <= 500 for item in field['evidence'])):
            return None
    try:
        captured = datetime.fromisoformat(value['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None or captured > datetime.now(timezone.utc) + timedelta(minutes=5):
            return None
        payload = {key: item for key, item in value.items() if key not in {'digest', 'captured_at'}}
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(encoded) > 32768 or hashlib.sha256(encoded).hexdigest() != value['digest']:
            return None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None
    return deepcopy(value)


def project(value, **scope):
    """Expose validated planning evidence only when its consumer rollout is on."""
    return validate(value, **scope) if enabled() else None


def framework_hint(value):
    """Use confident detector names, never a model label or a ready flag."""
    field = value['fields']['framework']
    name = field.get('value')
    return name if field.get('confidence') == 'high' and isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) else None


def hosting_hint(value):
    """A provider hint cannot establish automatic deployment or live evidence."""
    field = value['fields']['hosting']
    hint = field.get('value')
    if (not isinstance(hint, dict) or field.get('confidence') not in {'high', 'medium'}
        or not isinstance(hint.get('provider'), str) or hint['provider'] not in PROVIDERS or hint.get('conflict') is not False
        or hint.get('candidates') != [hint['provider']]):
        return None
    return hint['provider']


def for_connection(connection, source):
    """Read one current-generation snapshot; never inherit an older observation."""
    if not enabled():
        return None
    snapshot = connection.scan_snapshots.filter(generation=connection.generation,
        source_sha=source).order_by('-created_at').first()
    evidence = snapshot.evidence if snapshot and isinstance(snapshot.evidence, dict) else {}
    inventory = evidence.get('repository_inventory')
    inventory = inventory if isinstance(inventory, dict) else {}
    return project(inventory.get('site_fingerprint'), github_repo=connection.github_repo,
        source_sha=source, branch=connection.branch, app_root=connection.app_root)
