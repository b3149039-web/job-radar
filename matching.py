"""Evaluate the stated application requirements before ranking a job."""
import re
import unicodedata

PASS, UNKNOWN, FAIL = '符合', '待核验', '不符合'
LABELS = {'eligible': '符合条件', 'review': '待核验', 'ineligible': '不符合条件'}
PREFERENCE = re.compile(r'优先|优先考虑|择优|加分|为佳|更佳|优先录用')
UNRESTRICTED = re.compile(r'不限|不作要求|无要求|无需|无须|不要求|不限制|不作为硬性')
DEGREES = r'本科|本科学士|学士|硕士(?:研究生)?|博士(?:研究生)?|研究生|大专|专科|本硕|\b(?:JD|LLM|bachelor|master|PhD)\b'
PARTY = re.compile(r'中共(?:预备)?党员|共产党员|预备党员|党员')
CERT = re.compile(r'法律职业资格(?:证书?|[ABＣABC]证)?|法律执业资格证书?|法考(?:证书?)?|国家统一法律职业资格考试|司法考试|司法资格证书?|律师执业证')
LATER = re.compile(r'(?:毕业|入职|报到|录用|转正)(?:时|之?前)|可(?:后续|补交|补办|补提供)|允许.{0,12}(?:补交|补办)')
HARD = re.compile(r'必须|需(?:要)?|须|仅(?:招|限)|限招|要求|持有|具备|已取得|持证')
YEAR = r'(?<!\d)(20\d{2}|\d{2})(?=\s*届)'


def result(state, reason, evidence=''):
    return {'state': state, 'reason': reason, 'evidence': evidence[:160]}


def clauses(text):
    text = unicodedata.normalize('NFKC', text or '').replace(r'\n', '\n')
    text = re.sub(r'\s+(?=\d{1,2}[、.](?!\d))', '\n', text)
    return [re.sub(r'^\s*\d{1,2}[、.)]\s*', '', ' '.join(part.split()))
            for part in re.split(r'[。;；\n]+', text) if part.strip()]


def minimum_degree(text):
    """Extract explicit education terms and ignore preference-only degrees."""
    clean = re.sub(r'[（(][^()（）]*(?:优先|为佳|加分)[^()（）]*[)）]', '', text)
    if clean.strip() == '不限' or re.search(r'学历(?:要求)?\s*[:：]?\s*(?:不限|无要求|不作要求)', clean):
        return 0
    if '本硕' in clean:
        return 2
    alternatives = []
    for phrase in re.split(r'[,，]|、(?=[^、]{0,18}(?:优先|为佳))', clean):
        if PREFERENCE.search(phrase):
            continue
        for degree in re.findall(DEGREES, phrase, re.I):
            alternatives.append(2 if degree.lower() in ('本科', '本科学士', '学士', 'bachelor') else
                                1 if degree in ('大专', '专科') else
                                4 if degree.startswith('博士') or degree.lower() in ('phd', 'jd') else 3)
    return min(alternatives, default=None)


def education_requirement(job, lines):
    checks = []
    explicit_minimums = []
    for line in lines:
        if not re.search(DEGREES, line, re.I) and not re.search(r'学历.{0,5}不限', line):
            continue
        if not (re.search(r'学历|学位|及以上|以上学历|以上学位|本硕|均可|仅招|仅限|限招|最低|全日制|硕士(?:研究生)?应届|博士(?:研究生)?应届|(?:bachelor|master).{0,30}degree', line, re.I)
                or re.fullmatch(r'(?:' + DEGREES + r')', line)
                or re.search(r'本科.{0,5}(?:或|、|/).{0,5}硕士|硕士.{0,5}(?:或|、|/).{0,5}本科', line)):
            continue
        if re.search(r'(?:负责|协助|指导|带教|团队包括)', line) and not re.search(r'学历|学位', line):
            continue
        # An explicit minimum in the same clause survives a preference.
        parts = re.split(r'[,，](?=[^,，]{0,18}(?:优先|为佳))', line)
        degree = minimum_degree(parts[0]) if len(parts) > 1 and not re.search(r'或|\bor\b', parts[-1], re.I) else minimum_degree(line)
        if degree is not None:
            checks.append((degree, line))
            if degree > 2 and re.search(r'仅(?:限|招)|只(?:接受|招)|最低|必须|硕士(?:研究生)?应届|博士(?:研究生)?应届', line):
                explicit_minimums.append((degree, line))
    field = job.get('degree_requirement') or ''
    level = minimum_degree(field)
    # An unrestricted listing field cannot override explicit requirements in the body.
    if level is not None and (level != 0 or not checks):
        checks.append((level, f'学历字段：{field}'))
    below = [value for value, _ in checks if value <= 2]
    above = [(value, line) for value, line in checks if value > 2]
    if above and below:
        if explicit_minimums and not any(value <= 2 for value, line in checks if not line.startswith('学历字段：')):
            degree, evidence = max(explicit_minimums, key=lambda item: item[0])
            return {'minimum': degree, 'conflict': False, 'evidence': evidence[:160]}
        return {'minimum': None, 'conflict': True, 'evidence': ' / '.join(line for _, line in checks)[:160]}
    if above:
        level, evidence = max(above, key=lambda item: item[0])
        return {'minimum': level, 'conflict': False, 'evidence': evidence[:160]}
    return {'minimum': max(below) if below else None, 'conflict': False, 'evidence': checks[0][1][:160] if checks else ''}


