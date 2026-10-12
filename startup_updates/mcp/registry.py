"""One tool catalogue with audience and least-privilege boundaries."""
from copy import deepcopy

from rest_framework.exceptions import PermissionDenied, ValidationError

from . import tools
from .config import STARTUP_IDENTITY_SCOPES, mcp_url, mlai_url


def modules(resource):
    """Keep new tools off the legacy resource even if a token is malformed."""
    if resource == mcp_url():
        return (tools,)
    if resource == mlai_url():
        from . import articles, community
        return (tools, articles, community)
    raise PermissionDenied("Choose an authorised MCP resource.")


def scopes_for(module):
    """Map every advertised tool to an explicitly enforced permission."""
    if module is tools:
        return {item["name"]: "startup:draft:write" if item["name"] == "save_narrative_draft" else "startup:brief:read" for item in tools.TOOLS}
    return module.TOOL_SCOPES


def effective_scope(module, name, principal):
    """Permit startup identity discovery for every selected startup capability."""
    if module is tools and name == "list_startups" and principal.grant["resource"] == mlai_url():
        return next((scope for scope in STARTUP_IDENTITY_SCOPES if scope in principal.grant["scopes"]), "startup:brief:read")
    return scopes_for(module)[name]


def catalogue(principal):
    """Return granted tools with portable OAuth security metadata."""
    result = []
    for module in modules(principal.grant["resource"]):
        mapping = scopes_for(module)
        for item in module.TOOLS:
            scope = effective_scope(module, item["name"], principal)
            required = [scope] if isinstance(scope, str) else list(scope)
            if not set(required) <= set(principal.grant["scopes"]):
                continue
            value = deepcopy(item)
            security = [{"type": "oauth2", "scopes": required}]
            value["securitySchemes"] = security
            value.setdefault("_meta", {})["securitySchemes"] = deepcopy(security)
            result.append(value)
    if principal.grant["resource"] == mlai_url():
        from .native_agents import available_tools
        result.extend(available_tools(principal))
    return result


def call(principal, name, arguments):
    """Dispatch only within the token audience and recheck grants in the adapter."""
    if not isinstance(name, str):
        raise ValidationError("Use a supported tool name.")
    if principal.grant["resource"] == mlai_url():
        from . import native_agents
        if name in native_agents.TOOL_SCOPES:
            return native_agents.call_tool(principal, name, arguments)
    for module in modules(principal.grant["resource"]):
        mapping = scopes_for(module)
        if name in mapping:
            required = effective_scope(module, name, principal)
            required = {required} if isinstance(required, str) else set(required)
            if not required <= set(principal.grant["scopes"]):
                raise PermissionDenied("Reconnect MLAI with permission for this capability.")
            return module.call_tool(principal, name, arguments)
    raise ValidationError("Unknown MCP tool for this connection.")


def required_scopes(resource, name):
    """Expose a tool's requested scopes for standards-based step-up challenges."""
    if not isinstance(name, str):
        return []
    if resource == mlai_url():
        from .native_agents import TOOL_SCOPES
        if name in TOOL_SCOPES:
            return sorted(TOOL_SCOPES[name])
    for module in modules(resource):
        scope = scopes_for(module).get(name)
        if scope:
            return sorted([scope] if isinstance(scope, str) else scope)
    return []
