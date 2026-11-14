"""Export the shared catalog without candidate profile or private activity."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import matching

FIELDS = ('id', 'company', 'title', 'city', 'source', 'url', 'summary', 'cohort',
          'recruitment_type', 'employer_type', 'check_status', 'qualifications', 'first_seen')

SKILLS = {
    '合同审查': r'合同审核|合同审查|合同起草|contract (?:review|drafting)',
    '金融合规': r'金融合规|金融监管|汽车金融',
    '公司治理': r'公司治理|corporate governance',
    '工商登记': r'工商(?:登记|变更)|企业登记',
    '数据合规': r'数据合规|个人信息保护|data privacy',
    '内控培训': r'内控培训|合规培训|法律培训|compliance training',
    '争议解决': r'争议解决|纠纷处理|dispute resolution',
    '监管政策研究': r'监管政策研究|regulatory policy',
    '知识产权': r'知识产权|intellectual property',
}


def job_preferences(job, lines):
    title = job['title']
    text = '\n'.join(lines)
    employer = job.get('employer_type') or ''
    legal = re.search(r'法务|法律|合规|合同管理|legal|compliance|contract (?:manager|specialist)', title, re.I)
    collection_duty = re.search(r'^(?:主要|核心)?(?:负责|从事|职责[:：]?)\s*(?:电话催款|电话催收|批量清收|批量催收)', text, re.M)
    kind = ('collections' if re.search(r'催收|催款|清收', title) or collection_duty else
            'government' if re.search(r'政府事务|政府关系|公共事务|监管政策研究|government affairs|public affairs', title, re.I) else
            'legal' if legal else 'sales' if re.search(r'销售|电销|sales', title, re.I) else 'unknown')
    solo = next((line for line in [title, *lines] if re.search(r'唯一法务|一人法务|法务团队仅.{0,2}一人|sole (?:in-house )?(?:lawyer|counsel)', line, re.I)
                 and not re.search(r'(?:并非|不是|非)\s*(?:唯一法务|一人法务)', line)), '')
    staffed = next((line for line in lines if re.search(
        r'法务团队(?:目前|现有|共有|已有|共|有|由|人数为|规模为)*\s*(?:[2-9]|[1-9]\d+|[二两三四五六七八九十]+)\s*(?:人|名|位)',
        line)), '')
    tags = [key for key, pattern in {
        '外资': r'外商独资|外资企业|外商投资|合资企业',
        '上市公司': r'上市公司|上市企业', '集团': r'集团|group',
        '中大型企业': r'中大型企业|大型企业', '成熟法务团队': r'成熟法务团队|完善的法务团队',
        '央国企': r'央企|国企|国有企业|中央企业',
    }.items() if re.search(pattern, employer + ' ' + (job.get('company') or ''), re.I)]
    # Only a source-supplied, clearly delimited salary field is eligible for arithmetic.
    salary_text = redact_contacts(job.get('salary_text'))[:220]
    return {
        'role_kind': kind, 'solo_legal': True if solo else False if staffed else None,
        'solo_evidence': redact_contacts(solo or staffed)[:160],
        'employer_tags': tags, 'skills': [key for key, pattern in SKILLS.items() if re.search(pattern, text, re.I)],
        'salary': salary_terms(salary_text), 'hr_activity': None,
    }


def salary_terms(text):
    # Conservative RMB parser. Unknown, open-ended and variable components remain unset.
    clean = text.replace(',', '').replace('，', ' ').strip()
    if re.search(r'美元|港币|港元|USD|HKD|EUR|欧元|税后', clean, re.I):
        return {'evidence': text}
    def amount(label):
        m = re.search(label + r'\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:[-–—至~～]\s*(\d+(?:\.\d+)?)\s*)?(万元|万|千|[kK]|元)', clean)
        if not m or re.match(r'\s*(?:起|以上|以内|以下|\+|左右|约)', clean[m.end():]):
            return None
        scale = {'万元': 10000, '万': 10000, '千': 1000, 'k': 1000, '元': 1}[m[3].lower()]
        low, high = int(float(m[1]) * scale), int(float(m[2] or m[1]) * scale)
        return [low, high] if 0 < low <= high else None
    months = re.search(r'(?<!\d)(1[0-9]|2[0-4])\s*薪', clean)
    bonus = amount(r'(?:年度|每年)(?:保证|固定)奖金')
    variable = re.search(r'浮动绩效|绩效奖金|浮动奖金|另有奖金|提成|年终奖|另议|奖金另计', clean)
    annual = amount(r'(?:税前)?年(?:度)?总包')
    annual_complete = not variable and not re.search(r'另计|另有|另议|另发|预估|预计|约|最高可', clean) and bool(
        annual or re.search(r'总包仅含|无(?:其他|额外)?奖金|无其他收入', clean))
    months_guaranteed = months and not re.search(r'视绩效|根据绩效|最高|最多|不保证|保底\d+薪|含绩效', clean)
    return {'currency': 'CNY', 'monthly': amount(r'(?:税前)?月薪'), 'annual': annual,
            'annual_complete': annual_complete, 'months': int(months[1]) if months_guaranteed else None,
            'guaranteed_bonus': bonus, 'variable_performance': True if variable else None, 'evidence': text}


def redact_contacts(text):
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[邮箱已省略]', text or '')
    return re.sub(r'(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}[- ]\d{7,8}(?!\d)', '[电话已省略]', text)


def public_row(job):
    lines = matching.clauses(job.get('requirement_text') or job.get('summary') or '')
    qualifications = {
        'bachelor': matching.education(job, lines),
        'education': matching.education_requirement(job, lines),
        'major': matching.major_requirement(lines),
        'experience': matching.experience_requirement(lines),
        'social': job_preferences(job, lines),
        'cohort_2027': matching.graduation(job, lines, 2027),
        'non_party_member': matching.politics(lines, {'party_member': False}),
        'without_certificate': matching.certification(lines, {}),
        'objective_only': matching.certification(lines, {'legal_objective_passed': True}),
        'with_certificate': matching.certification(lines, {'legal_certificate': True, 'legal_objective_passed': True}),
        'neither_exam_nor_certificate': matching.certification(lines, {'legal_objective_passed': False}),
        'cohort_evidence': [matching.graduation(job, lines, year) | {'year': year}
                            for year in sorted({year for line in [job['title'], job.get('campaign_title') or '', *lines]
                                                for year in matching.years_in(line)} | {2027})],
        'attachment_unverified': bool(job.get('unread_attachments')),
    }
    for value in qualifications.values():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and 'evidence' in item:
                item['evidence'] = redact_contacts(item['evidence'])
    label = ' '.join(job.get(key) or '' for key in ('title', 'campaign_title'))
    recruitment_type = ('campus' if job.get('cohort') or re.search(r'校招|校园招聘|应届', label)
                        else 'experienced' if re.search(r'社招|社会招聘', label) else 'unknown')
    row = {key: job.get(key) or '' for key in FIELDS if key not in ('cohort', 'qualifications')}
    row.update(cohort=job.get('cohort'), recruitment_type=job.get('recruitment_type') or recruitment_type,
               qualifications=qualifications)
    # Keep the structured evidence. Free-form snippets may contain recruiter contact details.
    row['summary'] = ''
    return row


def build_seed(jobs):
    rows = [public_row(job) for job in jobs]
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    delimiter = '$jobradar_seed$'
    while delimiter in payload:
        delimiter = delimiter[:-1] + '_$'
    columns = ', '.join(FIELDS)
    return (
        '-- Shared recruitment catalog only. Re-running preserves existing rows.\n'
        'begin;\n'
        f'insert into public.jobs ({columns})\n'
        f'select {columns}\n'
        f'from jsonb_populate_recordset(null::public.jobs, {delimiter}\n{payload}\n{delimiter}::jsonb)\n'
        'on conflict (id) do nothing;\n'
        'commit;\n'
    )


if __name__ == '__main__':
    source = json.loads((ROOT / 'jobs.json').read_text(encoding='utf-8'))
    (Path(__file__).with_name('seed_jobs.sql')).write_text(build_seed(source['jobs']), encoding='utf-8')
