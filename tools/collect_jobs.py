"""Collect public jobs and build a report. This command never connects to Supabase."""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from functools import partial
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
ADAPTERS = {
    'ncss': (sources.NCSS_NAME, sources.collect_ncss),
    'uibe': (sources.UIBE_NAME, sources.collect_uibe),
    'jnj': (sources.JNJ_NAME, sources.collect_jnj),
    'lilly': (sources.LILLY_NAME, sources.collect_lilly),
    'asml': (sources.ASML_NAME, sources.collect_asml),
    'hebut': (sources.HEBUT_NAME, sources.collect_hebut),
}
PILOT_ADAPTERS = {
    'ebanx': ('EBANX官方招聘', partial(sources.collect_greenhouse, board='ebanx')),
    'glg': ('GLG官方招聘', partial(sources.collect_greenhouse, board='gersonlehrmangroup')),
    'shgzw': (sources.SHGZW_NAME, sources.collect_shgzw),
    'airwallex': (sources.AIRWALLEX_NAME, sources.collect_airwallex),
    'tju': (sources.TJU_NAME, sources.collect_tju),
}
GREENHOUSE_SOURCES = {PILOT_ADAPTERS[key][0] for key in ('ebanx', 'glg')}


def posting_key(row):
    url = normalize_url(row['url'])
    if row.get('source') == sources.TJU_NAME:
        # One campus notice can contain multiple distinct positions and shares one original link.
        return json.dumps([url, row['company'], row['title'], row['city']], ensure_ascii=False)
    return url


def stable_job_id(row):
    return job_id(posting_key(row))


