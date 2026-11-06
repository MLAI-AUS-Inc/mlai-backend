"""Signed email preference links: inspection is read-only; a CSRF POST confirms."""

from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.utils.html import format_html
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from crm.blasts import BlastError
from crm.deliverability import reconfirmation


@require_http_methods(['GET', 'POST'])
@csrf_protect
def reconfirm(request):
    """Record only a deliberate confirmation; scanners cannot confirm through GET."""
    token = request.GET.get('token', '') if request.method == 'GET' else request.POST.get('token', '')
    if request.method == 'GET' and request.GET.get('preview') == '1':
        result = 'preview'
    else:
        try:
            result = reconfirmation.confirm(token, apply=request.method == 'POST')
        except BlastError:
            result = 'unavailable'
    if result == 'ready':
        content = format_html('<p>Would you like to keep receiving MLAI community emails?</p>'
            '<form method="post" action="{}"><input type="hidden" name="csrfmiddlewaretoken" value="{}">'
            '<input type="hidden" name="token" value="{}"><button type="submit">Yes, keep me subscribed</button></form>'
            '<p>You can unsubscribe at any time using the link in an MLAI email.</p>',
            reconfirmation.PATH, get_token(request), token)
    elif result in ('confirmed', 'already_confirmed'):
        content = format_html('<p>{}</p>', 'Thanks. Your email preference has been recorded.')
    elif result == 'preview':
        content = format_html('<p>{}</p>', 'This is an inbox-check preview. No email preference has been changed.')
    else:
        content = format_html('<p>{}</p>', 'This preference link is no longer available. Your current email preferences have been kept.')
    response = HttpResponse(format_html('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>MLAI email preferences</title><style>body{{font:1rem system-ui;max-width:32rem;margin:4rem auto;padding:1.5rem;line-height:1.5}}'
        'button{{font:inherit;padding:.75rem 1rem;border-radius:.5rem;background:#222;color:#fff;border:0;cursor:pointer}}</style>'
        '</head><body><h1>MLAI email preferences</h1>{}</body></html>', content), status=400 if result == 'unavailable' else 200)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    return response
