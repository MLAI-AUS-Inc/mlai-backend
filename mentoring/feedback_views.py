"""Public pages behind the post-session feedback links (no sign-in; the token is the proof).

A GET never writes: mail scanners open every link in an email. The page a face
opens submits the rating itself with a small script, so a person gets one-tap
rating while a scanner that only fetches HTML records nothing. Without
JavaScript the page shows a Confirm button instead.

CSRF is off on purpose: nothing here uses a cookie or a session, and only the
holder of the link's token can change its answer.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlparse

from django.http import HttpResponse, HttpResponseRedirect
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import feedback
from .models import MentorSessionFeedback

FACE_SVG = {
    'sad': ('#fee2e2', '#dc2626', 'M19 40 Q28 32 37 40'),
    'neutral': ('#fef3c7', '#b45309', 'M19 38 L37 38'),
    'happy': ('#ccfbf1', '#0f766e', 'M18 34 Q28 45 38 34'),
}

COPY = {
    'happy': ('Thanks, {name}! Glad it was great.', 'Thanks! Glad it was great.',
              'What was most useful?', 'e.g. {mentor} helped me pick a price for the pro plan', True),
    'neutral': ('Thanks, {name}.', 'Thanks.', 'What would have made it better?', '', True),
    'sad': ('Thanks for telling us, {name}.', 'Thanks for telling us.', 'What went wrong?',
            'Anything you tell us goes to the MLAI team, not to {mentor}', False),
    'no_show': ('Sorry about that, {name}.', 'Sorry about that.', 'What happened?',
                'e.g. {mentor} didn’t join the call', False),
}


def _svg(key):
    bg, fg, mouth = FACE_SVG[key]
    return mark_safe(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="56" height="56" viewBox="0 0 56 56" aria-hidden="true">'
        f'<circle cx="28" cy="28" r="27" fill="{bg}"/><circle cx="20" cy="23" r="3.2" fill="{fg}"/>'
        f'<circle cx="36" cy="23" r="3.2" fill="{fg}"/><path d="{mouth}" stroke="{fg}" stroke-width="3.4" '
        f'fill="none" stroke-linecap="round"/></svg>'
    )


def _faces(token):
    return [{'key': key, 'label': feedback.RATINGS[key][0], 'svg': _svg(key),
             'href': feedback.page_url(token, key, MentorSessionFeedback.VIA_PAGE)} for key in feedback.FACES]


def _respond(request, context, status=200):
    nonce = secrets.token_urlsafe(16)
    asset_base = feedback.asset_base()
    parsed = urlparse(asset_base)
    asset_origin = f'{parsed.scheme}://{parsed.netloc}' if parsed.scheme and parsed.netloc else ''
    body = render_to_string('mentoring/feedback_page.html', {
        'asset_base': asset_base, 'nonce': nonce, 'title': 'Session feedback', **context,
    })
    response = HttpResponse(body, status=status)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Content-Security-Policy'] = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'nonce-{nonce}'; img-src 'self' data: {asset_origin}".rstrip() + '; '
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def _message(request, heading, message, status):
    return _respond(request, {'state': 'message', 'heading': heading, 'message': message}, status=status)


def _expired(request):
    return _message(request, 'This feedback link has expired',
                    'You can still tell us how it went at hi@mlai.au.', 410)


def _invalid(request):
    return _message(request, 'This feedback link isn’t valid',
                    'Check you copied the whole link, or tell us how it went at hi@mlai.au.', 404)


def _rated_context(row, info, token, *, commented=False, comment_error=''):
    name = info['founder_first']
    mentor = info['mentor_first']
    heading_named, heading_plain, prompt, placeholder, book = COPY[row.rating]
    return {
        'state': 'rated',
        'heading': heading_named.format(name=name) if name else heading_plain,
        'prompt': prompt,
        'placeholder': placeholder.format(mentor=mentor),
        'book_url': feedback.chat_url('/mentoring') if book else '',
        'rating': row.rating,
        'comment': row.comment,
        'commented': commented,
        'comment_error': comment_error,
    }


def _page(request, row, token, **extra):
    info = feedback.describe(row)
    context = {
        'subheading': f"Your session with {info['mentor_name']}, {info['date_short']}",
        'faces': _faces(token),
        'action': feedback.page_url(token),
        'comment_action': feedback.page_url(token).rstrip('/') + '/comment/',
        'no_show_url': feedback.page_url(token, MentorSessionFeedback.RATING_NO_SHOW, MentorSessionFeedback.VIA_PAGE),
    }
    if row.rating:
        context.update(_rated_context(row, info, token, **extra))
    else:
        mentor = info['mentor_name']
        context.update({'state': 'ask', 'heading': f'How was your session with {mentor}?', 'rating': ''})
    return context


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def feedback_page(request, token):
    try:
        row = feedback.load(token)
    except feedback.FeedbackError:
        return _invalid(request)
    if request.method == 'POST':
        try:
            feedback.record_rating(token, request.POST.get('rating', ''), request.POST.get('src', ''))
        except feedback.FeedbackError as exc:
            if exc.code == 'expired':
                return _expired(request)
            if exc.code == 'invalid_token':
                return _invalid(request)
            return HttpResponseRedirect(feedback.page_url(token), status=303)
        return HttpResponseRedirect(feedback.page_url(token), status=303)
    chosen = request.GET.get('r', '')
    if chosen in feedback.RATINGS:
        if feedback.is_expired(row):
            return _expired(request)
        if row.rating == chosen:
            return _respond(request, _page(request, row, token))
        info = feedback.describe(row)
        return _respond(request, {
            **_page(request, row, token),
            'state': 'confirm',
            'heading': 'Saving your answer…',
            'subheading': f"Your session with {info['mentor_name']}, {info['date_short']}",
            'chosen': chosen,
            'chosen_label': feedback.RATINGS[chosen][0],
            'via': request.GET.get('src', ''),
        })
    return _respond(request, _page(request, row, token))


@csrf_exempt
@require_http_methods(['POST'])
def feedback_comment(request, token):
    try:
        row = feedback.record_comment(token, request.POST.get('comment', ''))
    except feedback.FeedbackError as exc:
        if exc.code == 'expired':
            return _expired(request)
        if exc.code == 'invalid_token':
            return _invalid(request)
        try:
            row = feedback.load(token)
        except feedback.FeedbackError:
            return _invalid(request)
        if not row.rating:
            return _respond(request, _page(request, row, token), status=400)
        return _respond(request, _page(request, row, token, comment_error=exc.message), status=400)
    if not row.rating:
        return HttpResponseRedirect(feedback.page_url(token), status=303)
    return _respond(request, _page(request, row, token, commented=True))


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def feedback_unsubscribe(request, token):
    """GET asks; POST (the button, or a mail client's one-click unsubscribe) opts out."""
    try:
        feedback.load(token)
    except feedback.FeedbackError:
        return _invalid(request)
    if request.method == 'POST':
        feedback.opt_out(token)
        return _message(request, 'Done',
                        'You won’t get any more emails asking how your mentoring sessions went.', 200)
    return _respond(request, {
        'state': 'unsubscribe', 'heading': 'Stop session feedback emails?',
        'action': feedback.unsubscribe_url(token),
    })
