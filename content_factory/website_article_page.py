"""Read-only article-page choice from scan evidence; never grants readiness."""
import math
from django.conf import settings
from .website_goal import mapping
from .website_failure_redaction import redact_secret_text


def route(value):
    """Accept only a bounded public route, without queries, credentials or traversal."""
    if not isinstance(value, str) or len(value)>255 or not value.startswith("/") or value.startswith("//") or any(c in value for c in "?#%\\\r\n") or any(c.isspace() for c in value) or any(part in {".",".."} for part in value.split("/")) or redact_secret_text(value)!=value:
        return None
    return value.rstrip("/") or "/"


def confidence(value):
    """Malformed confidence is unknown; booleans and nonfinite scores never win."""
    return value if type(value) in {int,float} and math.isfinite(value) and 0<=value<=1 else None


def choose_page(discovery, *, pick_when_ambiguous=False):
    """Prefer a clear listing with detail children, then a stable existing/default route."""
    discovery=mapping(discovery);resolution=mapping(discovery.get("resolution"))
    ranked=mapping(resolution.get("ranked_candidates"))
    listing=mapping(resolution.get("listing_surface"))
    preferred=route(listing.get("route") or mapping(listing.get("metadata")).get("route_path"))
    raw=ranked.get("listing_surface_candidates") or resolution.get("listing_surface_candidates")
    if not isinstance(raw,list):raw=[]
    rows=[]
    candidates=discovery.get("candidates")
    if not isinstance(candidates,list):candidates=[]
    for item in (raw+candidates)[:128]:
        value=mapping(item)
        if value.get("candidate_group") not in {None,"listing_surface_candidates"}:continue
        path=route(value.get("route") or mapping(value.get("metadata")).get("route_path"))
        if path and not any(row["route"]==path for row in rows):rows.append({"route":path,"confidence":confidence(value.get("confidence"))})
    if preferred and not any(row["route"]==preferred for row in rows):rows.insert(0,{"route":preferred,"confidence":confidence(listing.get("confidence"))})
    details=ranked.get("detail_surface_candidates")
    if not isinstance(details,list):details=[]
    details=[mapping(item) for item in (details+candidates)[:128] if mapping(item).get("candidate_group") in {None,"detail_surface_candidates"}]
    def children(path):
        return any(isinstance(detail,str) and detail.startswith(path.rstrip("/")+"/") for item in details
            for detail in (item.get("route_template"),item.get("route"),mapping(item.get("metadata")).get("route_template")))
    ordered=sorted(rows,key=lambda item: (-(item["confidence"] if item["confidence"] is not None else -1),item["route"]))
    threshold=confidence(getattr(settings,"ARTICLES_PAGE_CONFIDENCE_MARGIN",.15))
    threshold=.15 if threshold is None or threshold==0 else threshold
    clear=False
    if ordered and ordered[0]["confidence"] is not None:
        runner=ordered[1]["confidence"] if len(ordered)>1 else 0
        clear=runner is not None and ordered[0]["confidence"]-runner>=threshold and children(ordered[0]["route"])
    ambiguous=resolution.get("existence_state")=="ambiguous" and len(rows)>1 and not clear
    chosen=ordered[0]["route"] if clear else preferred or (ordered[0]["route"] if ordered else "/articles")
    return {"mode":"existing" if rows else "none","route":chosen,"candidates":[item["route"] for item in ordered[:8]],
        "requiresChoice":bool(pick_when_ambiguous and ambiguous),"reason":"clear_listing" if clear else "existing_listing" if rows else "default_articles"}
