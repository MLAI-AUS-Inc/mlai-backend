"""Verify exact publisher-v2 schema/kernel/fixture bytes at one private CF SHA."""
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
LOCK = "contracts/publisher-inputs-v2.lock.json"
PRODUCER = "backend/original_publisher_inputs.py"
ARTIFACTS = {
    "contracts/publishing-inputs-v2.schema.json": "contracts/publishing-inputs-v2.schema.json",
    "contracts/publisher-component-v1.schema.json": "contracts/publisher-component-v1.schema.json",
    "content_factory/publisher_contract_v2.py": "backend/publisher_contract_v2.py",
    "content_factory/testdata/publisher-inputs-v2/component.json": "backend/tests/fixtures/publisher-inputs-v2/component.json",
}


def _hex(value, length):
    return type(value) is str and bool(re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ContractCheckError("publisher_v2_duplicate_metadata")
        result[key] = value
    return result


def verify(root=ROOT, *, remote=False, token=None, fetch=fetch_source):
    """Verify fixed paths and the real producer, without accepting execution claims."""
    lock = json.loads((root / LOCK).read_text(), object_pairs_hook=_unique)
    if (type(lock) is not dict or set(lock) != {"schema_version", "source_repository", "source_revision", "artifacts", "producer"}
            or type(lock["schema_version"]) is not int or lock["schema_version"] != 1
            or lock["source_repository"] != SOURCE_REPOSITORY or not _hex(lock["source_revision"], 40)
            or type(lock["artifacts"]) is not dict or set(lock["artifacts"]) != set(ARTIFACTS)
            or type(lock["producer"]) is not dict or set(lock["producer"]) != {"source_path", "sha256"}
            or lock["producer"]["source_path"] != PRODUCER or not _hex(lock["producer"]["sha256"], 64)):
        raise ContractCheckError("publisher_v2_invalid_lock")
    for path, source in ARTIFACTS.items():
        record = lock["artifacts"][path]
        if (type(record) is not dict or set(record) != {"source_path", "sha256"}
                or record["source_path"] != source or not _hex(record["sha256"], 64)):
            raise ContractCheckError("publisher_v2_invalid_artifact")
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ContractCheckError("publisher_v2_local_bytes_changed")
        if remote:
            if type(token) is not str or not token.strip(): raise ContractCheckError("publisher_v2_private_source_required")
            if fetch(source, lock["source_revision"], token) != raw:
                raise ContractCheckError("publisher_v2_private_source_bytes_changed")
    if remote:
        raw = fetch(PRODUCER, lock["source_revision"], token)
        if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != lock["producer"]["sha256"]:
            raise ContractCheckError("publisher_v2_private_producer_changed")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    try:
        verify(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
    except Exception:
        print("Publisher-v2 source check failed; verify the immutable pin and read-only source access.")
        return 1
    print("Publisher-v2 private producer bytes verified." if args.remote else "Publisher-v2 local bytes verified; remote provenance not rechecked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