def education(job, lines):
    facts = education_requirement(job, lines)
    level, evidence = facts['minimum'], facts['evidence']
    if facts['conflict']:
        return result(UNKNOWN, '学历条件表述冲突', evidence)
    if level is not None and level > 2:
        return result(FAIL, '仅招博士或同等层级' if level == 4 else '仅招硕士及以上', evidence)
    if level is not None:
        return result(PASS, '学历接受本科', evidence)
    return result(UNKNOWN, '学历要求未确认')


def major_requirement(lines):
    for line in lines:
        if re.search(r'专业.{0,3}不限|不限专业', line):
            return {'kind': 'any', 'evidence': line[:160]}
        if re.search(r'工作职责|负责|协助', line) or PREFERENCE.search(line):
            continue
        if re.search(r'法学|法律|民商法|诉讼法', line) and re.search(r'专业|degree in law', line, re.I):
            if re.search(r'或|、|等', line) and re.search(r'会计|金融|审计|管理|经济学|计算机', line):
                continue
            return {'kind': 'law', 'evidence': line[:160]}
        if re.search(r'\bdegree in law\b', line, re.I):
            return {'kind': 'law', 'evidence': line[:160]}
        if re.search(r'专业要求|限.{0,24}专业', line) and re.search(r'会计|财务|审计|计算机|金融', line):
            return {'kind': 'other', 'evidence': line[:160]}
    return {'kind': 'unknown', 'evidence': ''}


def experience_requirement(lines):
    for line in lines:
        if re.search(r'(?:工作经验|工作经历).{0,4}不限|无需.{0,4}(?:经验|经历)', line):
            return {'minimum': 0, 'kind': 'any', 'evidence': line[:160]}
        if PREFERENCE.search(line):
            continue
        match = re.search(r'(\d{1,2})(?:\s*[-–至]\s*(\d{1,2}))?\s*(?:\+\s*)?(?:年.{0,24}(?:经验|经历)|years? (?:of )?experience)', line, re.I)
        if match:
            kind = ('law_firm' if re.search(r'律所|律师事务所|law firm', line, re.I)
                    else 'in_house' if re.search(r'企业法务|in.house', line, re.I) else 'unspecified')
            return {'minimum': int(match[1]), 'maximum': int(match[2]) if match[2] else None,
                    'kind': kind, 'evidence': line[:160]}
    return {'minimum': None, 'kind': 'unknown', 'evidence': ''}


def politics(lines, candidate):
    positive = None
    for line in lines:
        if '政治面貌' in line and UNRESTRICTED.search(line):
            positive = result(PASS, '政治面貌不限', line)
        if not PARTY.search(line):
            continue
        if UNRESTRICTED.search(line) or PREFERENCE.search(line) or '团员' in line:
            positive = result(PASS, '党员为优先项或接受团员', line)
            continue
        if ('政治面貌' in line or re.search(r'须|必须|仅限|要求|应为|需为', line)
                or re.match(r'^(?:中共)?(?:预备)?党员(?:$|[，,、])', line)):
            if not candidate.get('party_member', False):
                return result(FAIL, '要求党员或预备党员', line)
            positive = result(PASS, '符合政治面貌要求', line)
        else:
            positive = result(UNKNOWN, '党员条件需核验', line)
    return positive or result(UNKNOWN, '政治面貌未说明')


def years_in(text):
    # Include common shared-suffix lists and ranges, for example 2026、2027届.
    expanded = re.sub(r'(?<!\d)(20\d{2}|\d{2})\s*([、/或及和~至—-])\s*(?=(?:20\d{2}|\d{2})\s*届)', r'\1届\2', text)
    years = {int(year) + (2000 if len(year) == 2 else 0) for year in re.findall(YEAR, expanded)}
    span = re.search(r'(20\d{2})届\s*(?:至|[-—~])\s*(20\d{2})届', expanded)
    if span and 0 < int(span[2]) - int(span[1]) <= 5:
        years.update(range(int(span[1]), int(span[2]) + 1))
    return years


