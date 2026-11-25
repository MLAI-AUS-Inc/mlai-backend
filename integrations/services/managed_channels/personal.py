"""Presentation hints on an already authorized, personal Slack catalog entry."""
from django.conf import settings


_INITIAL_NAMES = {
    "mlai-committee-2026": "committee",
    "mlai-studio-builders": "committee",
    "mlai-tech-team": "committee",
    "newsletters": "committee",
    "tech_volunteers": "committee",
    "victor-ai": "project",
    "project-cybertest": "project",
    "project-studynash": "project",
    "ada-conf": "event",
    "aie-sydney": "event",
    "dev-day-physical-ai": "event",
    "devday-exchange-volunteers": "event",
    "healthhack": "event",
}


def personal_section(conversation):
    from integrations.services.slack_chat_catalog import conversation_kind, conversation_metadata

    if conversation_kind(conversation) != "private_channel":
        return None
    workspace = getattr(conversation, "slack_workspace_id", "")
    source = getattr(conversation, "slack_conversation_id", "")
    if not workspace or not source:
        return None
    explicit = getattr(settings, "MANAGED_CHANNEL_SLACK_SECTIONS", {}).get(workspace, {})
    if source in explicit:
        section = explicit[source]
        return section if section in {"committee", "project", "event"} else None
    if workspace == getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", ""):
        return _INITIAL_NAMES.get(conversation_metadata(conversation).get("name"))
    return None
