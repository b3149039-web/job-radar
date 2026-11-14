import sys
from datetime import date
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from collect_jobs import payload, plan, sources
from supabase.generate_seed import salary_terms


class DailyTests(unittest.TestCase):
    def test_ncss_header_evidence_keeps_update_separate_from_publication(self):
        today = date(2026, 11, 13)
        markup = '''<li id="jobName">法务专员</li><li>——</li><li>法务专员</li>
          <li>8.3k-8.3k | 不限 | 招聘 1 人</li><li><span>10-11 02:04</span>更新</li>
          <span class="source-sp">示例大学</span><div class="site-tag">江苏省苏州市</div>
          <a href="#">投递简历</a><h2>职位详情</h2>
          {"gwzz":"负责起草和审核与公司业务相关的法律文件，包括各类合同、协议等",
           "rzzg":"本科及以上学历，持法律执业资格证书者优先","xzfl":"岗位工资加年终绩效奖金"}
        '''
        time = int(sources.datetime(2026, 10, 11, 2, 4, tzinfo=sources.CST).timestamp() * 1000)
        job = sources.parse_ncss_detail(markup, {'recName': '示例企业', 'publishDate': time, 'updateDate': time},
                                        ['苏州'], today)
        row = payload({**job, 'url': 'https://example.test/role'}, '2026-11-13')
        evidence = row['qualifications']['source_details']
        self.assertEqual((evidence['headcount'], evidence['salary_display'], evidence['publisher']), (1, '8.3k-8.3k', '示例大学'))
        self.assertEqual((evidence['source_posted_at'], evidence['source_updated_at'], evidence['platform_listed_at']),
                         ('', '2026-10-11', '2026-10-11'))
        self.assertEqual(len(evidence['grouping_hash']), 64)
        self.assertEqual(row['check_status'], '在招待确认')
        other = payload({**job, 'url': 'https://example.test/second'}, '2026-11-13')
        self.assertEqual(plan([row, other], [])[0]['new'], 2)
        self.assertEqual(plan([{**row, 'first_seen': '2026-11-14'}], [row])[1], [])
    def test_salary_requires_explicit_components_and_solo_evidence(self):
        self.assertIsNone(salary_terms('月薪10万元以上')['monthly'])
        self.assertIsNone(salary_terms('月薪1万元，最多14薪')['months'])
        self.assertFalse(salary_terms('月薪1万元 13薪')['annual_complete'])
        self.assertTrue(salary_terms('税前年总包13–15万元')['annual_complete'])
        self.assertFalse(salary_terms('年总包13–15万元，含浮动绩效')['annual_complete'])
        self.assertFalse(salary_terms('年总包13–15万元，奖金另计')['annual_complete'])
        self.assertIsNone(salary_terms('月薪 USD 10000元').get('monthly'))
        row = payload(dict(title='法务专员', company='示例集团', city='苏州', url='https://example.test/j',
                           requirement_text='唯一法务\n主要负责电话催收', salary_text='月薪1–2万元 13薪'), 'test')
        facts = row['qualifications']['social']
        self.assertEqual((facts['role_kind'], facts['solo_legal'], facts['salary']['monthly']),
                         ('collections', True, [10000, 20000]))
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
                            'requisition': 'fixture-2027',
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

    def test_same_title_different_campaigns_are_kept_and_refresh_is_idempotent(self):
        job = dict(company='Fixture', title='法务', city='上海', source='fixture', cohort=2027,
                   url='https://example.test/a', posted_at='2026-11-12', campaign_title='2027届秋招一批',
                   check_status='在招待确认', requirement_text='学历要求：本科及以上')
        first = payload(job, 'original')
        different = payload({**job, 'url': 'https://example.test/b', 'campaign_title': '2027届秋招二批'}, 'today')
        richer = payload({**job, 'requirement_text': '学历要求：本科及以上\n要求3年企业法务工作经验'}, 'today')
        counts, changes = plan([richer, different], [first], date(2026, 11, 13), count_similar=True)
        self.assertEqual((counts['new'], counts['updated'], counts['same_title_unconfirmed']), (1, 1, 1))
        updated = changes[0][1]
        self.assertEqual(updated['first_seen'], 'original')
        self.assertEqual(updated['qualifications']['experience']['minimum'], 3)
        self.assertEqual(plan([richer, different], [row for _, row in changes])[1], [])

    def test_official_deadline_outweighs_apply_button(self):
        self.assertEqual(sources.status_for('Application deadline: 2026-11-01', date(2026, 11, 13)),
                         ('已过期', '2026-11-01'))