def graduation(job, lines, target):
    title = job['title']
    campaign = job.get('campaign_title') or ''
    statements = [title, campaign, *lines]
    positive = []
    other = []
    for index, line in enumerate(statements):
        if re.search(r'(?:不(?:招|接收|接受)|排除).{0,8}(?:20)?' + str(target)[-2:] + r'\s*届', line):
            return result(FAIL, '届别不符', line)
        if PREFERENCE.search(line) and not re.search(r'仅限|仅招', line):
            continue
        years = years_in(line)
        if years and (index < 2 or re.search(r'届别|面向|招聘对象|(?:仅|限)招|仅限|届.{0,15}(?:毕业生|应届|可投|可报)', line)):
            (positive if target in years else other).append(line)
        window = re.search(r'(20\d{2})\s*年\s*(\d{1,2})\s*月.{0,12}?(?:至|到|[-—~])\s*(20\d{2})\s*年\s*(\d{1,2})\s*月.{0,14}毕业', line)
        if window:
            start, end = int(window[1]), int(window[3])
            if not start <= target <= end:
                other.append(line)
            elif start < target < end or (start < target == end and int(window[4]) >= 7):
                positive.append(line)
            elif start == target < end and int(window[2]) == 1:
                positive.append(line)
            elif start == target == end and int(window[2]) == 1 and int(window[4]) == 12:
                positive.append(line)
            else:
                return result(UNKNOWN, '需确认毕业月份是否在要求范围内', line)
    restricted = [line for line in other if re.search(r'仅|只招|限招|限于', line)]
    if restricted:
        return result(FAIL, '届别不符', restricted[0])
    if positive:
        return result(PASS, f'接受{target}届', positive[0])
    if other:
        return result(FAIL, '届别不符', other[0])
    return result(UNKNOWN, f'{target}届资格未明确')


def certification(lines, candidate):
    pending = None
    accepted = None
    for line in lines:
        if re.search(r'\bqualified to practice law in (?:the )?(?:PRC|China)\b', line, re.I):
            return result(PASS if candidate.get('legal_practice') else FAIL, '要求中国法律执业资格', line)
        if not CERT.search(line) and '客观题' not in line:
            continue
        if re.search(r'负责|协助|工作职责', line) and not re.search(r'证书|法考', line):
            continue
        if UNRESTRICTED.search(line) or PREFERENCE.search(line):
            accepted = result(PASS, '持证为优先项或无需持证', line)
            continue
        if '客观题' in line and not re.search(r'主观题|证书|持证', line):
            if candidate.get('legal_objective_passed'):
                accepted = result(PASS, '满足法考客观题要求', line)
            else:
                if candidate.get('legal_objective_passed') is False:
                    return result(FAIL, '要求已通过法考客观题', line)
                pending = result(UNKNOWN, '需核验客观题要求', line)
            continue
        if candidate.get('legal_certificate'):
            accepted = result(PASS, '符合持证要求', line)
            continue
        if LATER.search(line):
            pending = result(UNKNOWN, '毕业或入职前取证，待确认', line)
        elif (HARD.search(line) and re.search(r'证书?|资格证|持证', line)) or re.search(r'(?:投递|报名|申请)(?:时|前).{0,16}(?:通过|取得|持有)', line):
            return result(FAIL, '暂不符合，要求投递时持证或通过法考', line)
        elif re.search(r'(?:必须|须|要求|需|已)\s*通过(?:国家统一法律职业资格考试|法考|司法考试)', line):
            return result(FAIL, '要求通过完整法考或司法考试', line)
        elif re.search(r'通过|取得|持证|要求|证书', line):
            pending = result(UNKNOWN, '需确认法考或取证要求及时间', line)
    return pending or accepted or result(UNKNOWN, '法考或证书要求未说明')


def evaluate(job, candidate, target):
    text = job.get('requirement_text') or job.get('summary') or ''
    lines = clauses(text)
    checks = {
        'education': education(job, lines),
        'politics': politics(lines, candidate),
        'cohort': graduation(job, lines, target),
        'certificate': certification(lines, candidate),
    }
    if job.get('unread_attachments'):
        checks['attachment'] = result(UNKNOWN, '招聘附件条件待核验')
    reasons = [check['reason'] for check in checks.values() if check['state'] == FAIL]
    if reasons:
        return 'ineligible', checks, reasons
    pending = [check['reason'] for key, check in checks.items() if check['state'] == UNKNOWN and
               (key in ('education', 'cohort', 'attachment') or check['evidence'])]
    return ('review' if pending else 'eligible'), checks, pending


def preference_rank(job, cities):
    title = job['title']
    group = next((rank for rank, pattern in enumerate(('法务', '法律事务|法律专员', '合规', '合同管理', '风控'), 1)
                  if re.search(pattern, title)), 6)
    if group == 1 and re.search(r'央企|国企|国有|中央企业', job.get('employer_type') or ''):
        group = 0
    city_rank = min((0 if city in cities[:2] else 1 for city in (job.get('city') or '').split('、') if city in cities), default=2)
    return [city_rank, group]
