"""Conservative public application URLs and read-only, bounded endpoint checks."""
from datetime import date, timedelta
from http.client import HTTPSConnection, HTTPException
from ipaddress import ip_address
import re
import socket
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

WORKDAY = re.compile(r'(?:asml|lilly|jnj)\.wd\d{1,2}\.myworkdayjobs\.com')
GREENHOUSE = {'boards.greenhouse.io', 'job-boards.greenhouse.io'}
REASONS = {
    'source_form': '官方职位数据确认该岗位有申请表',
    'public_form': '公开入口显示可使用的职位申请表',
    'dead': '实际投递入口返回 HTTP 404 或 410',
    'closed': '招聘方在投递入口明确说明已结束',
    'limited': '公开访问受限、跳转或需要 JavaScript，无法确认',
    'redirect': '入口返回跳转，未跟随，无法确认投递',
    'network': '网络或响应超时，无法确认',
    'unconfirmed': '入口可访问，尚无可提交申请的证据',
}
OPEN_PROOFS = {'source_form', 'public_form'}


def safe_apply_url(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 2048 or re.search(r'\s|\\|%', value):
        return ''
    try:
        parts = urlsplit(value)
        host = parts.netloc
        if parts.scheme != 'https' or parts.query or not host or any(x in ('.', '..') for x in parts.path.split('/')):
            return ''
        if WORKDAY.fullmatch(host) and not parts.fragment and re.fullmatch(r'/(?:[A-Za-z0-9_-]+/){1,4}job/(?:[A-Za-z0-9_.-]+/)+apply/?', parts.path):
            return urlunsplit(parts._replace(path=parts.path.rstrip('/')))
        if host in GREENHOUSE and not parts.fragment and re.fullmatch(r'/(?:ebanx|gersonlehrmangroup)/jobs/\d+/?', parts.path):
            return urlunsplit(parts._replace(path=parts.path.rstrip('/')))
        if host == 'careers.airwallex.com' and parts.fragment == 'application' and re.fullmatch(r'/job/[a-f0-9-]{36}/[a-z0-9-]+/?', parts.path):
            return urlunsplit(parts)
    except ValueError:
        pass
    return ''


def public_socket(address, timeout, source_address=None):
    host, port = address
    endpoints = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not endpoints or any(not ip_address(item[4][0]).is_global or ip_address(item[4][0]).is_multicast for item in endpoints):
        raise OSError('Non-public endpoint')
    family, kind, protocol, _, target = endpoints[0]
    connection = socket.socket(family, kind, protocol)
    try:
        connection.settimeout(timeout)
        connection.connect(target)
        return connection
    except OSError:
        connection.close()
        raise


class ApplicationChecker:
    """Limit checks to reviewed ATS routes; an individual failure is inconclusive."""
    agent = 'JobRadar/1.0'

    def __init__(self, limit=6):
        self.remaining = limit
        self.rules = {}
        self.last_request = {}
        self.blocked_hosts = set()

    def get(self, host, path, delay=1.0):
        time.sleep(max(0, delay - (time.monotonic() - self.last_request.get(host, 0))))
        self.last_request[host] = time.monotonic()
        connection = HTTPSConnection(host, timeout=4)
        # Preserve hostname verification and SNI while using only the checked socket address.
        connection._create_connection = public_socket
        try:
            connection.request('GET', path, headers={'User-Agent': self.agent})
            response = connection.getresponse()
            if response.getheader('cf-mitigated') == 'challenge':
                raise HTTPError('', 403, '', response.headers, None)
            if response.status in (404, 410):
                sample = response.read(16_001).decode('utf-8', errors='replace')
                if access_challenge(sample):
                    raise HTTPError('', 403, '', response.headers, None)
            if response.status != 200:
                raise HTTPError('', response.status, '', response.headers, None)
            body = response.read(400_001)
            if len(body) > 400_000:
                raise ValueError('Oversized response')
            return body.decode('utf-8', errors='replace')
        finally:
            connection.close()

    def robots(self, host):
        if host not in self.rules:
            parser = RobotFileParser()
            try:
                raw = self.get(host, '/robots.txt')
            except HTTPError as error:
                if error.code not in (404, 410):
                    raise
                raw = ''
            if raw.lstrip().startswith('<'):
                raise ValueError('Unexpected robots response')
            parser.parse(raw.splitlines())
            self.rules[host] = parser, raw
        return self.rules[host]

    def check(self, url):
        if not self.remaining or not safe_apply_url(url):
            return ''
        from sources import plain, wildcard_disallowed

        parts = urlsplit(url)
        host = parts.netloc
        if host in self.blocked_hosts:
            return ''
        self.remaining -= 1
        try:
            rules, raw = self.robots(host)
            delay = max(1, rules.crawl_delay(self.agent) or 0, rules.crawl_delay('*') or 0)
            request_rate = rules.request_rate(self.agent) or rules.request_rate('*')
            if request_rate and request_rate.requests:
                delay = max(delay, request_rate.seconds / request_rate.requests)
            if delay > 6 or not rules.can_fetch(self.agent, url) or wildcard_disallowed(raw, url):
                self.blocked_hosts.add(host)
                return 'limited'
            try:
                page = self.get(host, parts.path, delay)
            except HTTPError as error:
                if error.code in (404, 410):
                    return 'dead'
                raise
        except HTTPError as error:
            self.blocked_hosts.add(host)
            return 'redirect' if 300 <= error.code < 400 else 'limited'
        except (OSError, HTTPException):
            self.blocked_hosts.add(host)
            return 'network'
        except ValueError:
            self.blocked_hosts.add(host)
            return 'limited'
        text = plain(page)
        if access_challenge(text):
            return 'limited'
        if re.search(r'(?:this (?:job|position) (?:is no longer available|has been filled)|applications? (?:are closed|no longer accepted)|(?:岗位|招聘|投递|申请)(?:已结束|已截止|已关闭))', text, re.I):
            return 'closed'
        forms = re.findall(r'<form\b([^>]*)>(.*?)</form\s*>', page, re.I | re.S)
        if any(form_on_host(attributes, host) and re.search(r'\btype=[\"\'](?:email|file)[\"\']', form, re.I) and
               re.search(r'\b(?:submit application|apply now)\b|提交申请|立即投递', plain(form), re.I) for attributes, form in forms):
            return 'public_form'
        return 'unconfirmed'


def access_challenge(text):
    return re.search(r'captcha|verify (?:you are|that you are) human|验证码|请先登录|access denied|enable javascript', text, re.I)


def form_on_host(attributes, host):
    action = re.search(r'\saction\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s]+))', attributes, re.I)
    value = next((part for part in action.groups() if part is not None), '') if action else ''
    if not value:
        return True
    if not re.fullmatch(r'[A-Za-z0-9_./:-]+', value):
        return False
    return ((value.startswith('/') and not value.startswith('//')) or
            value.startswith(f'https://{host}/'))


def reuse_check(previous, url, today):
    if previous.get('url') == url and previous.get('code') in REASONS and previous.get('checked_at'):
        checked = date.fromisoformat(previous['checked_at'])
        if timedelta(0) <= today - checked < timedelta(days=3):
            return previous
    return None
