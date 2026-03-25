from __future__ import annotations

import html
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.urls import reverse

PREVIEW_SIGNING_SALT = "content-factory-run-preview"
DEFAULT_PREVIEW_TTL_SECONDS = 7 * 24 * 60 * 60
SLACK_TEXT_LIMIT = 2800


def get_content_factory_preview_ttl_seconds() -> int:
    raw_value = getattr(settings, "CONTENT_FACTORY_PREVIEW_LINK_TTL_SECONDS", DEFAULT_PREVIEW_TTL_SECONDS)
    try:
        return max(int(raw_value), 1)
    except (TypeError, ValueError):
        return DEFAULT_PREVIEW_TTL_SECONDS


def build_content_factory_preview_signature(run_id: str) -> str:
    return signing.dumps(str(run_id), salt=PREVIEW_SIGNING_SALT)


def validate_content_factory_preview_signature(run_id: str, signature: str) -> None:
    resolved = signing.loads(
        signature,
        salt=PREVIEW_SIGNING_SALT,
        max_age=get_content_factory_preview_ttl_seconds(),
    )
    if str(resolved) != str(run_id):
        raise signing.BadSignature("Run ID does not match preview signature.")


def build_content_factory_preview_url(*, request, run_id: str) -> str:
    path = reverse("content_factory_run_preview", kwargs={"run_id": run_id})
    query = urlencode({"sig": build_content_factory_preview_signature(run_id)})
    configured_base = str(getattr(settings, "CONTENT_FACTORY_PREVIEW_BASE_URL", "") or "").strip()
    if configured_base:
        return f"{configured_base.rstrip('/')}{path}?{query}"
    return request.build_absolute_uri(f"{path}?{query}")


def render_content_preview_page(*, domain: str, content_package: Dict[str, Any]) -> str:
    title = _string(content_package.get("title")) or "Content Preview"
    meta_title = _string(content_package.get("meta_title")) or title
    meta_description = _string(content_package.get("meta_description"))
    article_html = _string(content_package.get("article_html"))
    article_markdown = _string(content_package.get("article_markdown"))
    if not article_html and article_markdown:
        article_html = f"<pre>{html.escape(article_markdown)}</pre>"

    hero_image = _dict(content_package.get("hero_image"))
    hero_url = _string(hero_image.get("url"))
    escaped_domain = html.escape(domain or "")
    escaped_title = html.escape(title)
    escaped_meta_title = html.escape(meta_title)
    escaped_meta_description = html.escape(meta_description)
    escaped_hero_url = html.escape(hero_url)

    meta_tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escaped_meta_title}</title>",
    ]
    if meta_description:
        meta_tags.append(f'<meta name="description" content="{escaped_meta_description}">')
        meta_tags.append(f'<meta property="og:description" content="{escaped_meta_description}">')
    meta_tags.append(f'<meta property="og:title" content="{escaped_meta_title}">')
    meta_tags.append('<meta property="og:type" content="article">')
    meta_tags.append('<meta name="twitter:card" content="summary_large_image">')
    if hero_url:
        meta_tags.append(f'<meta property="og:image" content="{escaped_hero_url}">')
        meta_tags.append(f'<meta name="twitter:image" content="{escaped_hero_url}">')

    header_parts = [f"<h1>{escaped_title}</h1>"]
    if escaped_domain:
        header_parts.append(f'<p class="domain">{escaped_domain}</p>')
    if meta_description:
        header_parts.append(f'<p class="dek">{escaped_meta_description}</p>')

    return f"""<!doctype html>
<html lang="en">
  <head>
    {' '.join(meta_tags)}
    <style>
      :root {{
        color-scheme: light;
        --page-bg: #f4efe6;
        --surface: #fffaf2;
        --ink: #1f1a14;
        --muted: #6d6257;
        --line: #decfb9;
        --accent: #88623b;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        padding: 0;
        font-family: Georgia, "Times New Roman", serif;
        background:
          radial-gradient(circle at top left, rgba(136, 98, 59, 0.12), transparent 30%),
          linear-gradient(180deg, #fbf7f1 0%, var(--page-bg) 100%);
        color: var(--ink);
      }}
      .shell {{
        max-width: 900px;
        margin: 0 auto;
        padding: 32px 20px 72px;
      }}
      .header {{
        background: rgba(255, 250, 242, 0.86);
        border: 1px solid var(--line);
        border-radius: 24px;
        padding: 28px 28px 24px;
        box-shadow: 0 20px 50px rgba(76, 53, 27, 0.08);
      }}
      .header h1 {{
        margin: 0;
        font-size: clamp(2rem, 4vw, 3.4rem);
        line-height: 1.05;
      }}
      .domain {{
        margin: 14px 0 0;
        font: 600 0.9rem/1.3 ui-sans-serif, system-ui, sans-serif;
        color: var(--accent);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      .dek {{
        margin: 14px 0 0;
        font: 400 1.05rem/1.6 ui-sans-serif, system-ui, sans-serif;
        color: var(--muted);
        max-width: 60ch;
      }}
      .article {{
        margin-top: 24px;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 24px;
        padding: 32px 28px 36px;
        box-shadow: 0 14px 38px rgba(76, 53, 27, 0.06);
      }}
      .article img {{
        display: block;
        max-width: 100%;
        height: auto;
        border-radius: 18px;
        margin: 20px auto;
      }}
      .article p,
      .article li,
      .article blockquote {{
        font-size: 1.05rem;
        line-height: 1.75;
      }}
      .article h1,
      .article h2,
      .article h3 {{
        line-height: 1.15;
      }}
      .article h2 {{
        margin-top: 2.2rem;
      }}
      .article a {{
        color: #6f4e2b;
      }}
      .article blockquote {{
        margin: 1.6rem 0;
        padding: 0.9rem 1.1rem;
        border-left: 4px solid var(--accent);
        background: rgba(136, 98, 59, 0.08);
      }}
      .footer {{
        margin-top: 20px;
        font: 500 0.9rem/1.4 ui-sans-serif, system-ui, sans-serif;
        color: var(--muted);
        text-align: center;
      }}
      @media (max-width: 640px) {{
        .shell {{
          padding: 18px 14px 48px;
        }}
        .header,
        .article {{
          border-radius: 18px;
          padding: 20px 16px 24px;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="shell">
      <section class="header">
        {''.join(header_parts)}
      </section>
      <section class="article">
        {article_html or '<p>Article preview is unavailable for this run.</p>'}
      </section>
      <p class="footer">Delivered by MLAI Content Factory</p>
    </main>
  </body>
</html>"""