def payload(job, moment):
    url = normalize_url(job['url'])
    row = public_row({**job, 'id': stable_job_id(job), 'first_seen': moment})
    evidence = row['qualifications']['experience']['evidence'] or job.get('experience_requirement') or next((
        line for line in job.get('requirement_text', '').splitlines()
        if re.search(r'\d+\s*年.{0,24}(?:经验|经历)|(?:无需|不限).{0,8}(?:经验|经历)', line)
    ), '')
    degree = job.get('degree_requirement') or next((
        line for line in job.get('requirement_text', '').splitlines()
        if re.search(r'(?:学历|学位).{0,10}(?:本科|硕士|博士|不限)|(?:本科|硕士|博士).{0,5}(?:及以上|学历|学位)', line)
    ), '')
    body = ' '.join((job.get('requirement_text') or '').split())
    group_body = ' '.join((job.get('grouping_body') or '').split())
    is_ncss = job.get('source') == sources.NCSS_NAME
    posted = job.get('source_posted_at') or ('' if is_ncss else job.get('posted_at', ''))
    row['qualifications']['source_details'] = {
        'posted_at': posted,  # Compatibility for existing report consumers.
        'source_posted_at': posted,
        'source_updated_at': job.get('source_updated_at', ''),
        **({'engagement': job['engagement']} if job.get('engagement') else {}),
        'source_update_label': job.get('source_update_label', ''),
        'platform_listed_at': job.get('platform_listed_at', ''),
        'date_evidence': '平台列表及详情' if is_ncss else '来源详情',
        **({'workplace_evidence': job['workplace_evidence']} if job.get('workplace_evidence') else {}),
        'publisher': redact_contacts(job.get('publisher'))[:100],
        'platform_posting_id': job.get('platform_posting_id', ''),
        'department': redact_contacts(job.get('department'))[:100],
        'batch': redact_contacts(job.get('batch'))[:100],
        'headcount': job.get('headcount'),
        'salary_display': redact_contacts(job.get('salary_display'))[:100],
        'grouping_hash': sha256(group_body.encode('utf-8')).hexdigest() if len(group_body) >= 50 else '',
        'status_evidence': job.get('status_evidence') or ('详情明确显示截止或结束' if job.get('check_status') in INACTIVE
                            else '详情状态待核验'),
        'status_checked_at': job.get('status_checked_at', moment),
        **({'status_evidence_url': job['status_evidence_url']} if job.get('status_evidence_url') else {}),
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
    if sources.NCSS_NAME in (left['source'], right['source']):
        # Distinct NCSS posting URLs remain separate rows for display grouping.
        return False
    if (left['source'] == right['source'] and left['source'] in GREENHOUSE_SOURCES
            and left_details.get('platform_posting_id') and right_details.get('platform_posting_id')):
        return left_details['platform_posting_id'] == right_details['platform_posting_id']
    for field in ('requisition', 'campaign', 'posted_at', 'deadline', 'batch', 'department', 'degree', 'headcount'):
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
            previous = old.get(key, {})
            # A fresh crawl alone is not a material update, so replay remains idempotent.
            merged = {**previous, **{k: v for k, v in value.items() if v and k not in ('status_checked_at', 'status_evidence')}}
            if existing['source'] == sources.NCSS_NAME and 'source_posted_at' in value:
                if 'source_posted_at' not in previous and previous.get('posted_at') and not merged.get('platform_listed_at'):
                    merged['platform_listed_at'] = previous['posted_at']
                merged['source_posted_at'] = value['source_posted_at']
                merged['posted_at'] = value['source_posted_at']
                if value.get('source_update_label') and value['source_update_label'] != previous.get('source_update_label'):
                    merged['source_updated_at'] = value.get('source_updated_at', '')
            if 'grouping_hash' in value:
                for field in ('grouping_hash', 'headcount', 'salary_display', 'degree', 'batch', 'department',
                              'requisition', 'campaign'):
                    if field in value:
                        merged[field] = value[field]
            terminal = existing['check_status'] in INACTIVE
            if not terminal or row['check_status'] == existing['check_status']:
                if value.get('status_evidence'):
                    merged['status_evidence'] = value['status_evidence']
                checked = value.get('status_checked_at', '')
                prior = previous.get('status_checked_at', '')
                renew_open_evidence = False
                if row['check_status'] == '官网显示可投递' and value.get('status_evidence') in (
                        '企业官网岗位页显示投递入口', '招聘官网公开职位数据含该岗位的申请入口'):
                    try:
                        renew_open_evidence = date.fromisoformat(checked[:10]) - date.fromisoformat(prior[:10]) >= timedelta(days=7)
                    except (TypeError, ValueError):
                        pass
                if value.get('status_checked_at') and (merged != previous or row['check_status'] != existing['check_status']
                                                       or not previous.get('status_checked_at') or renew_open_evidence):
                    merged['status_checked_at'] = value['status_checked_at']
            qualifications[key] = merged
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
            if value.get('solo_legal') is False and value.get('solo_evidence'):
                qualifications[key]['solo_legal'] = False
        elif isinstance(value, dict) and (value.get('evidence') or key not in old):
            qualifications[key] = value
        elif key == 'attachment_unverified' and (value or key not in old or fresh.get('source_details', {}).get('evidence_hash')):
            qualifications[key] = value
    status = existing['check_status']
    if status not in INACTIVE and (row['check_status'] != '在招待确认' or fresh.get('source_details', {}).get('status_checked_at')):
        status = row['check_status']
    return {**existing, 'qualifications': qualifications, 'check_status': status}


def greenhouse_position(row):
    source = row.get('source')
    if source in GREENHOUSE_SOURCES:
        identifier = (row.get('qualifications') or {}).get('source_details', {}).get('platform_posting_id', '')
        if re.fullmatch(r'[0-9]+', str(identifier)):
            return source, str(identifier)
    return None


def plan(rows, baseline, today=None, *, count_similar=False, by_source=None):
    catalog = {row['id']: row for row in baseline}
    urls = {posting_key(row): row['id'] for row in baseline}
    greenhouse_posts = {key: row['id'] for row in baseline if (key := greenhouse_position(row))}
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

    def count(row, key):
        counts[key] += 1
        if by_source is not None:
            by_source[row['source']][key] += 1

    for row in sorted(rows, key=lambda item: item['id'] not in catalog):
        platform_key = greenhouse_position(row)
        existing = catalog.get(urls.get(posting_key(row), greenhouse_posts.get(platform_key, row['id'])))
        exact = existing is not None
        if not exact and (key := identity(row)):
            matches = [catalog[job] for job in positions.get(key, ()) if same_position(row, catalog[job])]
            if matches:
                count(row, 'duplicates')
                continue
            if count_similar and positions.get(key):
                counts['same_title_unconfirmed'] += 1
        stable_id = existing['id'] if existing else row['id']
        if stable_id in seen:
            count(row, 'duplicates')
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
        if effective_status in INACTIVE:
            count(row, 'inactive')
        elif (
            effective_status == '在招待确认' or row['recruitment_type'] == 'unknown'
            or not all(details.get(field) for field in ('posted_at', 'degree', 'experience'))
            or (row['recruitment_type'] == 'campus' and not row['cohort'])
        ):
            count(row, 'needs_review')
        if existing:
            revised = refreshed(existing, row)
            if revised != existing:
                changes.append(('updated', revised))
                count(row, 'updated')
            else:
                count(row, 'duplicates')
        else:
            count(row, 'new')
            changes.append(('new', row))
            catalog[row['id']] = row
            urls[posting_key(row)] = row['id']
            if platform_key:
                greenhouse_posts[platform_key] = row['id']
            positions.setdefault(identity(row), []).append(row['id'])
    return dict(counts), changes


def historical_rows(path):
    contents = json.loads(path.read_text(encoding='utf-8'))
    if 'jobs' not in contents:
        # A prior public collection report already contains normalized rows and their evidence.
        return contents['rows']
    records = contents['jobs']
    return [{**payload(job, job.get('first_seen', '')), 'id': job['id'], 'url': job['url']} for job in records]


def rechecked_rows(reader, baseline, rows, cities, today, warn, blocked=()):
    current = {normalize_url(row['url']) for row in rows}
    stopped = set(blocked)
    checks = 0
    for old in sorted(baseline, key=lambda row: bool(
            row.get('qualifications', {}).get('source_details', {}).get('grouping_hash'))):
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
                          'degree_requirement': checked.get('degree_requirement', ''),
                          'url': old['url'], 'posted_at': checked.get('posted_at') or prior_details.get('posted_at', ''),
                          'status_checked_at': datetime.now(sources.CST).isoformat(timespec='seconds')}
                yield {**payload(merged, old['first_seen']), 'id': old['id']}
                continue
        if status in INACTIVE:
            evidence = '原详情返回 HTTP 404 或 410' if status == '无法访问' else '原详情明确截止或结束'
            qualifications = old.get('qualifications', {})
            yield {**old, 'check_status': status, 'qualifications': {**qualifications, 'source_details': {
                **qualifications.get('source_details', {}), 'status_evidence': evidence,
                'status_checked_at': datetime.now(sources.CST).isoformat(timespec='seconds'),
            }}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, help='Local jobs.json or prior collection report for dry-run comparison')
    adapters = {**ADAPTERS, **PILOT_ADAPTERS}
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--source', choices=adapters, action='append', help='Only run selected public sources')
    choice.add_argument('--pilot', action='store_true', help='Collect only the sources under trial')
    parser.add_argument('--expand-ncss', action='store_true',
                        help='Try up to three pages for selected Shanghai NCSS queries with new positions')
    args = parser.parse_args()
    config = json.loads((ROOT / 'daily_sources.json').read_text(encoding='utf-8'))
    if args.expand_ncss:
        config['ncss']['second_page'] = True
    reader = sources.PublicReader()
    moment = datetime.now(sources.CST)
    runs, warnings = [], []

    def report(source, scope, returned, accepted, **details):
        runs.append(dict(source=source, scope=scope, returned=returned, accepted=accepted, **details))

    def warn(source, scope, error):
        if isinstance(error, HTTPError):
            reason = f'HTTP {error.code}'
        elif sources.access_blocked(error):
            reason = '公开访问规则限制'
        elif isinstance(error, OSError):
            reason = '网络或响应超时'
        else:
            reason = '来源格式或详情核验失败'
        warnings.append(dict(source=source, scope=scope,
                             error=reason,
                             blocked=sources.access_blocked(error)))

    rows = []
    selected = args.source or (PILOT_ADAPTERS if args.pilot else ADAPTERS)
    for name in selected:
        _, adapter = adapters[name]
        for job in adapter(reader, config, moment.date(), report, warn):
            if job.get('company') and job.get('title') and job.get('city'):
                rows.append(payload(job, moment.isoformat(timespec='seconds')))
    baseline = []
    extracted = len(rows)
    if args.baseline:
        baseline = historical_rows(args.baseline)
        blocked = {warning['source'] for warning in warnings if warning['blocked']}
        blocked.update(source for key, (source, _) in adapters.items() if key not in selected)
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
    print(json.dumps({'extracted': extracted, 'scopes_completed': len(runs), 'warnings': len(warnings),
                      'counts': counts}, ensure_ascii=False))
    if not runs:
        raise SystemExit('No recruitment source completed successfully.')


if __name__ == '__main__':
    main()
