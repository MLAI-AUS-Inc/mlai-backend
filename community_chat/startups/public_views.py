"""An anonymous, read-only page for an explicitly approved public update."""
import re

from django.conf import settings
from django.shortcuts import get_object_or_404, render
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer, TemplateHTMLRenderer
from rest_framework.views import APIView

from community_chat.throttles import CommunityChatScopedThrottle
from .presentation import update_payload
from .publication import approved_updates


SECTIONS = (
    ("summary", "Summary"), ("highlights", "Highlights"),
    ("challenges", "Challenges"), ("learnings", "Learnings"),
    ("next30Days", "Next month"), ("asks", "Ways to help"),
)


def public_sections(update):
    """Render saved dot points as escaped text; never trust narrative as HTML."""
    sections = []
    for key, label in SECTIONS:
        value = update.get(key)
        if isinstance(value, list):
            points = [str(point).strip() for point in value if str(point).strip()]
        else:
            points = []
            parent_indent = None
            for line in str(value or "").splitlines():
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip())
                marker = re.match(r"^(\s*)(?:[-+*•](?:\s+|$)|\d+[.)]\s+)(.*)$", line)
                if parent_indent is not None and indent > parent_indent and points:
                    points[-1] += "\n" + line[min(parent_indent + 2, indent):]
                elif marker:
                    if marker[2].strip():
                        points.append(marker[2].strip())
                        parent_indent = len(marker[1])
                else:
                    points.append(line.strip())
                    parent_indent = None
        if points:
            sections.append({"label": label, "points": points})
    return sections


class PublicUpdateView(APIView):
    """Anyone with a public link may read the approved disclosure only."""
    authentication_classes = ()
    permission_classes = (AllowAny,)
    renderer_classes = (TemplateHTMLRenderer, JSONRenderer)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"
    http_method_names = ["get", "head", "options"]

    def get(self, request, update_id):
        if not getattr(settings, "COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED", False):
            raise NotFound("Update not found.")
        draft = get_object_or_404(approved_updates("public"), pk=update_id)
        update = update_payload(draft, published=True, community=True)
        metrics = [
            {"label": (update.get("metricEvidence", {}).get(key) or {}).get("label") or key,
             "value": value if value not in (None, "") else "Unknown"}
            for key, value in (update.get("metrics") or {}).items()
        ]
        return render(request, "community_chat/startups/public_update.html", {
            "update": update, "sections": public_sections(update), "metrics": metrics,
        })

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # Republishing privately must revoke this link immediately, including caches.
        response["Cache-Control"] = "no-store"
        response["X-Robots-Tag"] = "noindex, nofollow"
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        response["Referrer-Policy"] = "no-referrer"
        return response
