"""Render a registered message to (subject, html, text)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from django.template import Context, Template
from django.template.loader import render_to_string

from .registry import MessageSpec


class SystemEmailContextError(ValueError):
    """The caller left out context a template needs; nothing is sent."""


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, tuple, dict)) and len(value) == 0)


def render(spec: MessageSpec, context: Mapping[str, Any] | None = None,
           prerendered: RenderedEmail | None = None) -> RenderedEmail:
    """Render ``spec`` with ``context``; prerendered specs return the caller's copy.

    Missing required context raises ``SystemEmailContextError`` naming only the
    keys, never their values (which may be codes or links).
    """
    if spec.prerendered:
        if prerendered is None or not prerendered.subject or not (prerendered.html or prerendered.text):
            raise SystemEmailContextError(f"{spec.key}: prerendered subject and body are required")
        return prerendered
    context = dict(context or {})
    missing = [key for key in spec.required_context if _missing(context.get(key))]
    if missing:
        raise SystemEmailContextError(f"{spec.key}: missing context {', '.join(sorted(missing))}")
    subject = Template(spec.subject).render(Context(context)).strip()
    # A header can never carry a line break.
    subject = " ".join(subject.split())
    context.setdefault("subject", subject)
    html = render_to_string(spec.html_template, context)
    text = render_to_string(spec.text_template, context).strip() + "\n"
    return RenderedEmail(subject=subject, html=html, text=text)
