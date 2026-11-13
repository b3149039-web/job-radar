"""Read-only adapters for public recruitment listings."""
import html
import json
import re
import time
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

NCSS = 'https://job.ncss.cn'
UIBE = 'https://aeo.uibe.edu.cn'
JNJ = 'https://www.careers.jnj.com'
JNJ_NAME = '强生招聘官网'
ACCESS_BLOCKED = (401, 403, 429)
NCSS_NAME = '国家大学生就业服务平台'
UIBE_NAME = '对外经济贸易大学就业网'
CST = timezone(timedelta(hours=8))
ROLE = re.compile(r'法务|法律事务|法律合规|法律专员|合规|合同管理')
GOVERNMENT_ROLE = re.compile(r'政府事务|政府关系|公共事务|监管政策研究')
ENGLISH_ROLE = re.compile(r'\blegal (?:counsel|affairs|specialist|manager)|\b(?:compliance|contract) (?:counsel|manager|specialist)|\b(?:government|public) affairs', re.I)


def target_role(title, config):
    return ROLE.search(title) or (config.get('include_government_sources') and GOVERNMENT_ROLE.search(title))
CLOSED = re.compile(r'(?:该)?(?:职位|岗位)(?:已下线|已失效|已关闭|已下架|已过期|已停止招聘)|(?:招聘|报名|投递)(?:已结束|已截止)')
DEADLINE = re.compile(
    r'(?:(?:报名|投递|网申|申请)(?:时间)?\s*(?:截止(?:时间|日期)?|[：:]?\s*截至)'
    r'|截止(?:时间|日期))\s*(?:为|[：:])?\s*(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})'
)


def plain(markup):
    markup = re.sub(r'<!--.*?-->|<(script|style)\b[^>]*>.*?</\1>', '', markup, flags=re.S | re.I)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', markup)).split())


def group(pattern, value):
    match = re.search(pattern, value, re.S)
    return plain(match[1]) if match else ''


def requirement_text(markup, title):
    """Preserve clause boundaries and pair table headers with their own cells."""
    tables = []
    for table in re.findall(r'<table\b[^>]*>(.*?)</table>', markup, re.S | re.I):
        rows = [re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.S | re.I)
                for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S | re.I)]
        if rows and len(rows[0]) > 1 and any('要求' in plain(cell) or '学历' in plain(cell) for cell in rows[0]):
            selected = rows[1:] if len(rows) == 2 else [row for row in rows[1:] if title in map(plain, row)]
            for row in selected:
                if len(row) == len(rows[0]):
                    tables.extend(f'{plain(label)}：{plain(cell)}' for label, cell in zip(rows[0], row) if plain(cell))
    markup = re.sub(r'<table\b[^>]*>.*?</table>', '', markup, flags=re.S | re.I)
    markup = re.sub(r'</(?:tr|p|li|div|h[1-6])\s*>|<br\s*/?>', '\n', markup, flags=re.I)
    parts = [plain(line) for line in markup.splitlines()]
    return '\n'.join(part for part in [*tables, *parts] if part)[:18000]


def cohort(text):
    match = re.search(r'(?<!\d)(20\d{2}|\d{2})\s*届', text)
    if match:
        return int(match[1]) + (2000 if len(match[1]) == 2 else 0)
    window = re.search(r'(20\d{2})\s*年\s*(?:9|10|11|12)\s*月.{0,14}?[至到—–-]\s*(20\d{2})\s*年\s*[678]\s*月.{0,12}毕业', text)
    if window and int(window[2]) == int(window[1]) + 1:
        return int(window[2])
    return None


