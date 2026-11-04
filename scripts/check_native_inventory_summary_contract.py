#!/usr/bin/env python3
"""Check the display summary schema/example at their exact private producer commit."""
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
NAMES = {f"native-inventory-summary-v1.{suffix}.json" for suffix in ("schema", "example")}


def verify(root=ROOT, *, remote=False, token=None, fetch=fetch_source):
    """Reject mutable refs, extra paths and mismatched local/remote contract bytes."""
    lock = json.loads((root / "contracts/native-inventory-summary-v1.lock.json").read_text())
    if (not isinstance(lock, dict) or set(lock) != {"schema_version", "source_repository", "source_revision", "files"}
            or type(lock["schema_version"]) is not int or lock["schema_version"] != 1 or lock["source_repository"] != SOURCE_REPOSITORY
            or not isinstance(lock["source_revision"], str) or re.fullmatch(r"[a-f0-9]{40}", lock["source_revision"]) is None
            or not isinstance(lock["files"], list) or len(lock["files"]) != len(NAMES)):
        raise ContractCheckError("invalid_native_inventory_summary_lock")
    if remote and not token:
        raise ContractCheckError("CONTENT_FACTORY_CONTRACT_READ_TOKEN_required_for_pinned_private_source")
    seen = set()
    for row in lock["files"]:
        if (not isinstance(row, dict) or set(row) != {"source_path", "local_path", "sha256"}
                or not isinstance(row["local_path"], str)
                or row["local_path"] not in {"contracts/" + name for name in NAMES}
                or row["source_path"] != "backend/" + row["local_path"] or row["local_path"] in seen
                or not isinstance(row["sha256"], str) or re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) is None):
            raise ContractCheckError("invalid_native_inventory_summary_file")
        seen.add(row["local_path"])
        raw = (root / row["local_path"]).read_bytes()
        if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ContractCheckError("native_inventory_summary_local_bytes_changed")
        if remote and fetch(row["source_path"], lock["source_revision"], token) != raw:
            raise ContractCheckError("native_inventory_summary_producer_bytes_changed")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    try:
        verify(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
    except (ContractCheckError, OSError, ValueError, TypeError):
        print("Native inventory summary contract check failed; verify its immutable source lock and read-only access.")
        return 1
    print("Native inventory summary: pinned producer bytes verified." if args.remote else "Native inventory summary: local bytes verified; remote provenance was not rechecked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
