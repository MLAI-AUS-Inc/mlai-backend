"""Mocked producer wire fixtures; no browser/source/DB/provider authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from django.test import SimpleTestCase

from . import native_publisher_contract_v4 as protocol
from .website_native_publisher_v4 import validate_native_setup
from .website_publisher_inputs_v2 import wire_validate
from .website_publishing_inputs import PublishingInputsError

FIXTURE=Path(__file__).parent/"testdata/native-publisher-v4/synthetic.json"


def example():return json.loads(FIXTURE.read_text())


def received(value,original=None,**changes):
    original=original or example()
    return validate_native_setup(value,**{"candidate":original["candidate"],"records":original["records"],"identity":original["identity"],
        "supported_profile":original["profile"],"original_binding":original["original_binding"],"current_binding":original["current_binding"],"origin":"authenticated_hosted",**changes})


class NativePublisherV4ReaderTests(SimpleTestCase):
    def test_exact_producer_contract_fixture_parses_without_ready_or_publication_promotion(self):
        value=example();parsed=received(value["receipt"],value)
        self.assertEqual(parsed,value["receipt"])
        self.assertEqual(value["provenance"]["native_execution"],"mocked")
        self.assertTrue(parsed["native_verified"])
        self.assertTrue(all(parsed[key] is False for key in ("publication_authority","support_proven","live_goal_proof")))
        parsed["current"]["source_sha"]="d"*40
        self.assertNotEqual(parsed,value["receipt"])

    def test_vendored_schema_matches_the_source_reader_and_fixture(self):
        path=Path(__file__).resolve().parents[1]/"contracts/native-publisher-v4.schema.json"
        self.assertEqual(json.loads(path.read_text()),protocol.receipt_schema())
        wire_validate(example()["receipt"],protocol.receipt_schema())

    def test_current_and_original_reservations_cannot_be_missing_rebound_or_self_filled(self):
        value=example()
        for name in ("original_binding","current_binding"):
            for key in value[name]:
                for missing in (False,True):
                    binding=deepcopy(value[name])
                    if missing:binding.pop(key)
                    else:binding[key]=binding[key]+1 if type(binding[key]) is int else "changed-owned-context"
                    with self.subTest(name=name,key=key,missing=missing),self.assertRaises(protocol.NativePublisherError):
                        received(value["receipt"],value,**{name:binding})

    def test_current_operation_and_supported_profile_cannot_come_from_the_receipt(self):
        value=example()
        for name,argument in (("identity","identity"),("profile","supported_profile")):
            for key in value[name]:
                changed=deepcopy(value[name]);changed[key]=changed[key]+1 if type(changed[key]) is int else "changed-server-context"
                with self.subTest(name=name,key=key),self.assertRaises(protocol.NativePublisherError):
                    received(value["receipt"],value,**{argument:changed})

    def test_caller_and_ci_origins_are_not_hosted_transport_authority(self):
        for origin in (None,True,"caller","authenticated_ci",{}):
            with self.subTest(origin=type(origin)),self.assertRaises(protocol.NativePublisherError):received(example()["receipt"],origin=origin)

    def test_case_count_order_copy_layout_and_derived_oracles_survive_self_rehash(self):
        original=example()
        for fault in ("missing_case","order","count","body","canonical","layout","probe","oracle","current","authority"):
            value=deepcopy(original["receipt"])
            if fault=="missing_case":value["cases"].pop()
            elif fault=="order":value["cases"].reverse()
            elif fault=="count":value["cases"][2]["article_count"]+=1
            elif fault in {"body","canonical"}:value["cases"][2]["checks"]["body_copy" if fault=="body" else "canonical"]=False
            elif fault=="layout":value["cases"][4]["layout_preserved"]=False
            elif fault=="probe":value["cases"][3]["added_record_digests"]=["d"*64]
            elif fault=="oracle":value["cases"][2]["oracle_digest"]="d"*64
            elif fault=="current":value["current"]["source_sha"]="d"*40
            else:value["publication_authority"]=True
            value["evidence_digest"]=protocol.digest({key:item for key,item in value.items() if key!="evidence_digest"})
            with self.subTest(fault=fault),self.assertRaises(protocol.NativePublisherError):received(value,original)

    def test_every_object_and_scalar_remains_closed_and_uncoerced_after_rehash(self):
        original=example();proof=original["receipt"]
        def paths(value,path=()):
            yield path,value
            if isinstance(value,dict):
                for key,item in value.items():yield from paths(item,(*path,key))
            elif isinstance(value,list):
                for index,item in enumerate(value):yield from paths(item,(*path,index))
        for path,item in paths(proof):
            if path==("evidence_digest",):continue
            value=deepcopy(proof)
            if not path:value["unknown_field"]="unrepresented"
            else:
                parent=value
                for key in path[:-1]:parent=parent[key]
                if isinstance(item,dict):parent[path[-1]]["unknown_field"]="unrepresented"
                else:parent[path[-1]]=True if type(item) is int else 1 if type(item) is bool else {} if isinstance(item,list) else []
            value["evidence_digest"]=protocol.digest({key:item for key,item in value.items() if key!="evidence_digest"})
            with self.subTest(path=path),self.assertRaises(protocol.NativePublisherError):received(value,original)

    def test_schema4_does_not_become_legacy_or_schema3_ready_evidence(self):
        from .website_publishing_inputs import validate_candidate
        from .website_sealed_verification import validate_hosted_sealed_proof
        from .website_contract import WebsiteAuthorityError
        value=example()
        with self.assertRaises(PublishingInputsError):validate_candidate(value["candidate"])
        with self.assertRaises(WebsiteAuthorityError):validate_hosted_sealed_proof(value["receipt"],origin="authenticated_hosted",binding={},
            candidate=value["candidate"]["payload"]["native_inputs_v1"],supported_verifier={})


class NativePublisherV4ProvenanceTests(SimpleTestCase):
    def tree(self,root):
        from scripts import check_native_publisher_v4_contract as check
        base=Path(__file__).resolve().parents[1];remote={};records={}
        for local,source in check.ARTIFACTS.items():
            raw=(base/local).read_bytes();target=root/local;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            records[local]={"source_path":source,"sha256":hashlib.sha256(raw).hexdigest()};remote[source]=raw
        producers={}
        for path in check.PRODUCERS:
            raw=b"Synthetic producer provenance bytes\n";remote[path]=raw;producers[path]={"sha256":hashlib.sha256(raw).hexdigest()}
        lock={"schema_version":1,"source_repository":check.SOURCE_REPOSITORY,"source_revision":"a"*40,"artifacts":records,"producers":producers}
        (root/check.LOCK).write_text(json.dumps(lock))
        def fetch(source,revision,token):
            self.assertEqual(revision,"a"*40);self.assertEqual(token,"synthetic-read-key");return remote[source]
        return lock,remote,fetch

    def test_three_artifacts_and_actual_producers_are_checked_at_one_immutable_pin(self):
        from scripts import check_native_publisher_v4_contract as check
        with TemporaryDirectory() as directory:
            root=Path(directory);lock,_,fetch=self.tree(root);observed=Mock(side_effect=fetch)
            self.assertEqual(check.verify(root,remote=True,token="synthetic-read-key",fetch=observed),lock)
            self.assertEqual(observed.call_count,6)

    def test_missing_access_moving_refs_and_remapped_producers_fail_closed(self):
        from scripts import check_native_publisher_v4_contract as check
        for fault in ("token","ref","path","producer"):
            with self.subTest(fault=fault),TemporaryDirectory() as directory:
                root=Path(directory);lock,_,_=self.tree(root)
                if fault=="ref":lock["source_revision"]="main"
                elif fault=="path":lock["artifacts"][next(iter(check.ARTIFACTS))]["source_path"]="other/path.py"
                elif fault=="producer":lock["producers"]["other/producer.py"]=lock["producers"].pop(next(iter(check.PRODUCERS)))
                (root/check.LOCK).write_text(json.dumps(lock))
                fetch=Mock(side_effect=check.ContractCheckError("refused"))
                with self.assertRaises(check.ContractCheckError):check.verify(root,remote=True,token=None if fault=="token" else "synthetic-read-key",fetch=fetch)
                fetch.assert_not_called()

    def test_changed_local_remote_producer_and_duplicate_metadata_are_refused(self):
        from scripts import check_native_publisher_v4_contract as check
        for fault in ("local","remote","producer","duplicate"):
            with self.subTest(fault=fault),TemporaryDirectory() as directory:
                root=Path(directory);_,remote,fetch=self.tree(root)
                if fault=="local":(root/next(iter(check.ARTIFACTS))).write_text("Changed")
                elif fault=="remote":remote[next(iter(check.ARTIFACTS.values()))]=b"Changed"
                elif fault=="producer":remote[next(iter(check.PRODUCERS))]=b"Changed"
                else:
                    path=root/check.LOCK;path.write_text(path.read_text().replace('"schema_version": 1','"schema_version": 1, "schema_version": 1'))
                with self.assertRaises(check.ContractCheckError):check.verify(root,remote=True,token="synthetic-read-key",fetch=fetch)