def status_for(text, today):
    match = DEADLINE.search(text)
    deadline = ''
    if match:
        try:
            deadline = date(*map(int, match.groups())).isoformat()
        except ValueError:
            pass
    if not deadline:
        match = re.search(r'(?:application deadline|closing date|apply by)\s*[:：]?\s*(20\d{2}-\d{2}-\d{2})\b', text, re.I)
        if match:
            try:
                deadline = date.fromisoformat(match[1]).isoformat()
            except ValueError:
                pass
    if CLOSED.search(text):
        return '已结束', deadline
    if deadline and deadline < today.isoformat():
        return '已过期', deadline
    return '在招待确认', deadline


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SourceAccessDenied(ValueError):
    pass


def access_blocked(error):
    return isinstance(error, SourceAccessDenied) or (
        isinstance(error, HTTPError) and error.code in ACCESS_BLOCKED
    )


class PublicReader:
    """Respect robots rules, pace requests and avoid redirects into login pages."""
    agent = 'JobRadar/1.0'

    def __init__(self):
        self.robots = {}
        self.last_request = {}
        self.cache = {}
        self.opener = build_opener(NoRedirect)

    def request(self, url, binary=False):
        host = urlsplit(url).netloc
        time.sleep(max(0, .45 - (time.monotonic() - self.last_request.get(host, 0))))
        self.last_request[host] = time.monotonic()
        req = Request(url, headers={'User-Agent': self.agent, 'Accept': 'application/json,text/html,*/*'})
        with self.opener.open(req, timeout=10) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('响应超出大小限制')
            return raw if binary else raw.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')

    def get(self, url, binary=False):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc not in {urlsplit(origin).netloc for origin in (NCSS, UIBE, JNJ)}:
            raise ValueError('链接不属于已接入的公开来源')
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in self.robots:
            rules = RobotFileParser(origin + '/robots.txt')
            try:
                raw = self.request(rules.url)
            except HTTPError as exc:
                if exc.code not in (404, 410):
                    raise
                raw = ''
            raw = raw.lstrip('\ufeff')
            if raw.lstrip().startswith('<'):
                if not re.search(r'<title>\s*404\b', raw, re.I):
                    raise ValueError('robots.txt 返回非规则页面')
                raw = ''
            rules.parse(raw.splitlines())
            self.robots[origin] = rules
        if not self.robots[origin].can_fetch(self.agent, url):
            raise SourceAccessDenied('robots.txt 禁止读取该路径')
        key = (url, binary)
        if key not in self.cache:
            self.cache[key] = self.request(url, binary=binary)
        return self.cache[key]


def attachment_links(markup, base):
    clean = re.sub(r'<!--.*?-->', '', markup, flags=re.S)
    links = {}
    for href, label in re.findall(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>', clean, re.S | re.I):
        url = urljoin(base, html.unescape(href))
        if urlsplit(url).scheme != 'https':
            continue
        if re.search(r'\.(?:pdf|docx?|xlsx?|zip|png|jpg)$', urlsplit(url).path, re.I) or re.search(r'附件|岗位表|职位表', plain(label)):
            links[url] = True
    return list(links)


def attachment_row(text, title):
    rows = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in re.split(r'\t|\s{2,}|[|｜]', line)]
        if len(cells) > 1 and title in cells:
            rows.append('；'.join(cells))
    return rows[0] if len(rows) == 1 else ''


def read_attachments(reader, job, links):
    unresolved = []
    evidence = []
    for url in links[:4]:
        try:
            if not urlsplit(url).path.lower().endswith('.pdf'):
                unresolved.append(url)
                continue
            from pypdf import PdfReader
            pdf = PdfReader(BytesIO(reader.get(url, binary=True)))
            if len(pdf.pages) > 20:
                unresolved.append(url)
                continue
            text = '\n'.join(page.extract_text(extraction_mode='layout') or '' for page in pdf.pages)
            row = attachment_row(text, job['title'])
            if not row:
                unresolved.append(url)
                continue
            evidence.append({'url': url, 'text': row[:2000]})
        except (OSError, ValueError, ImportError, RuntimeError, AttributeError, KeyError):
            unresolved.append(url)
        except Exception as exc:
            # Optional PDF readers may expose version-specific parsing exceptions.
            if type(exc).__module__.split('.')[0] != 'pypdf':
                raise
            unresolved.append(url)
    job['unread_attachments'] = unresolved + links[4:]
    job['attachment_evidence'] = evidence
    if evidence:
        job['requirement_text'] += '\n' + '\n'.join(entry['text'] for entry in evidence)


