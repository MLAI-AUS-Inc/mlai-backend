#!/usr/bin/env python3
"""Watch the prevention jobs themselves (plan step F3).

31 red nights and every skipped weekly canary went unnoticed because nothing
looked at the scheduled jobs. This script turns red when any watched workflow
has been red, skipped, stuck or silent for N days (default 2).

The decision is a pure function, ``evaluate``, over recorded GitHub Actions
workflow-run lists; the live read (``fetch_runs``) is a thin, read-only call
with the job's own ``GITHUB_TOKEN``. Alerts go to the job summary only unless
the ``PREVENTION_WATCH_ALERT_MODE`` repository variable is set to ``slack``
(report-only is the default, so nothing pages by accident).

The same file is kept in each repository that runs prevention jobs
(content-factory and mlai-backend ``scripts/prevention_watch.py``, mlai-chat
``scripts/prevention-watch.py``) because each workflow token reads only its own
repository's runs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

DEFAULT_STALE_DAYS = 2
MODES = ('report-only', 'slack')
RED = {'failure', 'timed_out', 'cancelled', 'startup_failure', 'action_required', 'stale', 'neutral'}
SKIPPED = {'skipped'}
MAX_BYTES = 4 * 1024 * 1024


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days(delta):
    return round(delta.total_seconds() / 86400, 2)


def _outcome(run, now, stale_after):
    """Map one run to success, red, skipped, stuck or None (still in flight)."""
    if run.get('status') != 'completed':
        created = parse_time(run.get('created_at'))
        # A run waiting on an environment approval or a runner for N days is not coverage.
        if created is not None and now - created >= stale_after:
            return 'stuck'
        return None
    conclusion = run.get('conclusion')
    if conclusion == 'success':
        return 'success'
    if conclusion in SKIPPED:
        return 'skipped'
    return 'red'


def evaluate_entry(entry, runs, now, stale_after_days=DEFAULT_STALE_DAYS):
    """Decide one watched workflow. ``runs`` is the API's run list, or None when unreadable."""
    stale_after = timedelta(days=stale_after_days)
    base = {'id': entry['id'], 'workflow': entry['workflow'], 'repo': entry.get('repo'),
            'scheduled': entry.get('cadence_days') is not None, 'alert': False, 'state': 'green',
            'last_success_at': None, 'bad_since': None, 'bad_days': None, 'bad_runs': 0,
            'latest_conclusion': None, 'latest_url': None, 'detail': ''}
    if runs is None:
        return {**base, 'alert': True, 'state': 'unreadable',
                'detail': 'Workflow runs could not be read; an unwatched job counts as red'}
    events = set(entry.get('events') or ['schedule'])
    branch = entry.get('branch')
    selected = []
    for run in runs:
        if not isinstance(run, dict) or run.get('event') not in events:
            continue
        if branch and run.get('head_branch') != branch:
            continue
        created = parse_time(run.get('created_at'))
        if created is None or created > now:
            continue
        outcome = _outcome(run, now, stale_after)
        if outcome is not None:
            selected.append((created, outcome, run))
    selected.sort(key=lambda item: item[0])
    cadence = entry.get('cadence_days')
    if not selected:
        if cadence is None:
            return {**base, 'state': 'no_runs', 'detail': 'Not scheduled and no recorded runs (informational)'}
        return {**base, 'alert': True, 'state': 'not_running',
                'detail': 'No scheduled run in the recorded history; the schedule may be disabled'}
    latest_at, latest_outcome, latest = selected[-1]
    base.update(latest_conclusion=latest.get('conclusion') or latest_outcome, latest_url=latest.get('html_url'))
    successes = [created for created, outcome, _ in selected if outcome == 'success']
    if successes:
        base['last_success_at'] = successes[-1].isoformat()
    if latest_outcome == 'success':
        quiet = now - latest_at
        if cadence is not None and quiet >= timedelta(days=cadence) + stale_after:
            return {**base, 'alert': True, 'state': 'not_running', 'bad_since': latest_at.isoformat(),
                    'bad_days': _days(quiet), 'detail': f'Last scheduled run was {_days(quiet)} days ago'}
        return {**base, 'detail': 'Latest run passed'}
    streak = [(created, outcome) for created, outcome, _ in selected
              if not successes or created > successes[-1]]
    since = streak[0][0]
    outcomes = {outcome for _, outcome in streak}
    state = 'skipped' if outcomes == {'skipped'} else ('stuck' if outcomes == {'stuck'} else 'red')
    bad_for = now - since
    alert = bad_for >= stale_after
    if entry.get('informational'):
        # Dispatch-only jobs (for example the fleet while its gate is off) are shown, never alerted.
        return {**base, 'state': state + '_informational', 'bad_since': since.isoformat(),
                'bad_days': _days(bad_for), 'bad_runs': len(streak),
                'detail': f'{state.capitalize()} for {_days(bad_for)} days (informational, never alerts)'}
    words = {'skipped': 'skipped', 'stuck': 'stuck', 'red': 'red'}[state]
    detail = (f'{words.capitalize()} for {_days(bad_for)} days across {len(streak)} run(s)'
              + ('' if alert else f' (alerts at {stale_after_days} days)'))
    return {**base, 'alert': alert, 'state': state if alert else state + '_recent',
            'bad_since': since.isoformat(), 'bad_days': _days(bad_for), 'bad_runs': len(streak), 'detail': detail}


