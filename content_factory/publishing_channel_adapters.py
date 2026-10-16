"""Channel wrappers reuse existing guarded handlers; provider stubs deny effects."""

from .publishing_channel_contract import PublishingChannel
from .website_contract import WebsiteAuthorityError


class UnavailableChannel(PublishingChannel):
    """An unimplemented provider is visible without claiming a working connector."""

    name = "unavailable"

    def inspect(self, request):
        """Project local facts only; never dispatch provider work from a read."""
        from .website_publishing_channels import projection
        return {"channel": self.name, "capabilities": sorted(self.effect_capabilities),
            "planning": projection(request.context, request.config)}

    def _unavailable(self, request):
        raise WebsiteAuthorityError("publishing_adapter_required",
            "This publishing channel is not configured. Hosted and portable alternatives remain available.")

    capture_existing = _unavailable
    prepare = _unavailable
    verify = _unavailable
    publish = _unavailable
    unpublish = _unavailable
    disconnect = _unavailable


class GitChannel(UnavailableChannel):
    """Preserve the current Git operation, approval, build and publication controls."""

    name = "git"
    effect_capabilities = frozenset({"capture_existing", "prepare", "verify", "publish", "disconnect"})

    def capture_existing(self, request):
        """Use the current scoped scan with its existing dispatch reservation."""
        from .website_prepare import _dispatch_git_prepare_step
        return _dispatch_git_prepare_step(request.operation, request.context, request.config, "scan",
            user=request.user, journey=request.data.get("journey"))

    def prepare(self, request):
        """Use the original scan/setup preconditions and exact strategy pin."""
        from .website_prepare import _dispatch_git_prepare_step
        return _dispatch_git_prepare_step(request.operation, request.context, request.config, request.step,
            user=request.user, journey=request.data.get("journey"))

    def verify(self, request):
        """Retain exact-source CI and deployed-content proof requirements."""
        from .website_prepare import _dispatch_git_prepare_step
        return _dispatch_git_prepare_step(request.operation, request.context, request.config, request.step,
            user=request.user, journey=request.data.get("journey"))

    def publish(self, request):
        """Keep original generation intent, review identity, billing and merge checks."""
        if request.step == "first-article":
            from .website_first_article import _advance_git_first_article
            return _advance_git_first_article(request.operation, request.context, request.config,
                user=request.user, now=request.now)
        if request.run is not None:
            from .vibe_marketing_views import _control_article_run_for_channel
            return _control_article_run_for_channel(user=request.user, context=request.context,
                run=request.run, **request.data)
        from .vibe_marketing_views import _start_article_generation_for_channel
        return _start_article_generation_for_channel(user=request.user, context=request.context,
            **request.data)

    def disconnect(self, request):
        """Use the existing generation fence, refund and owned-cleanup manifest."""
        from .website_disconnect import start_disconnect
        return start_disconnect(request.config, user=request.user, data=request.data)


class CmsChannel(UnavailableChannel):
    """Authenticated CMS effects are supplied by v2 3.3 and 3.6."""

    name = "cms"
    effect_capabilities = frozenset({"disconnect"})

    def disconnect(self, request):
        """Erase stored CMS authority and retain published provider content."""
        from .cms_owner_connection import _owner
        from .website_disconnect import start_disconnect
        _owner(request.context, request.config, request.user)
        connection = request.config.website_connection
        if not connection or "_cms_binding" not in (connection.capabilities or {}):
            raise WebsiteAuthorityError("website_connection_required", "Choose the current CMS connection.")
        if request.data.get("remove_setup", False) is not False:
            raise WebsiteAuthorityError("published_content_review_required", "Disconnect keeps CMS articles. Unpublish each owned article separately.")
        operation = start_disconnect(request.config, user=request.user, data={**request.data, "remove_setup": False})
        operation.receipt = {**operation.receipt, "channel": "cms", "credential_erased": True,
            "credential_revocation_instruction": "Revoke this application password or integration key in your CMS as well.",
            "published_content_retained": True}
        operation.save(update_fields=["receipt", "updated_at"])
        return operation


class HostedChannel(UnavailableChannel):
    """Publication requires v2 3.4's selected and verified hosted origin."""

    name = "hosted"


class PortableChannel(UnavailableChannel):
    """Export through the existing explicit content-only admission and billing."""

    name = "portable"
    effect_capabilities = frozenset({"publish"})

    def publish(self, request):
        """Create a draft/export; never report that a first-article-live goal passed."""
        if request.step == "first-article":
            raise WebsiteAuthorityError("publishing_adapter_required", "A portable export does not publish a live article.")
        from .portable_drafts import explicit_portable_request
        data = request.data.get("data")
        if not explicit_portable_request(data or {}) or request.data.get("website_contract"):
            raise WebsiteAuthorityError("portable_repository_access_denied", "Confirm a separate portable draft without repository authority.")
        from .vibe_marketing_views import _start_article_generation_for_channel
        return _start_article_generation_for_channel(user=request.user, context=request.context, **request.data)