def parse_ncss_detail(markup, row, cities, today):
    markup = re.sub(r'<!--.*?-->', '', markup, flags=re.S)
    title = group(r'<li\b[^>]*\bid="jobName"[^>]*>(.*?)</li>', markup)
    content = plain(markup)
    if not title and not CLOSED.search(content):
        raise ValueError('详情缺少职位字段，页面可能不可访问')
    title = title or plain(row.get('jobName') or '')
    # The list often only exposes 江苏. Read the detail's workplace, never the employer address.
    locations = re.findall(r'<div\s+class="site-tag"[^>]*>(.*?)</div>', markup, re.S)
    location_text = ' '.join(map(plain, locations))
    if not location_text:
        location_text = row.get('areaCodeName') or ''
    city = '、'.join(city for city in cities if city in location_text)
    if not city:
        return None
    summary = content.partition('职位详情')[2].split('扫码快速投递简历', 1)[0].split('看了该职位', 1)[0]
    detail = markup.partition('职位详情')[2]
    detail = re.split(r'扫码快速投递简历|看了该职位', detail, maxsplit=1)[0]
    requirements = requirement_text(detail, title)
    if summary.strip().startswith('{'):
        try:
            structured, _ = json.JSONDecoder().raw_decode(summary.strip())
            if isinstance(structured, dict) and isinstance(structured.get('rzzg'), str):
                requirements = structured['rzzg']
        except ValueError:
            pass
    listing = group(r'<li>\s*——\s*</li>\s*<li[^>]*>(.*?)</li>', markup)
    year = cohort(f'{title} {listing} {summary}')
    before_detail = content.partition('职位详情')[0]
    status, deadline = status_for(f'{before_detail} {summary}', today)
    return dict(title=title, company=plain(row.get('recName') or ''), city=city,
                summary=summary.strip()[:420], cohort=year, deadline=deadline, check_status=status,
                requirement_text=requirements, campaign_title=listing,
                degree_requirement=plain(row.get('degreeName') or ''), employer_type=plain(row.get('recProperty') or ''),
                source=NCSS_NAME, publisher=plain(row.get('sourcesNameCh') or ''))


def parse_uibe_detail(markup, cities, year, today):
    title = group(r'<h[23]\b[^>]*class="content_header_title"[^>]*>(.*?)</h[23]>', markup)
    if not title:
        raise ValueError('详情缺少职位字段')
    company = group(r'<a\b[^>]*class="assist-link"[^>]*>(.*?)</a>', markup)
    location = group(r'<p>\s*<b>工作地点[：:]</b>(.*?)</p>', markup)
    city = '、'.join(city for city in cities if city in location)
    if not city:
        return None
    summary = group(r'<div\s+class="content">(.*?)</div>', markup)
    body = re.search(r'<div\s+class="content">(.*?)</div>', markup, re.S)
    degree = group(r'<p>\s*<b>学历要求[：:]</b>(.*?)</p>', markup)
    salary = group(r'<p>\s*<b>(?:月薪|每月薪水)[：:]</b>(.*?)</p>', markup)
    status, deadline = status_for(plain(markup), today)
    posted = group(r'发布于(20\d\d-\d\d-\d\d)', markup)
    return dict(title=title, company=company, city=city, summary=summary[:420], cohort=year,
                requirement_text=requirement_text(body[1], title) if body else '',
                degree_requirement=degree, salary_text='月薪 ' + salary if salary else '',
                posted_at=posted, deadline=deadline, check_status=status, source=UIBE_NAME)


