"""Minimize provider identity before an owner credential can be persisted."""

from .cms_api_transport import request_json, CmsApiError
from .cms_connection_contract import canonical_origin, CmsContractError


def inspect_identity(binding, credential):
    """Read the explicit destination; do not discover or follow credential URLs.

    The public site claim is checked without a credential first. A separate
    authenticated request proves the supplied application/integration key. This
    does not prove rendered publication or grant GitHub capabilities.
    """
    site, _ = request_json(binding, None, method="GET", resource="site")
    try:
        if binding["platform"] == "wordpress":
            if (not isinstance(site, dict) or canonical_origin(site.get("home")) != binding["public_origin"] or
                    canonical_origin(site.get("url")) != binding["api_origin"]):
                raise CmsApiError("site_mismatch")
            user, _ = request_json(binding, credential, method="GET", resource="users", identifier="me", query={"context": "edit"})
            if (not isinstance(user, dict) or type(user.get("id")) is not int or not 1 <= user["id"] < 10**19 or
                    not isinstance(user.get("capabilities"), dict)):
                raise CmsApiError("invalid_response")
            capabilities = user["capabilities"]
            if capabilities.get("edit_posts") is not True:
                raise CmsApiError("credentials_required")
            return {"schema_version": 1, "platform": "wordpress", "principal_id": str(user["id"]),
                "can_edit_posts": True, "can_publish_posts": capabilities.get("publish_posts") is True,
                "can_upload_media": capabilities.get("upload_files") is True}
        if (not isinstance(site, dict) or not isinstance(site.get("site"), dict) or
                canonical_origin(site["site"].get("url")) != binding["public_origin"]):
            raise CmsApiError("site_mismatch")
        # site/ is public in Ghost; its success alone is never authentication.
        posts, _ = request_json(binding, credential, method="GET", resource="posts", query={"limit": 1, "formats": "html"})
        if not isinstance(posts, dict) or not isinstance(posts.get("posts"), list) or len(posts["posts"]) > 1:
            raise CmsApiError("invalid_response")
        return {"schema_version": 1, "platform": "ghost", "principal_id": credential.secret.split(":")[0].lower(),
            "can_edit_posts": True, "can_publish_posts": True, "can_upload_media": True}
    except CmsApiError:
        raise
    except (CmsContractError, TypeError, KeyError):
        raise CmsApiError("site_mismatch") from None
