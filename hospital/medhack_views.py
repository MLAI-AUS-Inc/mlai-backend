from .announcement_views import HealthHackAnnouncementListCreateView


class CreateAnnouncementView(HealthHackAnnouncementListCreateView):
    """Legacy POST alias for the canonical HealthHack announcement endpoint."""

    http_method_names = ["post", "options"]