def ncss_search(reader, keyword, area, page_size):
    query = urlencode(dict(jobName=keyword, areaCode=area, jobType='', offset=1, limit=page_size))
    response = json.loads(reader.get(f'{NCSS}/student/jobs/jobslist/ajax/?{query}'))
    if not isinstance(response, dict) or response.get('flag') is not True:
        raise ValueError('公开职位接口未返回成功')
    rows = (response.get('data') or {}).get('list')
    if not isinstance(rows, list):
        raise ValueError('公开职位接口缺少列表字段')
    return rows


def collect_ncss(reader, config, today, report, warn):
    cities = config['preferred_cities']
    settings = config['ncss']
    seen = set()
    for city in cities:
        area = settings['area_codes'].get(city)
        if not area:
            continue
        # Each city has its own detail budget so Shanghai cannot crowd out Suzhou.
        candidates = []
        raw_count = 0
        for keyword in settings['keywords']:
            try:
                rows = ncss_search(reader, keyword, area, settings['page_size'])
            except (OSError, ValueError) as exc:
                warn(NCSS_NAME, f'{city}/{keyword}', exc)
                if access_blocked(exc):
                    return
                continue
            raw_count += len(rows)
            for row in rows:
                job_code = row.get('jobId') or ''
                title = plain(row.get('jobName') or '')
                if re.fullmatch(r'[A-Za-z0-9_-]+', job_code) and target_role(title, config):
                    candidates.append(row)
        accepted = 0
        checked = 0
        employers = set()
        if config.get('target_cohort'):
            candidates.sort(key=lambda row: cohort(row.get('jobName') or '') != config['target_cohort'])
        for row in candidates:
            if checked >= settings['detail_limit_per_city']:
                break
            code = row['jobId']
            key = (row.get('recName'), row.get('jobName'))
            if code in seen or (config.get('target_cohort') and key[0] and key in employers):
                continue
            seen.add(code)
            checked += 1
            url = f'{NCSS}/student/jobs/{code}/detail.html'
            try:
                markup = reader.get(url)
                job = parse_ncss_detail(markup, row, cities, today)
            except (OSError, ValueError) as exc:
                warn(NCSS_NAME, url, exc)
                if access_blocked(exc):
                    return
                continue
            if not job or not target_role(job['title'], config):
                continue
            posted = row.get('publishDate')
            try:
                posted_at = datetime.fromtimestamp(posted / 1000, CST).date().isoformat() if posted else ''
            except (TypeError, OverflowError, ValueError, OSError):
                posted_at = ''
            if posted_at and (posted_at > today.isoformat() or (
                config.get('target_cohort') and int(posted_at[:4]) < config['target_cohort'] - 1
            )):
                continue
            employers.add(key)
            accepted += 1
            read_attachments(reader, job, attachment_links(markup.partition('职位详情')[2].split('看了该职位', 1)[0], url))
            yield {**job, 'url': url, 'posted_at': posted_at}
        report(NCSS_NAME, city, raw_count, accepted)


def collect_uibe(reader, config, today, report, warn):
    for url in config.get('uibe_announcements', []):
        if urlsplit(url).netloc != urlsplit(UIBE).netloc or urlsplit(url).path != '/front/zpxx.jspa':
            warn(UIBE_NAME, url, ValueError('需要公开招聘公告地址'))
            continue
        try:
            markup = reader.get(url)
            heading = group(r'<h3\b[^>]*class="content_title common-head"[^>]*>(.*?)</h3>', markup)
            if not heading:
                raise ValueError('公告缺少标题字段')
            year = cohort(heading)
            links = {}
            if heading:
                for href, label in re.findall(r'<a\s+href="(/front/zwxx\.jspa\?[^"]+)"[^>]*>(.*?)</a>', markup, re.S):
                    if target_role(plain(label), config):
                        links[urljoin(url, html.unescape(href))] = True
            accepted = 0
            for detail in links:
                detail_markup = reader.get(detail)
                job = parse_uibe_detail(detail_markup, config['preferred_cities'], year, today)
                if job:
                    accepted += 1
                    job['campaign_title'] = heading
                    links_to_check = attachment_links(detail_markup, detail) + attachment_links(markup, url)
                    read_attachments(reader, job, list(dict.fromkeys(links_to_check)))
                    yield {**job, 'url': detail}
            report(UIBE_NAME, heading, len(links), accepted)
        except (OSError, ValueError) as exc:
            warn(UIBE_NAME, url, exc)
            if access_blocked(exc):
                return


