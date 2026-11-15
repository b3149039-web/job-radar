"""Build and validate the static daily jobs feed using public collection reports only."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from production_sync import SOURCE_ORIGINS, SCHEDULED_SOURCES, SafetyStop, check_scheduled_collection, report_rows
from collect_jobs import plan, sources, stable_job_id
from supabase.generate_seed import SKILLS, redact_contacts

FEED_NAME = 'daily-jobs.json'
MAX_BYTES = 2_000_000
MAX_JOBS = 80
MAX_HISTORY = 20_000
ID = re.compile(r'[a-f0-9]{16}')
PUBLIC_STATUS = {'企业官网岗位页显示投递入口', '招聘官网公开职位数据含该岗位的申请入口'}
PATHS = {sources.NCSS_NAME: r'/student/jobs/[A-Za-z0-9_-]+/detail\.html', sources.UIBE_NAME: r'/front/zwxx\.jspa',
         sources.JNJ_NAME: r'/en/jobs/r-\d+/[a-z0-9-]+/?', sources.LILLY_NAME: sources.LILLY_PATH,
         sources.ASML_NAME: sources.ASML_PATH, sources.HEBUT_NAME: sources.HEBUT_PATH}
REASONS = set('学历条件表述冲突|仅招博士或同等层级|仅招硕士及以上|学历接受本科|学历要求未确认|政治面貌不限|'
              '党员为优先项或接受团员|要求党员或预备党员|符合政治面貌要求|党员条件需核验|政治面貌未说明|届别不符|'
              '需确认毕业月份是否在要求范围内|要求中国法律执业资格|持证为优先项或无需持证|满足法考客观题要求|'
              '要求已通过法考客观题|需核验客观题要求|符合持证要求|毕业或入职前取证，待确认|暂不符合，要求投递时持证或通过法考|'
              '要求通过完整法考或司法考试|需确认法考或取证要求及时间|法考或证书要求未说明'.split('|'))


def iso_day(value):
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value).isoformat()
    return ''


def number(value, low, high):
    return value if type(value) is int and low <= value <= high else None


def flag(value):
    return value if type(value) is bool else None


def display(value, limit=160):
    if not isinstance(value, str) or re.search(r'(?i)sb_(?:secret|publishable)_|eyJ[A-Za-z0-9_-]+\.|https?://|postgres(?:ql)?://|\b(?:token|password|apikey|authorization)\s*[:=]', value):
        return ''
    return ' '.join(redact_contacts(value).split())[:limit]


def allow(value, choices, unknown=''):
    return value if isinstance(value, str) and value in choices else unknown


def amount(value):
    if isinstance(value, list) and len(value) == 2 and all(number(n, 1, 100_000_000) for n in value) and value[0] <= value[1]:
        return value
    return None


def salary_label(value):
    text = display(value, 100)
    return text if re.fullmatch(r'[\d\s.,+¥￥$kK～~–—/%（）()：:月年薪万千元面议税前范围计薪周期口径待核实CNY人民币至-]+', text) else ''


def qualification(item):
    reason = item.get('reason')
    if not isinstance(reason, str) or (reason not in REASONS and not re.fullmatch(r'(?:接受20\d\d届|20\d\d届资格未明确)', reason)):
        return None
    state = allow(item.get('state'), {'符合', '不符合', '待核验'})
    return {'state': state, 'reason': reason, 'evidence': '来源详情' if item.get('evidence') else ''} if state else None


def checked_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+08:00', value):
        raise ValueError('Daily feed requires a Beijing timestamp.')
    return datetime.fromisoformat(value)


def public_url(row):
    source = row['source']
    parts = urlsplit(row['url'])
    if parts.scheme != 'https' or parts.username or parts.password or parts.port or not re.fullmatch(r'/[A-Za-z0-9/_.-]+', parts.path):
        raise ValueError('Unexpected public job URL.')
    if source in PATHS:
        if f'{parts.scheme}://{parts.netloc}' != SOURCE_ORIGINS[source] or not re.fullmatch(PATHS[source], parts.path):
            raise ValueError('Unexpected source job path.')
    elif not sources.pilot_posting_url(row):
        raise ValueError('Unexpected pilot job path.')
    names = {'xqzwId', 'zpxxId', 'tid'} if source == sources.UIBE_NAME else {'gh_jid'} if source == sources.PILOT_NAMES[1] else set()
    pairs = [(key, value) for key, value in parse_qsl(parts.query) if key in names and re.fullmatch(r'[0-9]{1,24}', value)]
    if len({key for key, value in pairs}) != len(pairs) or (source == sources.UIBE_NAME and not pairs):
        raise ValueError('Unexpected posting identifier.')
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), urlencode(sorted(pairs)), ''))


def project(row, day):
    facts = row['qualifications']
    details, social = facts.get('source_details', {}), facts.get('social', {})
    education, experience, salary = facts.get('education', {}), facts.get('experience', {}), social.get('salary', {})
    cohort = number(row.get('cohort'), 2000, 2100)
    url = public_url(row)
    company, title = display(row['company']), display(row['title'])
    city = allow(row['city'], sources.CITY_EN)
    if not company or not title or not city or row['source'] not in SCHEDULED_SOURCES:
        raise ValueError('Daily job lacks a valid identity.')
    proof = allow(details.get('status_evidence'), PUBLIC_STATUS, '在招状态待核验')
    evidence_time = iso_day(str(details.get('status_checked_at', ''))[:10])
    posted, updated = (iso_day(details.get(field)) for field in ('source_posted_at', 'source_updated_at'))
    if posted > day or updated > day or evidence_time > day:
        raise ValueError('Future job evidence date.')
    q = {
        'education': {'minimum': number(education.get('minimum'), 0, 4), 'conflict': flag(education.get('conflict'))},
        'major': {'kind': allow(facts.get('major', {}).get('kind'), {'law', 'any', 'other'}, 'unknown')},
        'experience': {'minimum': number(experience.get('minimum'), 0, 80), 'maximum': number(experience.get('maximum'), 0, 80),
                       'kind': allow(experience.get('kind'), {'law_firm', 'in_house', 'both', 'any', 'unspecified'}, 'unknown')},
        'social': {'role_kind': allow(social.get('role_kind'), {'legal', 'government', 'collections', 'sales', 'administrative'}, 'unknown'),
                   'solo_legal': flag(social.get('solo_legal')), 'solo_evidence': '来源详情' if social.get('solo_evidence') else '',
                   'skills': sorted({key for key in social.get('skills', []) if isinstance(key, str) and key in SKILLS}),
                   'employer_tags': sorted({key for key in social.get('employer_tags', []) if isinstance(key, str) and key in
                                            {'外资', '上市公司', '集团', '中大型企业', '成熟法务团队', '央国企'}}),
                   'salary': {'currency': allow(salary.get('currency'), {'CNY'}), 'annual': amount(salary.get('annual')),
                              'monthly': amount(salary.get('monthly')), 'months': number(salary.get('months'), 12, 24),
                              'guaranteed_bonus': amount(salary.get('guaranteed_bonus')),
                              'annual_complete': flag(salary.get('annual_complete')),
                              'variable_performance': flag(salary.get('variable_performance'))}},
        'attachment_unverified': flag(facts.get('attachment_unverified')),
        'source_details': {'source_posted_at': posted, 'source_updated_at': updated, 'deadline': iso_day(details.get('deadline')),
                           'status_checked_at': evidence_time, 'status_evidence': proof,
                           'engagement': allow(details.get('engagement'), {'internship'}),
                           'salary_display': salary_label(details.get('salary_display')),
                           'workplace_evidence': allow(details.get('workplace_evidence'),
                                {'招聘详情工作地点', '来源列表，地点未在详情核实'}),
                           'platform_posting_id': allow_posting_id(details.get('platform_posting_id'))},
    }
    cohorts = []
    for entry in facts.get('cohort_evidence', []):
        year, item = number(entry.get('year'), 2000, 2100), qualification(entry)
        if year and item:
            cohorts.append({**item, 'year': year})
    q['cohort_evidence'] = sorted(cohorts, key=lambda item: item['year'])[:12]
    for field in ('bachelor', 'cohort_2027', 'non_party_member', 'with_certificate', 'without_certificate',
                  'objective_only', 'neither_exam_nor_certificate'):
        if item := qualification(facts.get(field, {})):
            q[field] = item
    if row.get('recruitment_type') == 'unknown' and re.search(
            r'^(?:任职要求|应聘条件)[:：].*(?:本科|学士|硕士|研究生|博士|专科)\s*应届毕业生', education.get('evidence') or '') and not re.search(
            r'欢迎|亦可|也可|均可|不限|优先应届|非应届', education.get('evidence') or ''):
        recruitment = 'campus'
    else:
        recruitment = allow(row.get('recruitment_type'), {'campus', 'experienced'}, 'unknown')
    clean = {'company': company, 'title': title, 'city': city, 'source': row['source'], 'url': url,
             'cohort': cohort, 'qualifications': q, 'recruitment_type': recruitment,
             'employer_type': allow(row.get('employer_type'), {'国有企业', '国企', '央企', '上市公司', '外资企业', '大型企业', '集团'}),
             'check_status': '官网显示可投递' if row.get('check_status') == '官网显示可投递' and proof in PUBLIC_STATUS and evidence_time else '在招待确认'}
    clean['id'] = stable_job_id(clean)
    return clean


def allow_posting_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else ''


def validate_feed(feed):
    if (not isinstance(feed, dict) or set(feed) != {'version', 'last_success_at', 'first_seen', 'jobs'} or
            type(feed['version']) is not int or feed['version'] != 1):
        raise ValueError('Unexpected daily feed schema.')
    day = checked_time(feed['last_success_at']).date().isoformat()
    history, jobs = feed['first_seen'], feed['jobs']
    if (not isinstance(history, dict) or len(history) > MAX_HISTORY or not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS or
            any(not ID.fullmatch(key) or not iso_day(value) or value > day for key, value in history.items())):
        raise ValueError('Unexpected daily feed counts or discovery dates.')
    keys = set()
    for row in jobs:
        clean = project(row, day)
        key = clean['id']
        if key in keys or key not in history or row != {**clean, 'first_seen': history[key]}:
            raise ValueError('Daily feed contains an unexpected or duplicated job field.')
        keys.add(key)
    return feed


def generate(report, previous, now):
    rows, counts, warnings, unavailable = report_rows(report, now, scheduled=True)
    check_scheduled_collection(rows, counts, warnings, unavailable, now.date())
    moment = checked_time(json.loads(report.read_bytes())['collected_at'])
    if previous and moment <= checked_time(previous['last_success_at']):
        raise ValueError('Refusing to replace the feed with an older or repeated collection.')
    day = moment.date().isoformat()
    _, changes = plan(rows, [], moment.date(), count_similar=True)
    history = dict(previous['first_seen']) if previous else {}
    public = {}
    for _, row in changes:
        job = project(row, day)
        key = job['id']
        job['first_seen'] = history.setdefault(key, day)
        public.setdefault(key, job)
    return validate_feed({'version': 1, 'last_success_at': moment.isoformat(), 'first_seen': history, 'jobs': list(public.values())})


def read_feed(data):
    if len(data) > MAX_BYTES:
        raise ValueError('Daily feed size exceeded.')
    return validate_feed(json.loads(data))


def published_feed(url):
    site = urlsplit(url)
    if (site.scheme != 'https' or not site.hostname or site.username or site.password or site.port or site.query or
            site.fragment or site.path.rsplit('/', 1)[-1] != FEED_NAME):
        raise ValueError('Unexpected previous feed URL.')
    try:
        request = Request(url, headers={'Cache-Control': 'no-cache'})
        with build_opener(sources.NoRedirect).open(request, timeout=15) as response:
            return read_feed(response.read(MAX_BYTES + 1))
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--previous', type=Path)
    group.add_argument('--previous-url')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        previous = (read_feed(args.previous.read_bytes()) if args.previous else
                    published_feed(args.previous_url) if args.previous_url else None)
        feed = generate(args.report, previous, datetime.now(sources.CST)) if args.report else previous
        if feed is not None:
            payload = (json.dumps(feed, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
            read_feed(payload)
            temporary = args.output.with_suffix('.tmp')
            temporary.write_bytes(payload)
            temporary.replace(args.output)
            print(f'Validated {len(feed["jobs"])} public candidates as of {feed["last_success_at"]}.')
    except (OSError, TypeError, KeyError, ValueError, AttributeError, SafetyStop) as error:
        parser.error(f'Daily feed unavailable or rejected ({type(error).__name__}). Existing output is preserved.')


if __name__ == '__main__':
    main()
