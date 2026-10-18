"""Closed native publisher setup evidence; no transport or Ready authority."""
from copy import deepcopy
import hashlib
import json
import re

CHECKS = {"content_loading", "listing", "detail", "unknown_slug_404", "canonical", "assets", "browser", "narrow_layout", "wide_layout", "body_copy"}
CASES = ("baseline", "initial", "independent_1", "independent_2", "successive_1", "successive_2")
IDENTITY_FIELDS = {"website_connection_id", "connection_generation", "repository_id", "github_repo", "deletion_epoch",
    "operation_id", "operation_attempt", "target_id", "target_inputs_digest"}
FIELDS = {"schema_version", "kind", "verification_mode", "identity", "publishing_inputs_digest", "original_scope_digest",
    "baseline", "current", "pipeline_evidence_digest", "profile", "cases", "native_verified", "publication_authority",
    "support_proven", "live_goal_proof", "evidence_digest"}


class NativePublisherError(ValueError):
    """A fixed diagnostic without page, command, provider or credential contents."""


def receipt_schema():
    """Export the closed wire shape; semantic/source checks remain mandatory."""
    def obj(properties):return {"type":"object","additionalProperties":False,"properties":properties,"required":list(properties)}
    def text(pattern=None,maximum=500):return {"type":"string","minLength":1,"maxLength":maximum,**({"pattern":pattern} if pattern else {})}
    h=text(r"[a-f0-9]{64}",64);sha=text(r"[a-f0-9]{40}(?:[a-f0-9]{24})?",64)
    uuid=text(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}",36)
    def integer(minimum=1,maximum=2**63-1):return {"type":"integer","minimum":minimum,"maximum":maximum}
    truth={"type":"boolean","const":True};false={"type":"boolean","const":False}
    identity=obj({"website_connection_id":uuid,"connection_generation":integer(),"repository_id":integer(),"github_repo":text(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}",201),
        "deletion_epoch":integer(0),"operation_id":uuid,"operation_attempt":integer(),"target_id":text(maximum=200),"target_inputs_digest":h})
    facts=obj({"source_sha":sha,"source_tree_sha":sha,"source_inventory_digest":h})
    profile=obj({"controller_sha256":h,"oracle_sha256":h,"layout_sha256":h,"python_version":text(r"\d+\.\d+\.\d+",20),
        "pyyaml_version":text(r"\d+\.\d+\.\d+",20),"playwright_version":{"type":"string","const":"1.58.0"},"runtime_image":text(r"sha256:[a-f0-9]{64}",71)})
    row=obj({"case":{"type":"string","enum":list(CASES)},"base_source_sha":sha,"article_count":integer(1,2002),
        "added_record_digests":{"type":"array","items":h,"minItems":0,"maxItems":2},"oracle_digest":h,"result_source_inventory_digest":h,
        "layout_evidence_digest":h,"content_evidence_digest":h,"layout_preserved":truth,"checks":obj({key:truth for key in sorted(CHECKS)}),
        "verified_routes":obj({key:text(r"/[^\\?#\s]*") for key in ("listing","detail","unknown_slug")})})
    return obj({"schema_version":{"type":"integer","const":4},"kind":{"type":"string","const":"original_publisher_native_verification"},
        "verification_mode":{"type":"string","const":"setup"},"identity":identity,"publishing_inputs_digest":h,"original_scope_digest":h,
        "baseline":facts,"current":facts,"pipeline_evidence_digest":h,"profile":profile,"cases":{"type":"array","items":row,"minItems":6,"maxItems":6},
        "native_verified":truth,"publication_authority":false,"support_proven":false,"live_goal_proof":false,"evidence_digest":h})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _hash(value):
    return type(value) is str and bool(re.fullmatch(r"[a-f0-9]{64}",value))


def identity_for(candidate, operation_id, operation_attempt):
    """Original site/target plus the independently supplied current child attempt."""
    payload = candidate["payload"]
    scope = payload["original_scope"]
    target = payload["native_inputs_v1"]["payload"]["publishing"]["target"]
    return {**{key:scope[key] for key in ("website_connection_id","connection_generation","repository_id","github_repo","deletion_epoch")},
        "operation_id":operation_id,"operation_attempt":operation_attempt,"target_id":target["target_id"],"target_inputs_digest":digest(target)}


def oracle_for(candidate, additions):
    """Derive each browser oracle from sealed seed inputs or sealed probe records."""
    value = candidate["payload"]["native_inputs_v1"]["payload"]
    result = {"contract":deepcopy(value["verification"]["execution"]), **deepcopy(value["verification"]["oracle"])}
    if additions:
        record = additions[-1]
        if (type(record.get("title")) is not str or not 1<=len(record["title"])<=512
                or any(ord(char)<32 for char in record["title"])):
            raise NativePublisherError("native_publisher_probe_invalid")
        result.update(seed_slug=record["slug"],expected_h1=record["title"],seed_is_setup_example=False,existing_seed_source=None)
    return result


def validate_identity(identity,candidate):
    """Require the exact current operation/site/target before execution."""
    from uuid import UUID
    try:
        if (type(identity) is not dict or set(identity)!=IDENTITY_FIELDS
                or identity!=identity_for(candidate,identity["operation_id"],identity["operation_attempt"])
                or canonical(identity)!=canonical(identity_for(candidate,identity["operation_id"],identity["operation_attempt"]))
                or type(identity["operation_id"]) is not str or str(UUID(identity["operation_id"]))!=identity["operation_id"]
                or type(identity["operation_attempt"]) is not int or identity["operation_attempt"]<1):raise ValueError
        return deepcopy(identity)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,RecursionError):
        raise NativePublisherError("native_publisher_identity_mismatch") from None