def parse_jnj_detail(markup, cities, today):
    def field(label):
        return group(r'<li[^>]*>\s*' + re.escape(label) + r'\s*<span[^>]*>(.*?)</span>', markup)

    title = field('Job title')
    code = field('Requisition number')
    if not title or not re.fullmatch(r'R-\d+', code):
        raise ValueError('缺少职位标题或岗位编号')
    locations = field('Location')
    city_names = {'上海': 'Shanghai', '苏州': 'Suzhou', '无锡': 'Wuxi', '常州': 'Changzhou'}
    city = '、'.join(city for city in cities if city in locations or re.search(
        r'\b' + city_names[city] + r'\b', locations, re.I))
    if not city or not ENGLISH_ROLE.search(title):
        return None
    # Parse only the job's qualification section, before site-wide application notices.
    details = re.search(r'<(?:strong|b)>\s*Qualifications\s*:?\s*</(?:strong|b)>.*?(<ul\b[^>]*>.*?</ul>)', markup, re.I | re.S)
    requirements = requirement_text(details[1], title) if details else ''
    degree = next((line for line in requirements.splitlines() if re.search(r'\b(?:bachelor|master|degree)\b', line, re.I)), '')
    experience = next((line for line in requirements.splitlines() if re.search(r'\b\d+\+?\s*(?:[-–]\s*\d+\s*)?years?\b', line, re.I)), '')
    posted = group(r'Date posted\s*<span[^>]*>\s*<time datetime="(\d{4}-\d{2}-\d{2})"', markup)
    if posted and date.fromisoformat(posted) > today:
        posted = ''
    year = cohort(title)
    body = plain(markup).partition('Job Applicant Consent Letter')[0]
    status, deadline = status_for(body, today)
    closed = re.search(r'this (?:job|position) (?:is no longer available|has been filled)', body, re.I)
    apply_link = re.search(r'<a\b[^>]*\bhref="https://[^"]+"[^>]*>\s*<span[^>]*>\s*Apply now\s*</span>', markup, re.I)
    return dict(title=title, company='Johnson & Johnson', city=city, source=JNJ_NAME, requisition=code,
                cohort=year, degree_requirement=degree, experience_requirement=experience,
                posted_at=posted, deadline=deadline, requirement_text=requirements,
                recruitment_type='campus' if year or re.search(r'graduate program|campus', title, re.I)
                else 'experienced' if experience else 'unknown',
                check_status='已结束' if closed else '官网显示可投递' if apply_link and status == '在招待确认' else status)


def collect_jnj(reader, config, today, report, warn):
    index = JNJ + '/en/locations/asia-pacific/china/'
    try:
        markup = reader.get(index)
        if not re.search(r'<h1\b[^>]*>\s*Careers in China', markup, re.I):
            raise ValueError('缺少中国招聘列表标题')
    except (OSError, ValueError) as exc:
        warn(JNJ_NAME, '中国招聘列表', exc)
        if access_blocked(exc):
            return
        markup = ''
    links = {}
    for href in re.findall(r'href="(/en/jobs/r-\d+/[^"]+)"', markup, re.I):
        if ENGLISH_ROLE.search(href.replace('-', ' ')):
            links[urljoin(index, html.unescape(href))] = None
    for href in config.get('jnj_details', []):
        if re.fullmatch(re.escape(JNJ) + r'/en/jobs/r-\d+/[a-z0-9-]+/?', href):
            links[href] = None
    accepted = 0
    for url in list(links)[:12]:
        try:
            job = parse_jnj_detail(reader.get(url), config['preferred_cities'], today)
            if job:
                accepted += 1
                yield {**job, 'url': url}
        except (OSError, ValueError) as exc:
            warn(JNJ_NAME, url, exc)
            if access_blocked(exc):
                break
    if markup or accepted:
        report(JNJ_NAME, '中国招聘列表与已知详情', len(links), accepted)


