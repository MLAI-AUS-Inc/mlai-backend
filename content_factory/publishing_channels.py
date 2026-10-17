"""Server-owned routing; neither page hints nor worker JSON grant provider access."""

from dataclasses import replace
from django.conf import settings

from .publishing_channel_contract import ChannelRequest, OPERATIONS
from .publishing_channel_adapters import GitChannel, CmsChannel, HostedChannel, PortableChannel
from .website_contract import WebsiteAuthorityError

_REGISTRY = {item.name: item() for item in (GitChannel, CmsChannel, HostedChannel, PortableChannel)}


def enabled():
    """Execution routing is independently disabled while adapters are completed."""
    return getattr(settings, "ARTICLES_PUBLISHING_CHANNELS_ENABLED", False)


def _selected(request):
    from .website_publishing_channels import projection
    record = projection(request.context, request.config)
    if record and record.get("selection"):
        return record["selection"]["channel"]
    # Historical Git operations retain their original channel. An informational
    # CMS recommendation does not redirect already authorized repository work.
    if request.config.website_connection and request.config.website_connection.github_repo:
        return "git"
    return record["recommendation"]["recommended_channel"] if record and record.get("recommendation") else "hosted"


def inspect(request):
    """Use the common read surface without reserving an operation or scheduling."""
    return _REGISTRY[_selected(request)].inspect(request)


def execute(name, request):
    """Route only a named common operation from a trusted controller context."""
    if not isinstance(request, ChannelRequest) or not isinstance(name, str) or name not in OPERATIONS or name == "inspect":
        raise WebsiteAuthorityError("invalid_connection_action", "Choose a supported publishing operation.", status=400)
    if not enabled():
        raise WebsiteAuthorityError("website_prepare_blocked", "Publishing channel execution is disabled.")
    if str(request.context.profile.user_id) != str(getattr(request.user, "pk", "")):
        raise WebsiteAuthorityError("website_scope_mismatch", "The publishing request belongs to another founder.")
    from .website_publishing_channels import scope_for
    scope_for(request.context, request.config)
    selected = _selected(request)
    if request.run is not None:
        from .portable_drafts import original_portable_run
        from .website_connections import scoped_run_contract
        if original_portable_run(request.run):
            selected = "portable"
        elif scoped_run_contract(request.run).get("website_connection_id"):
            selected = "git"
    if request.step == "generation":
        from .portable_drafts import explicit_portable_request
        if explicit_portable_request(request.data.get("data") or {}):
            selected = "portable"
    if name in {"capture_existing", "disconnect"}:
        # Existing repository authority belongs to the connected Git channel,
        # independently of a later preference for portable authoring.
        connection = request.config.website_connection
        if connection and connection.github_repo:
            selected = "git"
        elif connection and "_cms_binding" in (connection.capabilities or {}):
            selected = "cms"
    if request.operation is not None:
        from .website_goal import mapping
        pin = mapping(mapping(request.operation.receipt).get("goal")).get("strategy_pin")
        from .website_strategy import valid_pin
        if valid_pin(pin) and pin["rung"] == "hosted":
            selected = "hosted"
        # Existing durable goals cannot silently inherit a different channel.
        # Non-Git goal reservations are added with their own provider guard.
        if selected not in {"git", "hosted"}:
            raise WebsiteAuthorityError("publishing_adapter_required", "Start a separately authorized goal for this publishing channel.")
    adapter = _REGISTRY[selected]
    if name not in adapter.effect_capabilities:
        return adapter._unavailable(request)
    return getattr(adapter, name)(replace(request, data=dict(request.data)))
