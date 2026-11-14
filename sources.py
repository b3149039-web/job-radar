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
LILLY = 'https://careers.lilly.com'
LILLY_NAME = '礼来招聘官网'
ASML = 'https://www.asml.com'
ASML_NAME = 'ASML招聘官网'
HEBUT = 'https://career.hebut.edu.cn'
HEBUT_NAME = '河北工业大学就业网'
ACCESS_BLOCKED = (401, 403, 429)
NCSS_NAME = '国家大学生就业服务平台'
UIBE_NAME = '对外经济贸易大学就业网'
CST = timezone(timedelta(hours=8))
ROLE = re.compile(r'法务|法律事务|法律合规|法律专员|合规|合同管理')
GOVERNMENT_ROLE = re.compile(r'政府事务|政府关系|公共事务|监管政策研究')
ENGLISH_ROLE = re.compile(r'\blegal (?:counsel|affairs|specialist|manager|analyst)|\b(?:compliance|contracts?) (?:officer|counsel|manager|specialist|analyst)|\b(?:government|public) affairs', re.I)
CITY_EN = {'上海': 'Shanghai', '苏州': 'Suzhou', '无锡': 'Wuxi', '常州': 'Changzhou'}


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


def wildcard_disallowed(raw, url):
    """Python 3.12's RobotFileParser treats wildcard paths as literals."""
    path = urlsplit(url)
    target = path.path + ('?' + path.query if path.query else '')
    for rule in re.findall(r'^\s*Disallow\s*:\s*([^#\r\n]+)', raw, re.M | re.I):
        rule = rule.strip()
        if '*' in rule or rule.endswith('$'):
            anchored = rule.endswith('$')
            expression = re.escape(rule.removesuffix('$')).replace(r'\*', '.*')
            if re.match(expression + ('$' if anchored else ''), target):
                return True
    return False


class PublicReader:
    """Respect robots rules, pace requests and avoid redirects into login pages."""
    agent = 'JobRadar/1.0'

    def __init__(self):
        self.robots = {}
        self.robot_text = {}
        self.last_request = {}
        self.cache = {}
        self.opener = build_opener(NoRedirect)

    def request(self, url, binary=False):
        host = urlsplit(url).netloc
        rules = self.robots.get(f'https://{host}')
        delay = max(.75, rules.crawl_delay(self.agent) or 0) if rules else .75
        time.sleep(max(0, delay - (time.monotonic() - self.last_request.get(host, 0))))
        self.last_request[host] = time.monotonic()
        req = Request(url, headers={'User-Agent': self.agent, 'Accept': 'application/json,text/html,*/*'})
        with self.opener.open(req, timeout=10) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('响应超出大小限制')
            return raw if binary else raw.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')

    def get(self, url, binary=False):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc not in {
                urlsplit(origin).netloc for origin in (NCSS, UIBE, JNJ, LILLY, ASML, HEBUT)}:
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
            self.robot_text[origin] = raw
        if not self.robots[origin].can_fetch(self.agent, url) or wildcard_disallowed(self.robot_text[origin], url):
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