def validate_original_binding(binding,candidate):
    """The received proposal cannot create its previously saved reservation."""
    payload=candidate["payload"]
    expected={**payload["original_scope"],**payload["baseline"],"publishing_inputs_digest":candidate["digest"]}
    try:
        if type(binding) is not dict or set(binding)!=set(expected) or canonical(binding)!=canonical(expected):raise ValueError
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,RecursionError):
        raise NativePublisherError("native_publisher_original_binding_mismatch") from None


def validate_receipt(value, *, candidate, records, identity, profile, original_binding, current_binding, origin, candidate_validate, wire_validate):
    """Read complete typed evidence; callers still owe independent origin/scope."""
    try:
        if type(origin) is not str or origin!="authenticated_hosted":raise ValueError
        wire_validate(value,receipt_schema())
        candidate = candidate_validate(candidate)
        validate_original_binding(original_binding,candidate)
        payload = candidate["payload"]
        if type(value) is not dict or set(value)!=FIELDS or len(canonical(value))>98304:
            raise ValueError
        if (type(value["schema_version"]) is not int or value["schema_version"]!=4
                or value["kind"]!="original_publisher_native_verification" or value["verification_mode"]!="setup"
                or value["native_verified"] is not True or any(value[key] is not False for key in ("publication_authority","support_proven","live_goal_proof"))
                or value["identity"]!=identity or canonical(value["identity"])!=canonical(identity)
                or type(identity) is not dict or set(identity)!=IDENTITY_FIELDS
                or value["publishing_inputs_digest"]!=candidate["digest"] or value["original_scope_digest"]!=digest(payload["original_scope"])
                or value["baseline"]!=payload["baseline"] or canonical(value["baseline"])!=canonical(payload["baseline"])
                or value["profile"]!=profile or canonical(value["profile"])!=canonical(profile)):
            raise ValueError
        validate_identity(identity,candidate)
        current=value["current"]
        if (type(current) is not dict or set(current)!={"source_sha","source_tree_sha","source_inventory_digest"}
                or type(current_binding) is not dict or set(current_binding)!=set(current) or canonical(current_binding)!=canonical(current)):
            raise ValueError
        if (any(type(current[key]) is not str or not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?",current[key]) for key in ("source_sha","source_tree_sha"))
                or len(current["source_sha"])!=len(current["source_tree_sha"]) or not _hash(current["source_inventory_digest"])
                or current["source_sha"]==payload["baseline"]["source_sha"]):
            raise ValueError
        if (type(profile) is not dict or set(profile)!={"controller_sha256","oracle_sha256","layout_sha256","python_version","pyyaml_version","playwright_version","runtime_image"}
                or any(not _hash(profile[key]) for key in ("controller_sha256","oracle_sha256","layout_sha256"))
                or profile["playwright_version"]!="1.58.0" or not re.fullmatch(r"sha256:[a-f0-9]{64}",profile["runtime_image"])
                or any(profile[key]!=payload["executor"][key] for key in ("python_version","pyyaml_version","runtime_image"))
                or profile["oracle_sha256"]!=payload["native_inputs_v1"]["payload"]["verification"]["verifier"]["runner_sha256"]):
            raise ValueError
        if type(records) is not list or len(records)!=2 or [digest(row) for row in records]!=payload["probe_record_digests"]:
            raise ValueError
        cases=value["cases"]
        if type(cases) is not list or len(cases)!=6 or [row.get("case") for row in cases]!=list(CASES):
            raise ValueError
        base=cases[0]["article_count"]
        if type(base) is not int or not 1<=base<=2000:
            raise ValueError
        counts=(base,base,base+1,base+1,base+1,base+2)
        additions=([],[],records[:1],records[1:],records[:1],records)
        for index,(row,count,added) in enumerate(zip(cases,counts,additions)):
            if (type(row) is not dict or set(row)!={"case","base_source_sha","article_count","added_record_digests","oracle_digest","result_source_inventory_digest","layout_evidence_digest","content_evidence_digest","layout_preserved","checks","verified_routes"}
                    or type(row["article_count"]) is not int or row["article_count"]!=count
                    or row["added_record_digests"]!=[digest(item) for item in added] or row["layout_preserved"] is not True
                    or row["base_source_sha"]!=(payload["baseline"]["source_sha"] if index==0 else current["source_sha"])
                    or any(not _hash(row[key]) for key in ("oracle_digest","result_source_inventory_digest","layout_evidence_digest","content_evidence_digest"))
                    or type(row["checks"]) is not dict or set(row["checks"])!=CHECKS or any(item is not True for item in row["checks"].values())):
                raise ValueError
            oracle=oracle_for(candidate,added)
            if index==0:oracle.update(artifact_digest=None,artifact_marker_routes=[])
            if row["oracle_digest"]!=digest(oracle):raise ValueError
            execution=oracle["contract"]
            wanted={"listing":execution["listing_route"],"detail":execution["route_template"].replace("{slug}",oracle["seed_slug"]),
                "unknown_slug":execution["route_template"].replace("{slug}","mlai-nonexistent-"+(oracle["artifact_digest"] or identity["target_inputs_digest"])[:12])}
            if row["verified_routes"]!=wanted:raise ValueError
        if cases[0]["result_source_inventory_digest"]!=value["baseline"]["source_inventory_digest"] or cases[1]["result_source_inventory_digest"]!=current["source_inventory_digest"]:
            raise ValueError
        if not _hash(value["pipeline_evidence_digest"]) or value["evidence_digest"]!=digest({key:item for key,item in value.items() if key!="evidence_digest"}):
            raise ValueError
        return deepcopy(value)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
        raise NativePublisherError("native_publisher_receipt_invalid") from None
