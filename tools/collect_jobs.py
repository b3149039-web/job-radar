"""Collect public jobs and build a report. This command never connects to Supabase."""
import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
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
    body = ' '.join((job.get('requirement_text') or '').split())
    row['qualifications']['source_details'] = {
        'posted_at': job.get('posted_at', ''),
        'updated_at': job.get('source_updated_at', ''),
        'deadline': job.get('deadline', ''),
        'degree': redact_contacts(degree)[:220],
        'experience': redact_contacts(evidence)[:220],
        'requisition': job.get('requisition', ''),
        'campaign': redact_contacts(job.get('campaign_title'))[:160],
        'evidence_hash': sha256(body.encode('utf-8')).hexdigest() if len(body) >= 80 else '',
    }
    return row


def same_position(left, right):
    left_details = left.get('qualifications', {}).get('source_details', {})
    right_details = right.get('qualifications', {}).get('source_details', {})
    for field in ('requisition', 'campaign', 'posted_at', 'deadline'):
        if left_details.get(field) and right_details.get(field) and left_details[field] != right_details[field]:
            return False
    if left['cohort'] and right['cohort'] and left['cohort'] != right['cohort']:
        return False
    if left['recruitment_type'] != right['recruitment_type']:
        return False
    if left['source'] == right['source'] and left_details.get('requisition') and left_details['requisition'] == right_details.get('requisition'):
        return True
    return bool(left_details.get('posted_at') and left_details['posted_at'] == right_details.get('posted_at')
                and left_details.get('evidence_hash') and left_details['evidence_hash'] == right_details.get('evidence_hash'))


def refreshed(existing, row):
    old = existing.get('qualifications') or {}
    fresh = row.get('qualifications') or {}
    qualifications = dict(old)
    for key, value in fresh.items():
        if key == 'source_details':
            qualifications[key] = {**old.get(key, {}), **{k: v for k, v in value.items() if v}}
        elif key == 'cohort_evidence':
            years = {item['year']: item for item in old.get(key, [])}
            years.update({item['year']: item for item in value if item.get('evidence') or item['year'] not in years})
            qualifications[key] = sorted(years.values(), key=lambda item: item['year'])
        elif key == 'social':
            previous = old.get(key, {})
            if key not in old:
                qualifications[key] = value
                continue
            qualifications[key] = {**previous, **{k: v for k, v in value.items()
                if v and v != 'unknown' and (k != 'salary' or v.get('evidence'))}}
        elif isinstance(value, dict) and (value.get('evidence') or key not in old):
            qualifications[key] = value
        elif key == 'attachment_unverified' and (value or key not in old or fresh.get('source_details', {}).get('evidence_hash')):
            qualifications[key] = value
    status = existing['check_status']
    if status not in INACTIVE and row['check_status'] != '在招待确认':
        status = row['check_status']
    return {**existing, 'qualifications': qualifications, 'check_status': status}


def plan(rows, baseline, today=None, *, count_similar=False):
    catalog = {row['id']: row for row in baseline}
    urls = {normalize_url(row['url']): row['id'] for row in baseline}
    positions = {}
    for row in baseline:
        positions.setdefault(identity(row), []).append(row['id'])
    seen = set()
    counts = Counter(dict(new=0, updated=0, duplicates=0, inactive=0, needs_review=0))
    if today is not None:
        counts['newly_published_7d'] = 0
    if count_similar:
        counts['same_title_unconfirmed'] = 0
    changes = []
    for row in sorted(rows, key=lambda item: item['id'] not in catalog):
        existing = catalog.get(urls.get(normalize_url(row['url']), row['id']))
        exact = existing is not None
        if not exact and (key := identity(row)):
            matches = [catalog[job] for job in positions.get(key, ()) if same_position(row, catalog[job])]
            if matches:
                counts['duplicates'] += 1
                continue
            if count_similar and positions.get(key):
                counts['same_title_unconfirmed'] += 1
        stable_id = existing['id'] if existing else row['id']
        if stable_id in seen:
            counts['duplicates'] += 1
            continue
        seen.add(stable_id)
        status = row['check_status']
        details = row['qualifications'].get('source_details', {})
        if today is not None and details.get('posted_at'):
            try:
                counts['newly_published_7d'] += 0 <= (today - datetime.fromisoformat(details['posted_at']).date()).days <= 7
            except (TypeError, ValueError):
                pass
        effective_status = existing['check_status'] if existing and existing['check_status'] in INACTIVE else status
        counts['inactive'] += effective_status in INACTIVE
        counts['needs_review'] += effective_status not in INACTIVE and (
            effective_status == '在招待确认' or row['recruitment_type'] == 'unknown'
            or not all(details.get(field) for field in ('posted_at', 'degree', 'experience'))
            or (row['recruitment_type'] == 'campus' and not row['cohort'])
        )
        if existing:
            revised = refreshed(existing, row)
            if revised != existing:
                changes.append(('updated', revised))
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
        else:
            # Historical rechecks may recover qualifications without changing the original ID.
            if checked and checked.get('requirement_text'):
                prior_details = old.get('qualifications', {}).get('source_details', {})
                merged = {**old, **checked, 'company': checked.get('company') or old['company'],
                          'degree_requirement': checked.get('degree_requirement') or prior_details.get('degree', ''),
                          'url': old['url'], 'posted_at': checked.get('posted_at') or prior_details.get('posted_at', '')}
                yield {**payload(merged, old['first_seen']), 'id': old['id']}
                continue
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
    counts, changes = plan(rows, baseline, moment.date(), count_similar=True)
    if not args.baseline:
        counts['unique_candidates'] = counts.pop('new')
        counts.pop('updated')
    result = dict(collected_at=moment.isoformat(), baseline_count=len(baseline) if args.baseline else None, sources=runs,
                  warnings=warnings, extracted=extracted, counts=counts, rows=rows,
                  first_discovered_against_baseline=counts.get('new') if args.baseline else None,
                  planned_ids={key: [row['id'] for kind, row in changes if kind == key] for key in ('new', 'updated')}
                  if args.baseline else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key not in ('rows', 'planned_ids')}, ensure_ascii=False))
    if not runs:
        raise SystemExit('No recruitment source completed successfully.')


if __name__ == '__main__':
    main()
