"""Shared stable identifiers and conservative position matching."""
import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_url(url):
    try:
        parts = urlsplit((url or '').strip())
        if parts.scheme not in ('https', 'http') or not parts.netloc:
            return ''
        tracking = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid'}
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                 if key.lower() not in tracking]
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip('/') or '/', urlencode(query), ''))
    except ValueError:
        return ''


def job_id(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]


def identity(job):
    def key(value):
        return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value).casefold())

    title = re.sub(r'[（(](?:20)?\d{2}\s*届(?:校招|秋招|校园招聘)?[）)]$', '', job['title'])
    parts = tuple(key(job.get(field) or '') for field in ('company', 'city')) + (key(title),)
    return parts if all(parts) else None