def evaluate(config, runs_by_id, now, stale_after_days=None):
    """Pure decision over recorded run lists keyed by watch entry id."""
    if stale_after_days is None:
        stale_after_days = config.get('stale_after_days', DEFAULT_STALE_DAYS)
    if type(stale_after_days) is not int or stale_after_days < 1:
        raise ValueError('stale_after_days must be a positive whole number of days')
    entries = config.get('workflows')
    if not isinstance(entries, list) or not entries:
        raise ValueError('The watch list is empty; an empty watch can never turn red')
    rows = [evaluate_entry(entry, runs_by_id.get(entry['id']), now, stale_after_days) for entry in entries]
    alerts = [row for row in rows if row['alert']]
    return {'schema_version': 1, 'generated_at': now.isoformat(), 'stale_after_days': stale_after_days,
            'status': 'red' if alerts else 'green', 'alerts': [row['id'] for row in alerts], 'entries': rows}


def summary(report, *, title='Prevention watch'):
    head = 'RED' if report['status'] == 'red' else 'green'
    lines = [f'## {title}: {head}', '',
             f"A job red, skipped, stuck or silent for {report['stale_after_days']} days alerts.", '',
             '| Job | State | Since | Detail |', '| --- | --- | --- | --- |']
    for row in report['entries']:
        name = f"`{row['workflow']}`" + (f" ({row['repo']})" if row.get('repo') else '')
        if row.get('latest_url'):
            name = f"[{name}]({row['latest_url']})"
        marker = '**' + row['state'] + '**' if row['alert'] else row['state']
        lines.append(f"| {name} | {marker} | {row['bad_since'] or ''} | {row['detail']} |")
    return '\n'.join(lines) + '\n'


