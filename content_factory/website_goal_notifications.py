"""Fixed owner notices from accepted goal state and source-bound completion proof."""
from urllib.parse import urlencode, urlsplit
from django.conf import settings
from . import website_goal as goals
from .website_doctor import safe_identifier
from .website_status import OWNER_ACTIONS

LABELS = {"authorize_github":"Approve GitHub access", "review_github_permissions":"Approve GitHub access",
    "review_setup":"Review setup", "merge_pr":"Merge the setup pull request", "deploy_site":"Deploy your site",
    "choose_articles_page":"Choose your articles page", "remove_setup_example":"Review example cleanup",
    "choose_article_channel":"Choose your articles channel", "review_draft":"Review your first article"}


def notice_for(op, *, now):
    """Project one fixed owner action or proof-backed completion, without provider text."""
    goal = goals.mapping(op.receipt).get("goal")
    scope = goals.scope_for(op.connection,op.payload.get("company_id"))
    if not goals.valid_goal(goal,scope) or op.generation != op.connection.generation or op.connection.state != "connected":return None
    source = goal.get("current_source_sha")
    if op.state == "needs_user":
        raw = goals.mapping(op.receipt.get("user_action"));action = raw.get("id")
        if not isinstance(action,str) or action not in OWNER_ACTIONS | {"review_draft"}:return None
        context = {"action":action,"run_id":safe_identifier(raw.get("run_id")),"pr_number":raw.get("pr_number") if type(raw.get("pr_number")) is int and raw["pr_number"] > 0 else None}
        state,text = "needs_you",LABELS[action] + " to continue your articles setup."
    elif op.state == "completed" and goals.complete(goal):
        if any(item["required"] and not goals.valid_proof(item["name"],item.get("proof"),scope=scope,source_sha=source,now=now) for item in goal["milestones"]):return None
        state,context = "done",{}
        text = "Your first article is live." if goal["target"] == "first_article_live" else "Your articles setup is live."
    else:return None
    origin = str(getattr(settings,"COMMUNITY_CHAT_FRONTEND_URL","")).rstrip("/")
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:return None
    if (parts.scheme != "https" or parts.hostname not in {"chat.mlai.au","www.chat.mlai.au"} or port not in {None,443}
        or parts.path not in {"","/"} or parts.username or parts.password or parts.query or parts.fragment):return None
    link = origin + "/my-startup/create?" + urlencode({"step":"articleSystem","company_id":scope["company_id"],"website_goal":goal["goal_id"]})
    if state=="done" and goal["target"]=="first_article_live":
        receipt=goals.mapping(op.receipt.get("first_article_live_receipt"))
        proof=goal["milestones"][-1].get("proof")
        try:
            article=urlsplit(receipt.get("public_url",""))
            apex=op.connection.organization.domain.lower().rstrip(".").removeprefix("www.")
            if (receipt.get("status")!="passed" or receipt.get("provider_verified") is not True
                or receipt.get("source_sha")!=source or goals.digest(receipt)!=proof["evidence_digest"]
                or article.scheme!="https" or article.hostname not in {apex,"www."+apex} or article.port not in {None,443}
                or article.username or article.password or article.query or article.fragment or not article.path or article.path=="/"):
                return None
            link=article.geturl()
        except (ValueError,TypeError,AttributeError):return None
    authority = {name:value for name,value in scope.items() if name != "configuration_revision"}
    identity = {"goal_id":goal["goal_id"],"scope":authority,"state":state,**context}
    if state == "done":identity["source"] = source
    return {"version":1,"id":goals.digest(identity),"goal_id":goal["goal_id"],"state":state,"action":context.get("action"),
        "subject":"Your articles need your action" if state == "needs_you" else "Your articles are live",
        "text":text,"link":link}
