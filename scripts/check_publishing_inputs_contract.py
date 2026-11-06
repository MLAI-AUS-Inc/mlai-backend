#!/usr/bin/env python3
"""Check the closed candidate schema and actual CF fixtures at one immutable SHA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

if __package__:
    from .check_failure_contract import ContractCheckError, SOURCE_REPOSITORY, fetch_source
else:
    from check_failure_contract import ContractCheckError, SOURCE_REPOSITORY, fetch_source

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {
    "contracts/publishing-inputs-v1.schema.json": "contracts/publishing-inputs-v1.schema.json",
    **{f"content_factory/testdata/publishing-inputs-v1/{name}.json":
       f"backend/tests/fixtures/publishing-inputs-v1/{name}.json"
       for name in ("react-generated", "static-generated")},
}


def verify(root=ROOT, *, remote=False, token=None, fetch=fetch_source):
    """Require fixed local paths, full SHA and exact private-producer bytes."""
    lock = json.loads((root / "contracts/publishing-inputs-v1.lock.json").read_text())
    if (not isinstance(lock, dict) or set(lock) != {"schema_version", "source_repository", "source_revision", "artifacts"}
            or type(lock["schema_version"]) is not int or lock["schema_version"] != 1
            or lock["source_repository"] != SOURCE_REPOSITORY
            or not isinstance(lock["source_revision"], str) or not re.fullmatch(r"[a-f0-9]{40}", lock["source_revision"])
            or not isinstance(lock["artifacts"], dict) or set(lock["artifacts"]) != set(ARTIFACTS)):
        raise ContractCheckError("invalid_publishing_inputs_lock")
    for path, source in ARTIFACTS.items():
        record = lock["artifacts"][path]
        if (not isinstance(record, dict) or set(record) != {"source_path", "sha256"} or record["source_path"] != source
                or not isinstance(record["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
            raise ContractCheckError("invalid_publishing_inputs_artifact")
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ContractCheckError("publishing_inputs_local_bytes_changed")
        if remote:
            if not token:
                raise ContractCheckError("publishing_inputs_private_source_token_required")
            if fetch(source, lock["source_revision"], token) != raw:
                raise ContractCheckError("publishing_inputs_private_source_bytes_changed")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    try:
        verify(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
    except (ContractCheckError, OSError, TypeError, ValueError):
        print("Publishing-input contract check failed; check its immutable source pin and read-only access.")
        return 1
    print("Publishing inputs: pinned private producer bytes verified." if args.remote
          else "Publishing inputs: local bytes verified; remote provenance was not rechecked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
