"""Verify native-v4 artifacts and actual producer bytes at one private CF SHA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

if __package__:
    from .check_failure_contract import ContractCheckError,SOURCE_REPOSITORY,fetch_source
else:
    from check_failure_contract import ContractCheckError,SOURCE_REPOSITORY,fetch_source

ROOT=Path(__file__).resolve().parents[1]
LOCK="contracts/native-publisher-v4.lock.json"
ARTIFACTS={"content_factory/native_publisher_contract_v4.py":"backend/native_publisher_contract_v4.py",
    "contracts/native-publisher-v4.schema.json":"contracts/native-publisher-v4.schema.json",
    "content_factory/testdata/native-publisher-v4/synthetic.json":"backend/tests/fixtures/native-publisher-v4/synthetic.json"}
PRODUCERS={"backend/harness/native_publisher_worker.py","backend/harness/original_publisher_pipeline_worker.py","scripts/fixtures/native-publisher-runtime.Dockerfile"}


def _hex(value,length):return type(value) is str and bool(re.fullmatch(r"[a-f0-9]{"+str(length)+r"}",value))


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ContractCheckError("native_v4_duplicate_metadata")
        result[key]=value
    return result


def verify(root=ROOT,*,remote=False,token=None,fetch=fetch_source):
    """Read immutable code provenance; do not claim native/image/Ready proof."""
    lock=json.loads((root/LOCK).read_text(),object_pairs_hook=_unique)
    if (type(lock) is not dict or set(lock)!={"schema_version","source_repository","source_revision","artifacts","producers"}
            or type(lock["schema_version"]) is not int or lock["schema_version"]!=1 or lock["source_repository"]!=SOURCE_REPOSITORY
            or not _hex(lock["source_revision"],40) or type(lock["artifacts"]) is not dict or set(lock["artifacts"])!=set(ARTIFACTS)
            or type(lock["producers"]) is not dict or set(lock["producers"])!=PRODUCERS):
        raise ContractCheckError("native_v4_invalid_lock")
    if remote and (type(token) is not str or not token.strip()):raise ContractCheckError("native_v4_private_source_required")
    for local,source in ARTIFACTS.items():
        record=lock["artifacts"][local]
        if type(record) is not dict or set(record)!={"source_path","sha256"} or record["source_path"]!=source or not _hex(record["sha256"],64):
            raise ContractCheckError("native_v4_invalid_artifact")
        raw=(root/local).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=record["sha256"]:raise ContractCheckError("native_v4_local_bytes_changed")
        if remote and fetch(source,lock["source_revision"],token)!=raw:raise ContractCheckError("native_v4_private_bytes_changed")
    for source,record in lock["producers"].items():
        if type(record) is not dict or set(record)!={"sha256"} or not _hex(record["sha256"],64):raise ContractCheckError("native_v4_invalid_producer")
        if remote:
            raw=fetch(source,lock["source_revision"],token)
            if type(raw) is not bytes or hashlib.sha256(raw).hexdigest()!=record["sha256"]:raise ContractCheckError("native_v4_private_producer_changed")
    return lock


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--remote",action="store_true");args=parser.parse_args()
    try:verify(remote=args.remote,token=os.getenv("CONTENT_FACTORY_CONTRACT_READ_TOKEN"))
    except Exception:
        print("Native-v4 source check failed; verify the immutable pin and read-only access.");return 1
    print("Native-v4 private producer bytes verified." if args.remote else "Native-v4 local bytes verified; remote provenance not rechecked.")
    return 0


if __name__=="__main__":raise SystemExit(main())