def fetch_runs(repo, entry, token, *, opener=None):
    """Read-only list of a workflow's recent runs; None when it cannot be read."""
    params = {'per_page': 50}
    events = entry.get('events') or ['schedule']
    if len(events) == 1:
        params['event'] = events[0]
    if entry.get('branch'):
        params['branch'] = entry['branch']
    url = ('https://api.github.com/repos/' + quote(repo, safe='/') + '/actions/workflows/'
           + quote(entry['workflow'], safe='') + '/runs?' + urlencode(params))
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    opener = opener or build_opener()
    try:
        with opener.open(Request(url, headers=headers), timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        value = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    runs = value.get('workflow_runs') if isinstance(value, dict) else None
    return runs if isinstance(runs, list) else None


def resolve_mode(value):
    """Unknown or empty modes fall back to report-only: never page by accident."""
    value = (value or '').strip().lower()
    return value if value in MODES else 'report-only'


def page(mode, title, text, *, webhook='', opener=None, summary_path=None):
    """Deliver an alert. report-only writes the job summary; slack posts to the webhook."""
    mode = resolve_mode(mode)
    target = summary_path or os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a', encoding='utf-8') as handle:
            handle.write(f'### {title}\n\n{text}\n')
    if mode == 'report-only':
        return 'report-only'
    if not webhook.startswith('https://'):
        raise RuntimeError('Alert mode is slack but its webhook secret is empty or not HTTPS')
    body = json.dumps({'text': f'*{title}*\n{text}'[:3500]}).encode()
    opener = opener or build_opener()
    with opener.open(Request(webhook, data=body, method='POST',
                             headers={'Content-Type': 'application/json'}), timeout=20) as response:
        if not 200 <= getattr(response, 'status', 200) < 300:
            raise RuntimeError('Alert webhook rejected the message')
    return 'slack'


def _alert_text(report):
    rows = [row for row in report['entries'] if row['alert']]
    return '\n'.join(f"- {row['workflow']}{' (' + row['repo'] + ')' if row.get('repo') else ''}: "
                     f"{row['state']}, {row['detail']}" for row in rows)


def _stale_days(value, config):
    if value in (None, ''):
        return config.get('stale_after_days', DEFAULT_STALE_DAYS)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SystemExit('PREVENTION_WATCH_STALE_DAYS must be a whole number of days') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='Evaluate the watch list and turn red on any alert')
    check.add_argument('--config', type=Path, required=True)
    check.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', ''))
    check.add_argument('--runs-fixture', type=Path,
                       help='Recorded runs keyed by watch id (offline; null marks a run list as unreadable)')
    check.add_argument('--output', type=Path, required=True)
    check.add_argument('--stale-days', default=os.environ.get('PREVENTION_WATCH_STALE_DAYS'))
    check.add_argument('--mode', default=os.environ.get('PREVENTION_WATCH_ALERT_MODE', 'report-only'))
    check.add_argument('--now', help='ISO time override for replaying a recorded day')
    notify = commands.add_parser('page', help='Page (or, by default, only report) a red job')
    notify.add_argument('--mode', default='report-only')
    notify.add_argument('--title', required=True)
    notify.add_argument('--text', required=True)
    notify.add_argument('--webhook-env', default='PREVENTION_WATCH_SLACK_WEBHOOK')
    args = parser.parse_args(argv)
    if args.command == 'page':
        delivered = page(args.mode, args.title, args.text, webhook=os.environ.get(args.webhook_env, ''))
        print(f'Alert delivered as {delivered}')
        return 0
    config = json.loads(args.config.read_text())
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit('--now must be an ISO time')
    if args.runs_fixture:
        recorded = json.loads(args.runs_fixture.read_text())
        runs_by_id = {key: (value.get('workflow_runs') if isinstance(value, dict) else value)
                      for key, value in recorded.items()}
    else:
        token = os.environ.get('GITHUB_TOKEN', '')
        runs_by_id = {entry['id']: fetch_runs(entry.get('repo') or args.repo, entry, token)
                      for entry in config.get('workflows') or []}
    try:
        report = evaluate(config, runs_by_id, now, _stale_days(args.stale_days, config))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    text = summary(report)
    sys.stdout.write(text)
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a', encoding='utf-8') as handle:
            handle.write(text)
    if report['status'] == 'red':
        for row in report['entries']:
            if row['alert']:
                print(f"::error title=Prevention job {row['state']}::{row['workflow']}: {row['detail']}")
        mode = resolve_mode(args.mode)
        if mode != 'report-only':
            page(mode, 'Prevention jobs need attention', _alert_text(report),
                 webhook=os.environ.get('PREVENTION_WATCH_SLACK_WEBHOOK', ''), summary_path=os.devnull)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
