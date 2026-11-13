import sys
from datetime import date
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from collect_jobs import payload, plan, sources


class DailyTests(unittest.TestCase):
    def test_official_employer_details(self):
        job = sources.parse_jnj_detail('''
          <li>Job title<span>Legal Counsel</span></li>
          <li>Requisition number<span>R-1234</span></li>
          <li>Location<span>Shanghai / Beijing / China</span></li>
          <li>Date posted<span><time datetime="2026-11-12">Nov 12</time></span></li>
          <p><strong>Qualifications</strong></p><ul><li>Bachelor's degree in Law</li>
          <li>3-5 years of experience</li></ul>
          <a href="https://example.test"><span>Apply now</span></a>
        ''', ['上海', '苏州'], date(2026, 11, 13))
        self.assertEqual((job['city'], job['recruitment_type'], job['posted_at'], job['check_status']),
                         ('上海', 'experienced', '2026-11-12', '官网显示可投递'))
        self.assertEqual(job['experience_requirement'], '3-5 years of experience')
        self.assertIsNone(job['cohort'])

    def test_append_only_dedup_and_status(self):
        def row(url, **fields):
            return payload({'company': 'Fixture', 'title': '法务', 'city': '上海', 'source': 'fixture',
                            'url': url, 'cohort': 2027, 'check_status': '在招待确认',
                            'requirement_text': '3年工作经验', **fields}, '2026-11-13T09:17:00+08:00')

        existing = row('https://example.test/1')
        repost = row('https://example.test/2')
        refreshed = {**existing, 'check_status': '已过期', 'company': 'Ignored title edit'}
        next_year = {**row('https://example.test/3'), 'cohort': 2028}
        counts, changes = plan([repost, refreshed, next_year, next_year], [existing])
        self.assertEqual(counts, {'new': 1, 'updated': 1, 'duplicates': 2, 'inactive': 1, 'needs_review': 1})
        self.assertEqual([(kind, item['id']) for kind, item in changes],
                         [('updated', existing['id']), ('new', next_year['id'])])
        self.assertEqual(existing['check_status'], '在招待确认')

    def test_historical_inactive_status_is_preserved(self):
        row = payload({'company': 'Fixture', 'title': '法务', 'city': '上海', 'source': 'fixture',
                       'url': 'https://example.test/old', 'check_status': '在招待确认'}, 'fixture')
        counts, changes = plan([row], [{**row, 'check_status': '已结束'}])
        self.assertEqual((counts['duplicates'], counts['inactive'], counts['needs_review']), (1, 1, 0))
        self.assertEqual(changes, [])