def recheck_public(reader, job, cities, today):
    """Recheck only a known source and recognized detail URL."""
    url = job['url']
    parsed = urlsplit(url)
    if job['source'] == NCSS_NAME and parsed.netloc == urlsplit(NCSS).netloc and re.fullmatch(
            r'/student/jobs/[A-Za-z0-9_-]+/detail\.html', parsed.path):
        markup = reader.get(url)
        result = parse_ncss_detail(markup, {'jobName': job['title'], 'areaCodeName': job['city']}, cities, today)
    elif job['source'] == UIBE_NAME and parsed.netloc == urlsplit(UIBE).netloc and parsed.path == '/front/zwxx.jspa':
        markup = reader.get(url)
        result = parse_uibe_detail(markup, cities, job['cohort'], today)
    elif job['source'] == JNJ_NAME and parsed.netloc == urlsplit(JNJ).netloc and re.fullmatch(
            r'/en/jobs/r-\d+/[^/]+/?', parsed.path):
        result = parse_jnj_detail(reader.get(JNJ + parsed.path.rstrip('/') + '/'), cities, today)
    else:
        return None
    return result


def recheck_history(reader, jobs, collected, failed_urls, today, warn, limit):
    """Recheck a bounded set of known detail URLs that fell off the current lists."""
    current = {job['url'] for job in collected}
    candidates = sorted(jobs, key=lambda job: job.get('checked_at', ''))
    checked = 0
    stopped = set()
    for job in candidates:
        url = job['url']
        if url in current:
            continue
        if job['deadline'] and job['deadline'] < today.isoformat():
            if job['check_status'] != '已结束':
                job['check_status'] = '已过期'
            continue
        if url in failed_urls:
            job.update(check_status='无法访问', checked_at=datetime.now(CST).strftime('%Y-%m-%d %H:%M'))
            continue
        source = job['source']
        path = urlsplit(url).path
        if checked >= limit or source in stopped:
            continue
        ncss = source == NCSS_NAME and re.fullmatch(r'/student/jobs/[A-Za-z0-9_-]+/detail\.html', path)
        uibe = source == UIBE_NAME and path == '/front/zwxx.jspa'
        if not (ncss or uibe):
            continue
        checked += 1
        try:
            markup = reader.get(url)
            if ncss:
                result = parse_ncss_detail(markup, {'jobName': job['title']}, job['city'].split('、'), today)
            else:
                result = parse_uibe_detail(markup, job['city'].split('、'), job['cohort'], today)
            if not result:
                status, deadline = status_for(plain(markup), today)
            else:
                status, deadline = result['check_status'], result['deadline']
                if result['requirement_text']:
                    read_attachments(reader, result, attachment_links(markup.partition('职位详情')[2] if ncss else markup, url))
                    for field in ('requirement_text', 'unread_attachments', 'attachment_evidence'):
                        job[field] = result[field]
            job['check_status'] = status
            if deadline:
                job['deadline'] = deadline
        except (OSError, ValueError) as exc:
            job['check_status'] = '无法访问'
            warn(source, url, exc)
            if isinstance(exc, HTTPError) and exc.code in (401, 403, 429):
                stopped.add(source)
        job['checked_at'] = datetime.now(CST).strftime('%Y-%m-%d %H:%M')
