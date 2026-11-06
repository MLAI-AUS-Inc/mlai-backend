#!/usr/bin/env python3
"""Verify the E1 producer example at an immutable Content Factory commit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

if __package__:
    from .check_failure_contract import ContractCheckError, SOURCE_REPOSITORY, fetch_source
else:
    from check_failure_contract import ContractCheckError, SOURCE_REPOSITORY, fetch_source

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = 'contracts/site-fingerprint-v1.example.json'
SOURCE_PATH = 'backend/' + EXAMPLE


def verify(root=ROOT, *, remote=False, token=None, fetch=fetch_source):
    """Check local bytes and, in CI, the actual pinned private producer bytes."""
    lock = json.loads((root / 'contracts/site-fingerprint-v1.lock.json').read_text())
    keys = {'schema_version', 'source_repository', 'source_revision', 'source_path', 'sha256'}
    if (not isinstance(lock, dict) or set(lock) != keys
            or type(lock.get('schema_version')) is not int or lock['schema_version'] != 1
            or lock.get('source_repository') != SOURCE_REPOSITORY or lock.get('source_path') != SOURCE_PATH
            or not isinstance(lock.get('source_revision'), str)
            or not re.fullmatch(r'[a-f0-9]{40}', lock['source_revision'])
            or not isinstance(lock.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', lock['sha256'])):
        raise ContractCheckError('invalid_site_fingerprint_source_lock')
    raw = (root / EXAMPLE).read_bytes()
    if hashlib.sha256(raw).hexdigest() != lock['sha256']:
        raise ContractCheckError('site_fingerprint_local_digest_mismatch')
    if remote:
        if not token:
            raise ContractCheckError('CONTENT_FACTORY_CONTRACT_READ_TOKEN_required_for_pinned_private_source')
        if fetch(SOURCE_PATH, lock['source_revision'], token) != raw:
            raise ContractCheckError('site_fingerprint_pinned_source_byte_mismatch')
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote', action='store_true')
    args = parser.parse_args()
    try:
        verify(remote=args.remote, token=os.getenv('CONTENT_FACTORY_CONTRACT_READ_TOKEN'))
    except (ContractCheckError, OSError, ValueError):
        # Diagnostics must not include credential material or remote response data.
        print('Site fingerprint contract check failed; verify the immutable source lock and read-only access.')
        return 1
    print('Site fingerprint: pinned Content Factory bytes verified.' if args.remote
          else 'Site fingerprint: local bytes verified; remote provenance was not rechecked.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
