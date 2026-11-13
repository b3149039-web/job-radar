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


def redact_contacts(text):
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[邮箱已省略]', text or '')
    return re.sub(r'(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}[- ]\d{7,8}(?!\d)', '[电话已省略]', text)


def public_row(job):
    lines = matching.clauses(job.get('requirement_text') or job.get('summary') or '')
    qualifications = {
        'bachelor': matching.education(job, lines),
        'cohort_2027': matching.graduation(job, lines, 2027),
        'non_party_member': matching.politics(lines, {'party_member': False}),
        'without_certificate': matching.certification(lines, {}),
        'objective_only': matching.certification(lines, {'legal_objective_passed': True}),
        'attachment_unverified': bool(job.get('unread_attachments')),
    }
    for value in qualifications.values():
        if isinstance(value, dict):
            value['evidence'] = redact_contacts(value['evidence'])
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
