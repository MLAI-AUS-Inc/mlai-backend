"""Allow only an opaque startup article-sheet return, never an arbitrary URL."""
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID


def article_return_path(value):
    """Return a canonical article-sheet URL or None for untrusted metadata."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != '/my-startup/articles':
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len(pairs) != 2 or {key for key, _ in pairs} != {'company_id', 'articlePlan'}:
        return None
    fields = dict(pairs)
    try:
        if any(str(UUID(fields[key])) != fields[key].lower() for key in fields):
            return None
    except (ValueError, AttributeError, TypeError):
        return None
    return '/my-startup/articles?' + urlencode({'company_id': fields['company_id'], 'articlePlan': fields['articlePlan']})


def purchase_return_fields(purchase):
    """UI return metadata carries no payment confirmation or article authority."""
    origin = purchase.purchase_from or {}
    if not isinstance(origin, dict) or origin.get('surface') != 'my-startup':
        return {}
    target = article_return_path(origin.get('return_path'))
    if not target:
        return {}
    return {'returnTo': target, 'returnRuntime': 'tauri' if origin.get('return_runtime') == 'tauri' else 'browser'}
