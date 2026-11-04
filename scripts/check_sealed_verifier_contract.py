#!/usr/bin/env python3
"""Check hosted schema3 manifests, fixtures and executing runner at one CF SHA.

Offline validation checks only local bytes and closed metadata. CI must use
--remote to compare the private producer artifacts and the actual runner bytes;
a fixture checksum by itself does not establish execution or deployed evidence.
"""
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
LOCK = "contracts/native-verifier-v3.lock.json"
MANIFEST = "contracts/native-verifier-v3.json"
RUNNER = "backend/ci_templates/articles_verification.py"
ARTIFACTS = {
    MANIFEST: MANIFEST,
    "content_factory/testdata/sealed-verification-v3/static.json": "backend/tests/fixtures/native_evidence_v3_static.json",
    "content_factory/testdata/sealed-verification-v3/proposal.json": "backend/tests/fixtures/native_candidate_v3_static.json",
}


def _hex(value, length):
    return type(value) is str and bool(re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractCheckError("sealed_verifier_duplicate_metadata")
        result[key] = value
    return result


def _metadata(raw):
    value = json.loads(raw, object_pairs_hook=_object)
    if type(value) is not dict:
        raise ContractCheckError("sealed_verifier_invalid_metadata")
    return value


def _manifest(raw):
    value = _metadata(raw)
    if (set(value) != {"schema_version", "profile", "proof_schema_version", "runner_path", "runner_sha256", "python_version", "playwright_version"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["proof_schema_version"]) is not int or value["proof_schema_version"] != 3
            or value["profile"] != "native_ci_v1" or value["runner_path"] != RUNNER
            or value["python_version"] != "3.11" or value["playwright_version"] != "1.58.0"
            or not _hex(value["runner_sha256"], 64)):
        raise ContractCheckError("sealed_verifier_invalid_manifest")
    return value


def verify(root=ROOT, *, remote=False, token=None, fetch=fetch_source):
    """Verify fixed local/source paths and the actual runner at the same pin."""
    lock = _metadata((root / LOCK).read_bytes())
    if (set(lock) != {"schema_version", "source_repository", "source_revision", "artifacts", "runner"}
            or type(lock["schema_version"]) is not int or lock["schema_version"] != 1
            or lock["source_repository"] != SOURCE_REPOSITORY or not _hex(lock["source_revision"], 40)
            or type(lock["artifacts"]) is not dict or set(lock["artifacts"]) != set(ARTIFACTS)
            or type(lock["runner"]) is not dict or set(lock["runner"]) != {"source_path", "sha256"}
            or lock["runner"]["source_path"] != RUNNER or not _hex(lock["runner"]["sha256"], 64)):
        raise ContractCheckError("sealed_verifier_invalid_lock")
    local = {}
    for path, source in ARTIFACTS.items():
        record = lock["artifacts"][path]
        if (type(record) is not dict or set(record) != {"source_path", "sha256"}
                or record["source_path"] != source or not _hex(record["sha256"], 64)):
            raise ContractCheckError("sealed_verifier_invalid_artifact")
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ContractCheckError("sealed_verifier_local_bytes_changed")
        local[path] = raw
    manifest = _manifest(local[MANIFEST])
    if manifest["runner_sha256"] != lock["runner"]["sha256"]:
        raise ContractCheckError("sealed_verifier_runner_metadata_changed")
    if remote:
        if type(token) is not str or not token.strip():
            raise ContractCheckError("sealed_verifier_private_source_token_required")
        for path, source in ARTIFACTS.items():
            raw = fetch(source, lock["source_revision"], token)
            if type(raw) is not bytes or raw != local[path]:
                raise ContractCheckError("sealed_verifier_private_source_bytes_changed")
        runner = fetch(RUNNER, lock["source_revision"], token)
        if type(runner) is not bytes or hashlib.sha256(runner).hexdigest() != manifest["runner_sha256"]:
            raise ContractCheckError("sealed_verifier_private_runner_bytes_changed")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    try:
        verify(remote=args.remote, token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
    except Exception:
        # Provider errors and unexpected read failures never print credentials,
        # private source bodies or URLs. Failure is always a nonzero CI result.
        print("Sealed verifier contract check failed; check its immutable source pin and read-only access.")
        return 1
    print("Sealed verifier: pinned private artifacts and runner bytes verified." if args.remote
          else "Sealed verifier: local bytes verified; remote artifacts and runner were not rechecked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
