"""Place completed React server-rendered fragments in their static preview slots."""
import re

from bs4 import Comment


# React emits an inline completion call after it streams a segment. The first
# call can follow the runtime definition in the same script. Read literal DOM
# IDs only. Leave the site's JavaScript to the normal preview sanitizer.
_COMPLETE = re.compile(
    r'(?:^|;)\s*\$(RC|RS)\(\s*"([\w.:-]+)"\s*,\s*"([\w.:-]+)"\s*\)\s*(?=;|$)'
)


def _boundary_fallback(slot):
    """Return siblings up to the matching Suspense close, or None if incomplete."""
    opening = slot.previous_sibling
    if not isinstance(opening, Comment) or opening != "$?":
        return None
    fallback = []
    depth = 0
    for sibling in slot.next_siblings:
        if isinstance(sibling, Comment):
            if sibling == "/$":
                if depth == 0:
                    return fallback
                depth -= 1
            elif sibling in {"$", "$?", "$!", "$~"}:
                depth += 1
        fallback.append(sibling)
    return None


def complete_streamed_markup(document):
    if not document.find("template", id=True):
        return
    for script in document.find_all("script", src=False):
        if script.get("type", "").lower() not in {"", "text/javascript", "application/javascript"}:
            continue
        for command, first, second in _COMPLETE.findall(script.string or ""):
            slot_id, segment_id = (first, second) if command == "RC" else (second, first)
            slot_prefix = "B:" if command == "RC" else "P:"
            if not re.fullmatch(r"[\w.-]*" + slot_prefix + r"[0-9a-fA-F]+", slot_id):
                continue
            if not re.fullmatch(r"[\w.-]*S:[0-9a-fA-F]+", segment_id):
                continue
            slot = document.find("template", id=slot_id)
            segment = document.find(id=segment_id, hidden=True)
            if slot is None or segment is None:
                continue
            if any(parent is segment for parent in slot.parents) or any(parent is slot for parent in segment.parents):
                continue
            if command == "RC":
                fallback = _boundary_fallback(slot)
                if fallback is None:
                    continue
                for node in fallback:
                    node.extract()
                slot.previous_sibling.replace_with(Comment("$"))
            for child in list(segment.contents):
                slot.insert_before(child.extract())
            slot.decompose()
            segment.decompose()
