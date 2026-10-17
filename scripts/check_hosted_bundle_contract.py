#!/usr/bin/env python3
"""Verify exact immutable CF hosted bundle validator, schema and fixture bytes."""
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
PAIRS = {'backend/hosted_bundle_contract.py': 'content_factory/hosted_bundle_contract.py', 'backend/contracts/hosted-static-bundle-v1.schema.json': 'contracts/hosted-static-bundle-v1.schema.json', 'backend/contracts/hosted-static-bundle-v1.example.json': 'contracts/hosted-static-bundle-v1.example.json'}


def verify(root=ROOT, *, remote=False, token=None, fetch=fetch_source):
    """Require exact bytes and one allowlisted producer path at an immutable commit."""
    lock = json.loads((root / "contracts/hosted-bundle-v1.lock.json").read_text())
    if (not isinstance(lock, dict) or set(lock) != {"schema_version", "source_repository", "source_revision", "files"}
            or type(lock["schema_version"]) is not int or lock["schema_version"] != 1
            or lock["source_repository"] != SOURCE_REPOSITORY or not isinstance(lock["source_revision"], str)
            or re.fullmatch(r"[a-f0-9]{40}", lock["source_revision"]) is None
            or not isinstance(lock["files"], list) or len(lock["files"]) != len(PAIRS)):
        raise ContractCheckError("invalid_hosted_bundle_lock")
    seen = set()
    for row in lock["files"]:
        if (not isinstance(row, dict) or set(row) != {"source_path", "local_path", "sha256"}
                or row.get("source_path") not in PAIRS or PAIRS[row["source_path"]] != row.get("local_path")
                or row["source_path"] in seen or not isinstance(row.get("sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) is None):
            raise ContractCheckError("invalid_hosted_bundle_file")
        seen.add(row["source_path"])
        raw = (root / row["local_path"]).read_bytes()
        if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ContractCheckError("hosted_bundle_local_bytes_changed")
        if remote and not token:
            raise ContractCheckError("CONTENT_FACTORY_CONTRACT_READ_TOKEN_required_for_pinned_private_source")
        if remote and fetch(row["source_path"], lock["source_revision"], token) != raw:
            raise ContractCheckError("hosted_bundle_producer_bytes_changed")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    try:
        verify(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
    except (ContractCheckError, OSError, ValueError, TypeError):
        print("Hosted bundle contract check failed; verify its immutable producer lock and read-only access.")
        return 1
    print("Hosted bundle contract: pinned producer bytes verified." if args.remote else "Hosted bundle contract: local bytes verified; remote provenance was not rechecked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