def ncss_date(value, today):
    if not isinstance(value, (int, float)) or value <= 0:
        return ''
    try:
        result = datetime.fromtimestamp(value / 1000, CST).date()
        return result.isoformat() if result <= today else ''
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


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
    body = requirements
    if summary.strip().startswith('{'):
        try:
            structured, _ = json.JSONDecoder().raw_decode(summary.strip())
            if isinstance(structured, dict) and isinstance(structured.get('rzzg'), str):
                requirements = structured['rzzg']
                body = '\n'.join(structured.get(key, '') for key in ('gwzz', 'rzzg', 'xzfl')
                                 if isinstance(structured.get(key), str))
        except ValueError:
            pass
    listing = group(r'<li>\s*——\s*</li>\s*<li[^>]*>(.*?)</li>', markup)
    year = cohort(f'{title} {listing} {summary}')
    before_detail = plain(markup.split('id="jobName"', 1)[-1].partition('职位详情')[0])
    status, deadline = status_for(f'{before_detail} {summary}', today)
    heading = re.search(r'\|\s*([^|]{1,25})\|\s*招聘\s*(\d+)\s*人', before_detail)
    # Read the visible metadata line without accidentally including toolbar text.
    header_markup = markup.split('id="jobName"', 1)[-1].partition('职位详情')[0]
    pay = re.search(r'(?<![\w.])((?:\d+(?:\.\d+)?[kK])(?:\s*[-–]\s*\d+(?:\.\d+)?[kK])?|面议)\s*\|', before_detail)
    update_label = group(r'<span[^>]*>\s*(\d{2}-\d{2}\s+\d{2}:\d{2})\s*</span>\s*更新', header_markup)
    source_update = ncss_date(row.get('updateDate'), today)
    if not source_update and update_label:
        # A matching prior dated source observation can disambiguate the year on recheck.
        prior = row.get('known_update_date', '')
        if prior[5:] == update_label[:5]:
            source_update = prior
    return dict(title=title, company=plain(row.get('recName') or ''), city=city,
                summary=summary.strip()[:420], cohort=year, deadline=deadline, check_status=status,
                requirement_text=requirements, campaign_title=listing,
                degree_requirement=plain(heading[1]) if heading else plain(row.get('degreeName') or ''),
                employer_type=plain(row.get('recProperty') or ''),
                source=NCSS_NAME, publisher=group(r'<span[^>]*class="source-sp"[^>]*>(.*?)</span>', header_markup)
                or plain(row.get('sourcesNameCh') or ''),
                source_updated_at=source_update, source_update_label=update_label,
                platform_listed_at=ncss_date(row.get('publishDate'), today),
                platform_posting_id=row.get('jobId') or group(r'<input[^>]*name="jobId"[^>]*value="([^"]+)"', markup),
                headcount=int(heading[2]) if heading else None, salary_display=pay[1] if pay else '',
                department=group(r'(?:招聘部门|所属部门)\s*[：:]\s*([^<]+)', detail),
                batch=group(r'(?:招聘批次|校招批次)\s*[：:]\s*([^<]+)', detail),
                requisition=group(r'(?:企业岗位编号|招聘需求编号)\s*[：:]\s*([A-Za-z0-9_-]+)', detail),
                grouping_body=body, status_evidence='明确截止或结束' if status != '在招待确认' else '公开详情可读取，投递状态未确认')


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
    headcount = group(r'<p>\s*<b>(?:招聘人数|需求人数)[：:]</b>\s*(\d+)\s*(?:人)?\s*</p>', markup)
    status, deadline = status_for(plain(markup), today)
    posted = group(r'发布于(20\d\d-\d\d-\d\d)', markup)
    return dict(title=title, company=company, city=city, summary=summary[:420], cohort=year,
                requirement_text=requirement_text(body[1], title) if body else '',
                degree_requirement=degree, salary_text='月薪 ' + salary if salary else '',
                grouping_body=summary, headcount=int(headcount) if headcount else None, salary_display=salary,
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
            yield {**job, 'url': url}
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
    city = '、'.join(city for city in cities if city in locations or re.search(
        r'\b' + CITY_EN[city] + r'\b', locations, re.I))
    if not city or not ENGLISH_ROLE.search(title):
        return None
    # Parse only the job's qualification section, before site-wide application notices.
    details = re.search(
        r'<(?:strong|b)>\s*Qualifications\s*:?\s*</(?:strong|b)>(.*?)'
        r'(?:Similar jobs|Core Competencies|Required Skills|Job Applicant Consent Letter)', markup, re.I | re.S)
    if not details:
        details = re.search(r'<(?:strong|b)>\s*Qualifications\s*:?\s*</(?:strong|b)>.*?(<ul\b[^>]*>.*?</ul>)', markup, re.I | re.S)
    requirements = requirement_text(details[1], title) if details else ''
    degree = next((line for line in requirements.splitlines() if re.search(r'\b(?:bachelor|master|degree)\b', line, re.I)), '')
    experience = next((line for line in requirements.splitlines()
                       if re.search(r'\b\d+\+?\s*years?\b', line, re.I)), '')
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
                status_evidence='企业官网岗位页显示投递入口' if apply_link and not closed and status == '在招待确认'
                else '企业官网明确截止或结束' if closed or status != '在招待确认' else '官网投递状态待核验',
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


def embedded_object(markup, marker):
    match = re.search(marker, markup, re.S)
    if not match:
        raise ValueError('未找到公开岗位结构化数据')
    data, _ = json.JSONDecoder().raw_decode(markup[match.end():].lstrip())
    if not isinstance(data, dict):
        raise ValueError('公开岗位数据格式变化')
    return data


def source_date(value):
    try:
        return date.fromisoformat((value or '')[:10]).isoformat()
    except (TypeError, ValueError):
        return ''


def official_job(title, company, source, locations, cities, requisition, description, posted, deadline, today,
                 *, published_entry=False, closed=False, department='', degree=''):
    city = '、'.join(city for city in cities if city in locations or re.search(
        r'\b' + CITY_EN[city] + r'\b', locations, re.I))
    if not city or not (ROLE.search(title) or GOVERNMENT_ROLE.search(title) or ENGLISH_ROLE.search(title)):
        return None
    requirements = requirement_text(description, title)
    if not requirements:
        raise ValueError('缺少职位任职正文')
    year = cohort(title + '\n' + requirements)
    campus = year or re.search(r'校园招聘|应届毕业生|graduate program|campus recruitment', title + '\n' + requirements, re.I)
    experienced = re.search(r'\d+\s*\+?\s*(?:年.{0,25}(?:经验|经历)|years?\b.{0,35}\bexperience)', requirements, re.I)
    status, textual_deadline = status_for(requirements, today)
    deadline = min(filter(None, (source_date(deadline), textual_deadline)), default='')
    if closed or status == '已结束':
        status, evidence = '已结束', '招聘官网标明关闭或停止招聘'
    elif deadline and deadline < today.isoformat():
        status, evidence = '已过期', '超过招聘官网标注的申请截止日期'
    elif published_entry:
        status, evidence = '官网显示可投递', '招聘官网公开职位数据含该岗位的申请入口'
    else:
        status, evidence = '在招待确认', '官网详情可读取，申请状态待核验'
    posted = source_date(posted)
    return dict(title=title, company=company, source=source, city=city, requisition=requisition,
                source_posted_at=posted if posted <= today.isoformat() else '', deadline=deadline,
                check_status=status, status_evidence=evidence, department=department,
                recruitment_type='campus' if campus else 'experienced' if experienced else 'unknown',
                cohort=year, degree_requirement=degree, requirement_text=requirements)


def parse_lilly_detail(markup, cities, today, expected):
    posting = None
    for raw in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', markup, re.S | re.I):
        try:
            document = json.loads(raw)
        except ValueError:
            continue
        if isinstance(document, dict) and document.get('@type') == 'JobPosting':
            posting = document
            break
    if not posting and re.search(r'(?:job|position) (?:is no longer available|has been filled)', plain(markup), re.I):
        return {'check_status': '已结束'}
    if not posting:
        raise ValueError('职位详情缺少 JobPosting 数据')
    job = embedded_object(markup, r'phApp\.ddo\s*=\s*').get('jobDetail') or {}
    data = (job.get('data') or {}).get('job') or {}
    identifier = (posting.get('identifier') or {}).get('value', '')
    if not re.fullmatch(r'R-\d+', identifier) or data.get('jobId') != identifier or data.get('jobSeqNo') != expected:
        raise ValueError('职位标识与详情不一致')
    locations = posting.get('jobLocation') or []
    locations = locations if isinstance(locations, list) else [locations]
    location_text = ' '.join((location.get('address') or {}).get('addressLocality', '') for location in locations)
    apply = urlsplit(data.get('applyUrl') or '')
    return official_job(posting.get('title', ''), 'Eli Lilly and Company', LILLY_NAME, location_text,
                        cities, identifier, html.unescape(posting.get('description') or ''), posting.get('datePosted'),
                        posting.get('validThrough'), today, department=data.get('category') or '',
                        published_entry=job.get('status') == 200 and apply.scheme == 'https' and
                        bool(re.fullmatch(r'lilly\.wd\d+\.myworkdayjobs\.com', apply.netloc)) and
                        identifier in apply.path and apply.path.endswith('/apply'))


def collect_lilly(reader, config, today, report, warn):
    if not config.get('lilly_china'):
        return
    index = LILLY + '/us/en/china/'
    seen = set()
    returned = accepted = 0
    listed = False
    for offset in range(0, 50, 10):
        scope = index if not offset else index + '?' + urlencode({'from': offset, 's': 1})
        try:
            listing = embedded_object(reader.get(scope), r'phApp\.ddo\s*=\s*')
            packet = next((value for value in listing.values() if isinstance(value, dict) and
                           isinstance((value.get('data') or {}).get('jobs'), list)), None)
            if packet is None or not isinstance(packet.get('totalHits'), int):
                raise ValueError('中国列表未返回完整的分页字段')
        except (OSError, ValueError) as exc:
            warn(LILLY_NAME, scope, exc)
            break
        listed = True
        rows = packet['data']['jobs']
        returned += len(rows)
        for item in rows:
            title, seq = item.get('title', ''), item.get('jobSeqNo', '')
            if not re.fullmatch(r'[A-Z0-9]+', seq) or seq in seen or not (
                    target_role(title, config) or ENGLISH_ROLE.search(title)):
                continue
            seen.add(seq)
            slug = re.sub(r'[^a-zA-Z0-9-]+', '-', title).strip('-')
            if not slug:
                continue
            url = f'{LILLY}/us/en/job/{seq}/{slug}'
            try:
                job = parse_lilly_detail(reader.get(url), config['preferred_cities'], today, seq)
                if job and job.get('title'):
                    accepted += 1
                    yield {**job, 'url': url}
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                warn(LILLY_NAME, url, exc)
                if access_blocked(exc):
                    report(LILLY_NAME, '中国职位列表', returned, accepted)
                    return
        if offset + len(rows) >= packet['totalHits'] or not rows:
            break
    if listed:
        report(LILLY_NAME, '中国职位列表', returned, accepted)


def parse_asml_detail(markup, cities, today, url):
    page = embedded_object(markup, r'<script\b[^>]*id="__NEXT_DATA__"[^>]*>')['props']['pageProps']
    job = page.get('jobData')
    if not job or job.get('detailPageUrl') != url or not re.fullmatch(r'J-\d+', job.get('id', '')):
        raise ValueError('ASML 详情缺少可核对的职位编号')
    apply = urlsplit(job.get('applyUrl') or '')
    return official_job(job.get('displayJobTitle', ''), 'ASML', ASML_NAME, job.get('location') or '', cities,
                        job['id'], job.get('descriptionExternal') or '', job.get('datePosted'),
                        job.get('postingExpirationDate'), today, department='、'.join(job.get('team') or []),
                        degree='、'.join(job.get('degree') or []),
                        published_entry=job.get('status') == 'Open' and apply.scheme == 'https' and
                        bool(re.fullmatch(r'asml\.wd\d+\.myworkdayjobs\.com', apply.netloc)) and
                        job['id'] in apply.path and apply.path.endswith('/apply'),
                        closed=job.get('status') in ('Closed', 'Filled', 'Cancelled'))


ASML_PATH = r'/en/careers/find-your-job/[a-z0-9-]+-j\d+'
LILLY_PATH = r'/us/en/job/([A-Z0-9]+)/[a-zA-Z0-9-]+'
HEBUT_PATH = r'/correcruit/content/id/(\d+)\.html'


def collect_asml(reader, config, today, report, warn):
    for url in dict.fromkeys(config.get('asml_details', [])):
        if not re.fullmatch(re.escape(ASML) + ASML_PATH, url):
            warn(ASML_NAME, ASML, ValueError('需要 ASML 公开职位官网链接'))
            continue
        try:
            job = parse_asml_detail(reader.get(url), config['preferred_cities'], today, url)
            report(ASML_NAME, '已知公开职位详情', 1, int(job is not None))
            if job:
                yield {**job, 'url': url}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            warn(ASML_NAME, url, exc)
            if access_blocked(exc):
                return


def parse_hebut_detail(markup, cities, today):
    company = group(r'<a\b[^>]*class="p2"[^>]*href="/company/index/id/\d+\.html"[^>]*>(.*?)</a>', markup).strip('【】 ')
    suffix = '-' + company
    heading = group(r'<title[^>]*>(.*?)</title>', markup)
    if not company or not heading.endswith(suffix):
        raise ValueError('高校职位缺少独立岗位或企业字段')
    title = heading[:-len(suffix)]
    locations = group(r'工作地域[：:]([^<]+?)职位类别[：:]', markup)
    degree = group(r'学历要求[：:]\s*([^<]+?)招聘人数[：:]', markup)
    posted = group(r'发布时间[：:]\s*(\d{4}-\d{2}-\d{2})', markup)
    major = group(r'专业要求[：:]</div>\s*<div class="content">(.*?)</div>', markup)
    section = re.search(r'职位描述[：:]</div>\s*<div class="content">(.*?)</div>', markup, re.S)
    if not section:
        raise ValueError('高校职位缺少正文')
    job = official_job(title, company, HEBUT_NAME, locations, cities, '',
                       f'<p>专业要求：{major}</p>{section[1]}' if major else section[1], posted, '', today, degree=degree)
    if job:
        job['status_evidence'] = '高校发布的具体岗位；当前是否仍可投递待核验' if job['check_status'] == '在招待确认' else job['status_evidence']
        job['platform_posting_id'] = group(r'Toudi\((\d+)\)', markup)
        job['salary_display'] = group(r'转正后工资待遇[：:]\s*([^<]+)', markup)
        headcount = group(r'招聘人数[：:]\s*(\d+)\s*人', markup)
        job['headcount'] = int(headcount) if headcount else None
    return job


def collect_hebut(reader, config, today, report, warn):
    for url in dict.fromkeys(config.get('hebut_details', [])):
        if not re.fullmatch(re.escape(HEBUT) + HEBUT_PATH, url):
            warn(HEBUT_NAME, HEBUT, ValueError('需要高校就业网的独立岗位链接'))
            continue
        try:
            job = parse_hebut_detail(reader.get(url), config['preferred_cities'], today)
            report(HEBUT_NAME, '已知公开校招岗位详情', 1, int(job is not None))
            if job:
                yield {**job, 'url': url}
        except (OSError, ValueError) as exc:
            warn(HEBUT_NAME, url, exc)
            if access_blocked(exc):
                return


def recheck_public(reader, job, cities, today):
    """Recheck only a known source and recognized detail URL."""
    url = job['url']
    parsed = urlsplit(url)
    if job['source'] == NCSS_NAME and parsed.netloc == urlsplit(NCSS).netloc and re.fullmatch(
            r'/student/jobs/[A-Za-z0-9_-]+/detail\.html', parsed.path):
        markup = reader.get(url)
        prior = job.get('qualifications', {}).get('source_details', {})
        result = parse_ncss_detail(markup, {'jobName': job['title'], 'areaCodeName': job['city'],
            'jobId': parsed.path.split('/')[3],
            'known_update_date': prior.get('source_updated_at') or prior.get('platform_listed_at')
            or (prior.get('posted_at') or '' if 'source_posted_at' not in prior else '')}, cities, today)
    elif job['source'] == UIBE_NAME and parsed.netloc == urlsplit(UIBE).netloc and parsed.path == '/front/zwxx.jspa':
        markup = reader.get(url)
        result = parse_uibe_detail(markup, cities, job['cohort'], today)
    elif job['source'] == JNJ_NAME and parsed.netloc == urlsplit(JNJ).netloc and re.fullmatch(
            r'/en/jobs/r-\d+/[^/]+/?', parsed.path):
        result = parse_jnj_detail(reader.get(JNJ + parsed.path.rstrip('/') + '/'), cities, today)
    elif job['source'] == LILLY_NAME and re.fullmatch(re.escape(LILLY) + LILLY_PATH, url):
        result = parse_lilly_detail(reader.get(url), cities, today, re.fullmatch(LILLY_PATH, parsed.path)[1])
    elif job['source'] == ASML_NAME and re.fullmatch(re.escape(ASML) + ASML_PATH, url):
        result = parse_asml_detail(reader.get(url), cities, today, url)
    elif job['source'] == HEBUT_NAME and re.fullmatch(re.escape(HEBUT) + HEBUT_PATH, url):
        result = parse_hebut_detail(reader.get(url), cities, today)
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
