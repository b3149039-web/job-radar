"""Evaluate the stated application requirements before ranking a job."""
import re
import unicodedata

PASS, UNKNOWN, FAIL = '符合', '待核验', '不符合'
LABELS = {'eligible': '符合条件', 'review': '待核验', 'ineligible': '不符合条件'}
PREFERENCE = re.compile(r'优先|优先考虑|择优|加分|为佳|更佳|优先录用')
UNRESTRICTED = re.compile(r'不限|不作要求|无要求|无需|无须|不要求|不限制|不作为硬性')
DEGREES = r'本科|本科学士|学士|硕士(?:研究生)?|博士(?:研究生)?|研究生|大专|专科|本硕'
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
    if UNRESTRICTED.search(clean) and not re.search(r'专业不限|年龄不限', clean):
        return 0
    if '本硕' in clean:
        return 2
    alternatives = []
    for phrase in re.split(r'[,，]|、(?=[^、]{0,18}(?:优先|为佳))', clean):
        if PREFERENCE.search(phrase):
            continue
        for degree in re.findall(DEGREES, phrase):
            alternatives.append(2 if degree in ('本科', '本科学士', '学士') else
                                1 if degree in ('大专', '专科') else
                                4 if degree.startswith('博士') else 3)
    return min(alternatives, default=None)


def education(job, lines):
    checks = []
    for line in lines:
        if not re.search(DEGREES, line) and not re.search(r'学历.{0,5}不限', line):
            continue
        if not (re.search(r'学历|学位|及以上|以上学历|以上学位|本硕|均可|仅招|仅限|限招|最低|全日制', line)
                or re.fullmatch(r'(?:' + DEGREES + r')', line)
                or re.search(r'本科.{0,5}(?:或|、|/).{0,5}硕士|硕士.{0,5}(?:或|、|/).{0,5}本科', line)):
            continue
        if re.search(r'(?:负责|协助|指导|带教|团队包括)', line) and not re.search(r'学历|学位', line):
            continue
        # An explicit minimum in the same clause survives a preference.
        parts = re.split(r'[,，](?=[^,，]{0,18}(?:优先|为佳))', line)
        degree = minimum_degree(parts[0]) if len(parts) > 1 and '或' not in line else minimum_degree(line)
        if degree is not None:
            checks.append((degree, line))
    field = job.get('degree_requirement') or ''
    level = minimum_degree(field)
    if level is not None:
        checks.append((level, f'学历字段：{field}'))
    below = [value for value, _ in checks if value <= 2]
    above = [(value, line) for value, line in checks if value > 2]
    if above and below:
        return result(UNKNOWN, '学历条件表述冲突', ' / '.join(line for _, line in checks))
    if above:
        level, evidence = max(above, key=lambda item: item[0])
        return result(FAIL, '仅招博士' if level == 4 else '仅招硕士及以上', evidence)
    if below:
        return result(PASS, '学历接受本科', checks[0][1])
    return result(UNKNOWN, '学历要求未确认')


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
                or re.fullmatch(r'(?:中共)?(?:预备)?党员(?:[（(]含预备党员[）)])?', line)):
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
                pending = result(UNKNOWN, '需核验客观题要求', line)
            continue
        if candidate.get('legal_certificate'):
            accepted = result(PASS, '符合持证要求', line)
            continue
        if LATER.search(line):
            pending = result(UNKNOWN, '毕业或入职前取证，待确认', line)
        elif (HARD.search(line) and re.search(r'证书?|资格证|持证', line)) or re.search(r'(?:投递|报名|申请)(?:时|前).{0,16}(?:通过|取得|持有)', line):
            return result(FAIL, '暂不符合，要求投递时持证或通过法考', line)
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
