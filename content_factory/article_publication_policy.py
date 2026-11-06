"""Shared publication capability over already validated website facts."""


def can_publish_article(*,integration_verified,write_verified,deployment_verified):
    """Require explicit current integration, write access and live deployment."""
    return all(value is True for value in (integration_verified,write_verified,deployment_verified))


def journey_can_publish(journey):
    """Read the journey's underlying facts instead of trusting its derived label."""
    def mapping(value):return value if isinstance(value,dict) else {}
    journey=mapping(journey)
    capabilities=mapping(journey.get("capabilities"))
    policy=mapping(journey.get("policy"))
    deployment=mapping(mapping(journey.get("prerequisites")).get("deployment"))
    return can_publish_article(integration_verified=capabilities.get("canGenerateArticle"),
        write_verified=capabilities.get("repositoryWriteVerified") is True and policy.get("allowed") is True,
        deployment_verified=deployment.get("status")=="complete")
