import json
import sys
from datetime import date
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from collect_jobs import payload, plan, sources
from supabase.generate_seed import salary_terms


class DailyTests(unittest.TestCase):
    def test_robots_wildcard_blocks_filtered_paths(self):
        rules = 'User-agent: *\nDisallow: /*?job_city=\nDisallow: /login$'
        base = 'https://www.asml.com'
        self.assertTrue(sources.wildcard_disallowed(rules, base + '/en/careers/find-your-job?job_city=Shanghai'))
        self.assertTrue(sources.wildcard_disallowed(rules, base + '/login'))
        self.assertFalse(sources.wildcard_disallowed(rules, base + '/en/careers/find-your-job/legal-counsel-j100'))

    def test_lilly_public_posting_uses_detail_date_and_hard_pqe(self):
        posting = {'@type': 'JobPosting', 'identifier': {'value': 'R-101'},
                   'title': 'Associate Director-Legal Counsel',
                   'datePosted': '2026-11-06', 'validThrough': '2026-11-10',
                   'jobLocation': {'address': {'addressLocality': 'Shanghai'}},
                   'description': '<p>Law degree from a university.</p>'
                                  '<p>Minimum 8 years’ post-qualification experience in law firms or in-house.</p>'}
        data = {'jobDetail': {'status': 200, 'data': {'job': {'jobId': 'R-101', 'jobSeqNo': 'TEST101',
            'applyUrl': 'https://lilly.wd115.myworkdayjobs.com/LLY/job/Legal-Counsel_R-101/apply'}}}}
        markup = f'<script type="application/ld+json">{json.dumps(posting)}</script><script>phApp.ddo = {json.dumps(data)}</script>'
        job = sources.parse_lilly_detail(markup, ['上海', '苏州'], date(2026, 11, 14), 'TEST101')
        self.assertEqual((job['source_posted_at'], job['city'], job['check_status']), ('2026-11-06', '上海', '已过期'))
        row = payload({**job, 'url': 'https://careers.lilly.com/us/en/job/TEST101/Legal-Counsel'}, 'fixture')
        facts = row['qualifications']
        self.assertEqual((facts['experience']['minimum'], facts['experience']['kind'], facts['major']['kind']),
                         (8, 'unspecified', 'law'))
        with self.assertRaises(ValueError):
            sources.parse_lilly_detail(markup, ['上海'], date(2026, 11, 14), 'OTHER')

    def test_jnj_reads_qualification_lists_after_education(self):
        markup = '''<li>Job title <span>Sr Legal Counsel</span></li>
          <li>Requisition number <span>R-456</span></li><li>Location <span>Shanghai, China</span></li>
          <strong>Qualifications</strong><p>Education</p><ul><li>Bachelor of Laws (LL.B.)</li>
          <li>Qualified lawyer admitted to practice in China.</li></ul>
          <p>Experience</p><ul><li>Typically 8-10+ years of post-qualification legal experience.</li></ul>
          <p>Core Competencies</p><a href="https://careers.jnj.com.cn/example"><span>Apply now</span></a>'''
        row = payload({**sources.parse_jnj_detail(markup, ['上海'], date(2026, 11, 14)),
                       'url': 'https://www.careers.jnj.com/en/jobs/r-456/sr-legal-counsel/'}, 'fixture')
        facts = row['qualifications']
        self.assertEqual(facts['experience']['minimum'], 8)
        self.assertIsNone(facts['experience']['maximum'])
        self.assertEqual(facts['with_certificate']['reason'], '要求中国法律执业资格')

    def test_asml_and_campus_require_explicit_location_and_open_evidence(self):
        url = 'https://www.asml.com/en/careers/find-your-job/legal-counsel-j100'
        detail = {'detailPageUrl': url, 'id': 'J-100', 'displayJobTitle': 'Legal Counsel',
                  'descriptionExternal': '<p>A minimum of 12 years of legal experience.</p>',
                  'location': 'Shanghai, China', 'datePosted': '2026-11-02', 'status': 'Open',
                  'applyUrl': 'https://asml.wd3.myworkdayjobs.com/Careers/job/Legal-Counsel_J-100/apply'}
        def markup():
            return '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'jobData': detail}}}) + '</script>'
        today = date(2026, 11, 14)
        job = sources.parse_asml_detail(markup(), ['上海'], today, url)
        self.assertEqual(job['check_status'], '官网显示可投递')
        detail['applyUrl'] = 'https://example.test/J-100/apply'
        self.assertEqual(sources.parse_asml_detail(markup(), ['上海'], today, url)['check_status'], '在招待确认')
        detail['status'] = 'Closed'
        self.assertEqual(sources.parse_asml_detail(markup(), ['上海'], today, url)['check_status'], '已结束')
        campus = sources.parse_hebut_detail('''<title>法务专员（2027届校招）-示例企业</title>
          <a class="p2" href="/company/index/id/42.html">【示例企业】</a>
          发布时间：2026-11-10 <p>工作地域：上海市 / 北京市　职位类别：法律专业人员
          学历要求：本科　招聘人数：5人</p><div>专业要求：</div><div class="content">法学</div>
          <div>职位描述：</div><div class="content">审核合同。</div><a onclick="Toudi(21)">投递简历</a>''', ['上海'], today)
        self.assertEqual((campus['cohort'], campus['degree_requirement'], campus['recruitment_type'], campus['check_status']),
                         (2027, '本科', 'campus', '在招待确认'))

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

    def test_team_size_remains_unknown_without_explicit_evidence(self):
        base = dict(title='法务专员', company='示例企业', city='苏州', url='https://example.test/j')
        for requirement, expected in [('现有法务团队共有3人', False), ('负责筹建法务团队', None),
                                      ('岗位是唯一法务', True), ('并非唯一法务', None)]:
            with self.subTest(requirement=requirement):
                row = payload(dict(base, requirement_text=requirement), 'test')
                self.assertIs(row['qualifications']['social']['solo_legal'], expected)
        old = payload(dict(base, requirement_text='唯一法务'), 'original')
        new = payload(dict(base, requirement_text='法务团队目前有3人'), 'refreshed')
        _, changes = plan([new], [old])
        self.assertEqual(changes[0][1]['first_seen'], 'original')
        self.assertIs(changes[0][1]['qualifications']['social']['solo_legal'], False)
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

