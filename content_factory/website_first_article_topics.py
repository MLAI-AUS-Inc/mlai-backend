"""Choose one uncovered first-article topic using current approved editorial policy."""

from collections import Counter
import math

from . import website_goal as goals
from .editorial_catalog import CATALOG_KEY, article_brief_for_catalog, catalog_payload
from .website_contract import WebsiteAuthorityError


def _number(value, default=0):
    if type(value) is str and len(value)<=32:
        try:value=float(value)
        except ValueError:return default
    return value if type(value) in {int,float} and math.isfinite(value) and value>=0 else default


def _brief(strategy, candidate, source):
    if CATALOG_KEY not in (strategy or {}):
        return None
    catalog=catalog_payload(strategy)
    approved=[item for item in catalog["audience_options"] if item["status"]=="approved"]
    saved=goals.mapping(source.get("editorial_brief") or source.get("editorialBrief"))
    audience_id=source.get("preferred_audience_id") or goals.mapping(source.get("research_audience")).get("id") or saved.get("audience_id")
    audience=next((item for item in approved if item["id"]==audience_id),None)
    if audience is None and not audience_id and len(approved)==1:
        audience=approved[0]
    if audience is None:
        raise ValueError("No current approved customer profile matches this topic")
    offers=[item for item in catalog["cta_options"] if item["status"]=="approved"
        and audience["id"] in item["audience_ids"] and item["countries"]]
    brief={"audience_id":audience["id"],"audience_version":audience["version"],
        "reader_task":audience["reader_task"],
        "distinct_contribution":f"Answer {candidate['title']} with evidence and concrete examples for this reader task.",
        "acceptance_criteria":[audience["reader_task"],*audience.get("constraints",[])[:8]]}
    if offers:
        offer=next((item for item in offers if item["id"]==saved.get("offer_id")),offers[0])
        country=saved.get("country")
        if country not in offer["countries"]:
            country="AU" if "AU" in offer["countries"] else sorted(offer["countries"])[0]
        brief.update(conversion_intent="offer",offer_id=offer["id"],offer_version=offer["version"],country=country)
    elif audience.get("allow_no_offer"):
        # This no-offer permission is itself covered by the owner's audience
        # approval receipt. Never invent that permission for a draft profile.
        brief.update(conversion_intent="none",no_offer_reason="Informational first article for the approved reader task.",
            country=saved.get("country") or "AU")
    else:
        raise ValueError("No approved offer is available for this customer profile")
    return article_brief_for_catalog(strategy,{"editorial_brief":brief})


def choose(candidates, *, strategy, sources):
    """Rank current uncovered candidates; ambiguity never grants an editorial approval."""
    if not isinstance(candidates,list) or len(candidates)>2000:
        raise WebsiteAuthorityError("website_prepare_blocked","The first-article topic inventory is unavailable.")
    counts=Counter(item.get("id") for item in candidates if isinstance(item,dict) and isinstance(item.get("id"),str))
    eligible=[]
    for candidate in candidates:
        if not isinstance(candidate,dict):continue
        identifier=candidate.get("id")
        if (not isinstance(identifier,str) or not identifier or len(identifier)>512 or counts[identifier]!=1
            or candidate.get("alreadyWritten") or candidate.get("writtenArticle") or candidate.get("coveredTopic")
            or str(candidate.get("status") or "").lower() in {"written","in_progress","skipped","declined"}):continue
        title=str(candidate.get("title") or candidate.get("keyword") or "").strip()
        keyword=str(candidate.get("keyword") or title).strip()
        if not title or len(title)>1000 or len(keyword)>1000:continue
        item={**candidate,"title":title,"keyword":keyword}
        try:brief=_brief(strategy,item,goals.mapping(sources.get(item.get("sourceRunId"))))
        except ValueError:continue
        eligible.append((item,brief))
    if not eligible:
        raise WebsiteAuthorityError("website_prepare_blocked",
            "No uncovered topic matches the approved customer profile and offer. Review the initial article choices.")
    candidate,brief=min(eligible,key=lambda pair:(-_number(pair[0].get("opportunityScore")),
        -_number(pair[0].get("volume")),_number(pair[0].get("difficulty"),100),pair[0]["keyword"],pair[0]["id"]))
    data={"topic_candidate_id":candidate["id"],"selected_title":candidate["title"],
        "topic":candidate["title"],"target_keyword":candidate["keyword"],"source_run_id":candidate.get("sourceRunId") or ""}
    if brief is not None:data["editorial_brief"]=brief
    return data


def current_selection(context, config):
    """Read the canonical catalogue/island pool and its tenant-owned audience provenance."""
    from .vibe_marketing_views import _topic_selection_candidate_pool, DISCOVERY_WORKFLOWS
    from workflow_runs.models import ContentFactoryRun
    candidates=_topic_selection_candidate_pool(context.organization,config)
    identifiers={item.get("sourceRunId") for item in candidates if isinstance(item,dict)
        and isinstance(item.get("sourceRunId"),str) and item["sourceRunId"]}
    if len(identifiers)>128:
        raise WebsiteAuthorityError("website_prepare_blocked","The first-article topic inventory exceeded its bounded source set.")
    sources={run.run_id:goals.mapping(run.run_request) for run in ContentFactoryRun.objects.filter(
        organization_id=context.organization.pk,workflow__in=DISCOVERY_WORKFLOWS,run_id__in=identifiers).only("run_id","run_request")}
    return choose(candidates,strategy=config.pillar_strategy,sources=sources)
