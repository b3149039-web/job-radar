"""Collect public jobs and build a report. This command never connects to Supabase."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sources
from supabase.generate_seed import public_row, redact_contacts
from job_identity import identity, job_id, normalize_url

INACTIVE = {'已结束', '已过期', '无法访问'}


def payload(job, moment):
    url = normalize_url(job['url'])
    row = public_row({**job, 'id': job_id(url), 'first_seen': moment})
    evidence = job.get('experience_requirement') or next((
        line for line in job.get('requirement_text', '').splitlines()
        if re.search(r'\d+\s*年.{0,24}(?:经验|经历)|(?:无需|不限).{0,8}(?:经验|经历)', line)
    ), '')
    degree = job.get('degree_requirement') or next((
        line for line in job.get('requirement_text', '').splitlines()
        if re.search(r'(?:学历|学位).{0,10}(?:本科|硕士|博士|不限)|(?:本科|硕士|博士).{0,5}(?:及以上|学历|学位)', line)
    ), '')
    row['qualifications']['source_details'] = {
        'posted_at': job.get('posted_at', ''),
        'deadline': job.get('deadline', ''),
        'degree': redact_contacts(degree)[:220],
        'experience': redact_contacts(evidence)[:220],
    }
    return row


def same_position(left, right):
    # Cross-posts need corroborating cohort or published date. Different cycles stay distinct.
    if left['cohort'] and right['cohort']:
        return left['cohort'] == right['cohort']
    left_details = left.get('qualifications', {}).get('source_details', {})
    right_details = right.get('qualifications', {}).get('source_details', {})
    return bool(left_details.get('posted_at') and left_details['posted_at'] == right_details.get('posted_at')
                and left['recruitment_type'] == right['recruitment_type'])


def plan(rows, baseline):
    catalog = {row['id']: row for row in baseline}
    urls = {normalize_url(row['url']): row['id'] for row in baseline}
    positions = {}
    for row in baseline:
        positions.setdefault(identity(row), []).append(row['id'])
    seen = set()
    counts = Counter(dict(new=0, updated=0, duplicates=0, inactive=0, needs_review=0))
    changes = []
    for row in sorted(rows, key=lambda item: item['id'] not in catalog):
        existing = catalog.get(urls.get(normalize_url(row['url']), row['id']))
        exact = existing is not None
        if not exact and (key := identity(row)):
            matches = [catalog[job] for job in positions.get(key, ()) if same_position(row, catalog[job])]
            if matches:
                counts['duplicates'] += 1
                continue
        if row['id'] in seen:
            counts['duplicates'] += 1
            continue
        seen.add(row['id'])
        status = row['check_status']
        details = row['qualifications']['source_details']
        effective_status = existing['check_status'] if existing and existing['check_status'] in INACTIVE else status
        counts['inactive'] += effective_status in INACTIVE
        counts['needs_review'] += effective_status not in INACTIVE and (
            effective_status == '在招待确认' or row['recruitment_type'] == 'unknown'
            or not all(details.get(field) for field in ('posted_at', 'degree', 'experience'))
            or (row['recruitment_type'] == 'campus' and not row['cohort'])
        )
        if existing:
            if status in INACTIVE and existing['check_status'] not in INACTIVE:
                changes.append(('updated', {**row, 'id': existing['id'], 'url': existing['url']}))
                counts['updated'] += 1
            else:
                counts['duplicates'] += 1
        else:
            counts['new'] += 1
            changes.append(('new', row))
            catalog[row['id']] = row
            urls[normalize_url(row['url'])] = row['id']
            positions.setdefault(identity(row), []).append(row['id'])
    return dict(counts), changes


def historical_rows(path):
    records = json.loads(path.read_text(encoding='utf-8'))['jobs']
    return [{**payload(job, job.get('first_seen', '')), 'id': job['id'], 'url': job['url']} for job in records]


def rechecked_rows(reader, baseline, rows, cities, today, warn, blocked=()):
    current = {normalize_url(row['url']) for row in rows}
    stopped = set(blocked)
    checks = 0
    for old in baseline:
        if normalize_url(old['url']) in current or old['source'] in stopped or old['check_status'] in INACTIVE:
            continue
        if checks >= 6:
            break
        checks += 1
        try:
            checked = sources.recheck_public(reader, old, cities, today)
            status = checked and checked['check_status']
        except HTTPError as exc:
            warn(old['source'], old['url'], exc)
            if exc.code in (401, 403, 429):
                stopped.add(old['source'])
            status = '无法访问' if exc.code in (404, 410) else None
        except (OSError, ValueError) as exc:
            warn(old['source'], old['url'], exc)
            if sources.access_blocked(exc):
                stopped.add(old['source'])
            status = None
        if status in INACTIVE:
            yield {**old, 'check_status': status}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, help='Optional local jobs.json for dry-run comparison')
    args = parser.parse_args()
    config = json.loads((ROOT / 'daily_sources.json').read_text(encoding='utf-8'))
    reader = sources.PublicReader()
    moment = datetime.now(sources.CST)
    runs, warnings = [], []

    def report(source, scope, returned, accepted):
        runs.append(dict(source=source, scope=scope, returned=returned, accepted=accepted))

    def warn(source, scope, error):
        warnings.append(dict(source=source, scope=scope,
                             error=f'HTTP {error.code}' if hasattr(error, 'code') else str(error),
                             blocked=sources.access_blocked(error)))

    rows = []
    for adapter in (sources.collect_ncss, sources.collect_uibe, sources.collect_jnj):
        for job in adapter(reader, config, moment.date(), report, warn):
            if job.get('company') and job.get('title') and job.get('city'):
                rows.append(payload(job, moment.isoformat(timespec='seconds')))
    baseline = []
    extracted = len(rows)
    if args.baseline:
        baseline = historical_rows(args.baseline)
        blocked = {warning['source'] for warning in warnings if warning['blocked']}
        rows.extend(rechecked_rows(reader, baseline, rows, config['preferred_cities'], moment.date(), warn, blocked))
    counts, changes = plan(rows, baseline)
    if not args.baseline:
        counts['unique_candidates'] = counts.pop('new')
        counts.pop('updated')
    result = dict(collected_at=moment.isoformat(), baseline_count=len(baseline) if args.baseline else None, sources=runs,
                  warnings=warnings, extracted=extracted, counts=counts, rows=rows,
                  planned_ids={key: [row['id'] for kind, row in changes if kind == key] for key in ('new', 'updated')}
                  if args.baseline else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key not in ('rows', 'planned_ids')}, ensure_ascii=False))
    if not runs:
        raise SystemExit('No recruitment source completed successfully.')


if __name__ == '__main__':
    main()