def render_content_preview_error_page(*, title: str, message: str) -> str:
    escaped_title = html.escape(title)
    escaped_message = html.escape(message)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escaped_title}</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        font-family: ui-sans-serif, system-ui, sans-serif;
        background: linear-gradient(180deg, #f7f2ea 0%, #efe3d1 100%);
        color: #23180d;
      }}
      .card {{
        width: min(520px, calc(100vw - 32px));
        background: rgba(255, 250, 244, 0.95);
        border: 1px solid #d6c2a4;
        border-radius: 22px;
        padding: 28px 24px;
        box-shadow: 0 20px 50px rgba(78, 54, 26, 0.12);
      }}
      h1 {{
        margin: 0 0 10px;
        font-size: 1.5rem;
      }}
      p {{
        margin: 0;
        line-height: 1.6;
        color: #5f5142;
      }}
    </style>
  </head>
  <body>
    <section class="card">
      <h1>{escaped_title}</h1>
      <p>{escaped_message}</p>
    </section>
  </body>
</html>"""


def build_draft_pr_created_blocks(
    *,
    domain: str,
    pr_url: str,
    pr_number: Optional[int] = None,
    route_path: str = "",
    preview_url: str = "",
) -> List[Dict[str, Any]]:
    pr_label = f"PR #{pr_number}" if pr_number else "Draft PR"
    section_text = f"📝 *Draft PR created* for {domain}\n\n*{pr_label}* is ready for review."
    if route_path:
        section_text += f"\nRoute: `{route_path}`"

    actions = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Open PR"},
            "style": "primary",
            "url": pr_url,
        }
    ]
    if preview_url:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open Preview"},
                "url": preview_url,
            }
        )

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": section_text,
            },
        },
        {
            "type": "actions",
            "elements": actions,
        },
    ]


def build_preview_ready_blocks(
    *,
    domain: str,
    pr_url: str,
    preview_url: str,
    pr_number: Optional[int] = None,
    route_path: str = "",
) -> List[Dict[str, Any]]:
    pr_label = f"PR #{pr_number}" if pr_number else "Draft PR"
    section_text = (
        f"✅ *Preview ready* for {domain}\n\n"
        f"*{pr_label}* now has a live preview."
    )
    if route_path:
        section_text += f"\nRoute: `{route_path}`"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": section_text,
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Preview"},
                    "style": "primary",
                    "url": preview_url,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open PR"},
                    "url": pr_url,
                },
            ],
        },
    ]


def build_content_ready_blocks(*, domain: str, content_package: Dict[str, Any], preview_url: str = "") -> List[Dict[str, Any]]:
    title = _string(content_package.get("title")) or "Untitled article"
    summary = _summary_text(content_package) or "The full article is ready below."
    references = _list(content_package.get("references"))
    inline_images = [image for image in _list(content_package.get("inline_images")) if _string(_dict(image).get("url"))]
    hero_image = _dict(content_package.get("hero_image"))
    hero_url = _string(hero_image.get("url"))

    section_text = (
        f"✅ *Article content ready* for {domain}\n\n"
        f"*{title}*\n"
        f"{summary}"
    )
    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": section_text,
            },
        }
    ]
    if hero_url:
        blocks.append(
            {
                "type": "image",
                "image_url": hero_url,
                "alt_text": _string(hero_image.get("alt_text")) or title,
            }
        )
    context_bits = [
        f"*References:* {len(references)}",
        f"*Inline images:* {len(inline_images)}",
    ]
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " • ".join(context_bits)}],
        }
    )
    if preview_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open Preview"},
                        "style": "primary",
                        "url": preview_url,
                    }
                ],
            }
        )
    return blocks


def build_content_thread_messages(content_package: Dict[str, Any]) -> List[Dict[str, Any]]:
    article = _dict(content_package.get("article_json"))
    messages: List[Dict[str, Any]] = []

    summary_items = _list(article.get("summary_items"))
    if summary_items:
        lines = ["*Quick Summary*"]
        for item in summary_items:
            payload = _dict(item)
            label = _string(payload.get("label")) or "Summary"
            description = _string(payload.get("description"))
            if description:
                lines.append(f"• *{label}:* {description}")
        messages.extend(_text_message_series(lines))

    inline_by_section = {}
    for raw_image in _list(content_package.get("inline_images")):
        image = _dict(raw_image)
        if not _string(image.get("url")):
            continue
        key = _string(image.get("section_id"))
        if key:
            inline_by_section[key] = image

    for raw_section in _list(article.get("sections")):
        section = _dict(raw_section)
        section_id = _string(section.get("section_id"))
        heading = _string(section.get("heading")) or "Section"
        body_segments = _section_body_segments(section)
        messages.extend(
            _prefixed_message_series(
                heading=heading,
                body_segments=body_segments,
            )
        )
        inline_image = inline_by_section.get(section_id)
        if inline_image:
            messages.append(_image_message(inline_image, heading))

    faq_items = _list(article.get("faq_items"))
    if faq_items:
        faq_segments: List[str] = []
        for item in faq_items:
            payload = _dict(item)
            question = _string(payload.get("question"))
            answer = _string(payload.get("answer"))
            if not question and not answer:
                continue
            block = f"*Q: {question}*\n{answer}".strip()
            if block:
                faq_segments.append(block)
        messages.extend(
            _prefixed_message_series(
                heading="FAQ",
                body_segments=faq_segments,
            )
        )

    cta = _dict(article.get("cta"))
    if cta:
        cta_lines = ["*Next Step*"]
        title = _string(cta.get("title"))
        body = _string(cta.get("body"))
        button_text = _string(cta.get("button_text"))
        button_href = _string(cta.get("button_href"))
        if title:
            cta_lines.append(title)
        if body:
            cta_lines.append(body)
        if button_text and button_href:
            cta_lines.append(_slack_link(button_href, button_text))
        messages.extend(_text_message_series(cta_lines))

    references = _list(content_package.get("references")) or _list(article.get("references"))
    if references:
        reference_lines = ["*References*"]
        for reference in references:
            payload = _dict(reference)
            title = _string(payload.get("title")) or _string(payload.get("url"))
            url = _string(payload.get("url"))
            if title and url:
                reference_lines.append(f"• {_slack_link(url, title)}")
        messages.extend(_text_message_series(reference_lines))

    return [message for message in messages if message.get("text")]


def _section_body_segments(section: Dict[str, Any]) -> List[str]:
    segments: List[str] = []
    for paragraph in _list(section.get("paragraphs")):
        text = _string(paragraph)
        if text:
            segments.append(text)

    bullets = [f"• {_string(item)}" for item in _list(section.get("bullets")) if _string(item)]
    if bullets:
        segments.append("\n".join(bullets))

    callout = _dict(section.get("callout"))
    if callout:
        title = _string(callout.get("title"))
        body = _string(callout.get("body"))
        callout_lines = []
        if title:
            callout_lines.append(f"*{title}*")
        if body:
            callout_lines.append(body)
        if callout_lines:
            segments.append("\n".join(callout_lines))

    for raw_subsection in _list(section.get("subsections")):
        subsection = _dict(raw_subsection)
        heading = _string(subsection.get("heading"))
        subsection_lines: List[str] = []
        if heading:
            subsection_lines.append(f"*{heading}*")
        for paragraph in _list(subsection.get("paragraphs")):
            text = _string(paragraph)
            if text:
                subsection_lines.append(text)
        bullet_lines = [f"• {_string(item)}" for item in _list(subsection.get("bullets")) if _string(item)]
        if bullet_lines:
            subsection_lines.append("\n".join(bullet_lines))
        if subsection_lines:
            segments.append("\n".join(subsection_lines))

    return segments


def _prefixed_message_series(*, heading: str, body_segments: Iterable[str]) -> List[Dict[str, Any]]:
    segments = [segment.strip() for segment in body_segments if str(segment or "").strip()]
    if not segments:
        return [{"text": f"*{heading}*", "blocks": _mrkdwn_blocks(f"*{heading}*")}]

    initial_prefix = f"*{heading}*\n\n"
    continuation_prefix = f"*{heading}* _(continued)_\n\n"
    chunks = _chunk_segments(
        segments,
        initial_prefix=initial_prefix,
        continuation_prefix=continuation_prefix,
    )
    return [{"text": chunk, "blocks": _mrkdwn_blocks(chunk)} for chunk in chunks]


def _text_message_series(lines: Iterable[str]) -> List[Dict[str, Any]]:
    segments = [str(line).strip() for line in lines if str(line or "").strip()]
    chunks = _chunk_segments(segments, initial_prefix="", continuation_prefix="")
    return [{"text": chunk, "blocks": _mrkdwn_blocks(chunk)} for chunk in chunks]


def _image_message(image: Dict[str, Any], fallback_title: str) -> Dict[str, Any]:
    caption = _string(image.get("caption")) or _string(image.get("section_heading")) or fallback_title
    alt_text = _string(image.get("alt_text")) or caption or fallback_title or "Article image"
    blocks: List[Dict[str, Any]] = []
    if caption:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": caption}})
    blocks.append(
        {
            "type": "image",
            "image_url": _string(image.get("url")),
            "alt_text": alt_text,
        }
    )
    return {"text": caption or alt_text, "blocks": blocks}


def _chunk_segments(
    segments: Iterable[str],
    *,
    initial_prefix: str,
    continuation_prefix: str,
) -> List[str]:
    chunks: List[str] = []
    current_prefix = initial_prefix
    current_segments: List[str] = []
    current_length = len(current_prefix)

    for segment in segments:
        clean_segment = str(segment).strip()
        if not clean_segment:
            continue

        separator_length = 2 if current_segments else 0
        segment_length = len(clean_segment) + separator_length
        if current_segments and current_length + segment_length > SLACK_TEXT_LIMIT:
            chunks.append(current_prefix + "\n\n".join(current_segments))
            current_segments = [clean_segment]
            current_prefix = continuation_prefix
            current_length = len(current_prefix) + len(clean_segment)
            continue

        current_segments.append(clean_segment)
        current_length += segment_length

    if current_segments or not chunks:
        chunks.append(current_prefix + "\n\n".join(current_segments))

    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _mrkdwn_blocks(text: str) -> List[Dict[str, Any]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _summary_text(content_package: Dict[str, Any]) -> str:
    article = _dict(content_package.get("article_json"))
    summary_items = _list(article.get("summary_items"))
    if summary_items:
        summary_fragments = []
        for item in summary_items[:2]:
            payload = _dict(item)
            label = _string(payload.get("label"))
            description = _string(payload.get("description"))
            if label and description:
                summary_fragments.append(f"*{label}:* {description}")
            elif description:
                summary_fragments.append(description)
        if summary_fragments:
            return "\n".join(summary_fragments)
    return _string(content_package.get("meta_description"))


def _slack_link(url: str, label: str) -> str:
    return f"<{url}|{label}>"


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []
