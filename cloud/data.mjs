export const stages = ['未处理', '待投递', '已投递', '面试中', '不合适'];
export const interviewStatuses = ['待安排', '已安排', '已完成', '已取消'];
export const blankProfile = {
  search_mode: 'experienced', cities: [], keywords: ['法务', '法律事务', '合规'],
  target_cohort: null, bachelor: null, party_member: null,
  legal_objective_passed: null, legal_certificate: null,
};
export function validateConfig(config) {
  if (!/^https:\/\/[a-z0-9-]+\.supabase\.co$/.test(config?.url || '') ||
      !/^sb_publishable_[A-Za-z0-9_-]+$/.test(config?.publishableKey || '') ||
      config.publishableKey === 'sb_publishable_REPLACE_ME') {
    throw new Error('请配置 Supabase Project URL 和 sb_publishable_ 开头的公开密钥。');
  }
  return config;
}

export function legacyRows(raw) {
  if (typeof raw !== 'string' || raw.length > 500000) throw new Error('备份文件过大或无法读取。');
  let parsed;
  try { parsed = JSON.parse(raw); } catch { throw new Error('备份文件不是有效的 JSON。'); }
  if (parsed?.format !== 'jobradar-legacy-stages' || parsed.version !== 1 ||
      !parsed.records || typeof parsed.records !== 'object' || Array.isArray(parsed.records)) {
    throw new Error('请选择旧版网页导出的投递状态备份。');
  }
  const entries = Object.entries(parsed.records);
  if (entries.length > 2000 || entries.some(([id, stage]) => !/^[a-f0-9]{16}$/.test(id) || !stages.includes(stage))) {
    throw new Error('备份含无效岗位 ID 或投递状态。');
  }
  return entries.map(([job_id, stage]) => ({job_id, stage}));
}

export function planLegacyImport(rows, jobs, activity) {
  const known = new Set(jobs.map(job => job.id));
  const existing = new Map(activity.map(row => [row.job_id, row.stage]));
  const plan = {ready: [], identical: [], conflicts: [], unknown: []};
  for (const row of rows) {
    if (!known.has(row.job_id)) plan.unknown.push(row);
    else if (!existing.has(row.job_id)) plan.ready.push(row);
    else if (existing.get(row.job_id) === row.stage) plan.identical.push(row);
    else plan.conflicts.push({...row, cloud_stage: existing.get(row.job_id)});
  }
  return plan;
}

export function matchJob(job, profile, today = shanghaiToday()) {
  const reasons = [];
  const excluded = [];
  const advantages = [];
  const social = profile.search_mode === 'experienced' ? profile.social_preferences : null;
  const role = jobRole(job);
  const jobSocial = job.qualifications?.social ?? {};
  const skillHits = (social?.skills ?? []).filter(skill => jobSocial.skills?.includes(skill)).length;
  if (role === 'legal') advantages.push('岗位职责方向与法律职业发展相关');
  if (skillHits) advantages.push('技能匹配：' + [...new Set(social.skills)].filter(skill => jobSocial.skills?.includes(skill)).join('、'));
  if (['collections', 'sales', 'administrative'].includes(role)) excluded.push(
    {collections: '催收或清收岗位', sales: '销售类岗位', administrative: '普通行政岗位'}[role]);
  if (role === 'unknown') reasons.push('法律相关职责待核验');
  let stretch = false;
  if (social) {
    if (social.excluded_types?.includes(role)) excluded.push('岗位类型在个人排除范围');
    if (social.excluded_keywords?.some(word => matchesJobKeyword(job, word))) excluded.push('命中个人排除关键词');
    if (role === 'government' && social.include_government === false) excluded.push('未纳入政府事务类岗位');
    const unqualifiedTitle = job.title.replace(/(?:并非|不是|非)\s*(?:唯一法务|一人法务)/g, '');
    const solo = /唯一法务|一人法务|sole (?:in-house )?(?:lawyer|counsel)/i.test(unqualifiedTitle) ? true : jobSocial.solo_legal;
    if (social.accept_solo === false && solo === true) excluded.push('明确为一人法务');
    if (social.accept_solo === false && role === 'legal' && solo == null) reasons.push('法务团队配置待核验');
    if (social.accept_solo === false && role === 'legal' && solo === false) advantages.push('有多人法务团队证据');
    const pay = compensation(jobSocial.salary);
    if (social.minimum_annual != null) {
      if (pay.ceiling != null && pay.ceiling < social.minimum_annual) excluded.push('公开年总包上限低于底线');
      else if (pay.floor == null || pay.floor < social.minimum_annual) reasons.push('年总包是否达到底线待核验');
      else advantages.push('已核实的年薪下限达到个人底线');
    }
  }
  const status = recruitmentStatus(job, today);
  if (status === '已截止' || status === '无法访问') excluded.push(status);
  else if (status !== '确认在招') reasons.push('是否仍在招聘待核验');
  else advantages.push('官网显示可投递');
  const facts = job.qualifications || {};
  const fields = [];
  if (job.recruitment_type !== 'unknown' && job.recruitment_type !== profile.search_mode) {
    excluded.push(profile.search_mode === 'campus' ? '社招岗位' : '校招岗位');
  } else if (job.recruitment_type === 'unknown') reasons.push('招聘类型待核验');
  if (profile.cities.length && !job.city.split('、').some(city => profile.cities.includes(city))) {
    if (job.city) excluded.push('城市不在意向范围');
    else reasons.push('工作城市待核验');
  }
  if (profile.keywords.length && !profile.keywords.some(word => matchesJobKeyword(job, word)) &&
      !(social?.include_government === true && role === 'government')) {
    excluded.push('职位未匹配关键词');
  }
  const degree = profile.degree_level ?? (profile.bachelor === true ? 2 : null);
  if (facts.education?.conflict) reasons.push('学历字段与任职正文有冲突');
  else if (facts.education?.minimum > 0) {
    if (degree === null) reasons.push('个人学历待设置');
    else if (degree < facts.education.minimum) excluded.push('学历低于岗位最低要求');
    else advantages.push('符合明示的最低学历');
  } else if (facts.education?.minimum !== 0) {
    if (degree === 2) fields.push(facts.bachelor || {state: '待核验', reason: '学历要求未确认'});
    else if (facts.bachelor?.state === '不符合' && degree === 1) fields.push(facts.bachelor);
    else reasons.push('学历匹配待核验');
  }
  const major = profile.major_kind;
  if (facts.major?.kind === 'law') {
    if (major === 'other') excluded.push('岗位要求法学相关专业');
    else if (!major) reasons.push('个人专业待设置');
    else advantages.push('法学相关专业匹配');
  } else if (facts.major?.kind === 'other') {
    reasons.push('请核对岗位限定的专业范围');
  }
  const experience = facts.experience;
  const years = experienceYears(profile, today);
  if (experience?.minimum != null && social) {
    if ((social.experience_max != null && experience.minimum > social.experience_max) ||
        (social.experience_min != null && experience.maximum != null && experience.maximum < social.experience_min)) {
      reasons.push('岗位年限范围超出个人偏好');
    }
  }
  if (experience?.minimum > 0) {
    if (years == null) reasons.push('个人相关工作年限待设置');
    else if (years < experience.minimum) {
      if (social && skillHits >= 2) {
        stretch = true;
        reasons.push('正式工作年限未达要求，有至少两项技能证据，可尝试');
      } else excluded.push('相关工作年限不足');
    } else if (experience.maximum == null || years <= experience.maximum) advantages.push('工作年限达到明示要求');
    if (social && experience.maximum != null && years != null && years > experience.maximum) {
      stretch = true;
      reasons.push('个人年限超过岗位公布区间，需确认是否接受');
    }
    if (['law_firm', 'in_house'].includes(experience.kind)) {
      if (!profile.experience_kind) reasons.push('相关工作经历类型待核验');
      else if (![experience.kind, 'both'].includes(profile.experience_kind)) excluded.push(
        experience.kind === 'law_firm' ? '岗位明确要求律所经历' : '岗位明确要求企业法务经历');
      else if (profile.experience_kind === 'both') reasons.push('需核对指定经历类型的实际年限');
    }
  } else if (profile.search_mode === 'experienced' && experience?.minimum !== 0) reasons.push('岗位工作经验要求待核验');
  if (profile.search_mode === 'campus') {
    const evidence = facts.cohort_evidence?.find(item => item.year === profile.target_cohort) ??
      (profile.target_cohort === 2027 ? facts.cohort_2027 : null);
    if (evidence) fields.push(evidence);
    else if (profile.target_cohort && job.cohort && job.cohort !== profile.target_cohort) excluded.push('届别不符');
    else if (!job.cohort || !profile.target_cohort) reasons.push('目标届别待核验');
    if ((evidence?.state === '符合' || (!evidence && profile.target_cohort === job.cohort)) &&
        profile.target_cohort) advantages.push('明确接受 ' + profile.target_cohort + ' 届');
  }
  if (profile.party_member === false && facts.non_party_member) fields.push(facts.non_party_member);
  if (profile.party_member == null && facts.non_party_member?.state === '不符合') reasons.push('岗位要求党员，个人政治面貌待设置');
  if (facts.with_certificate?.reason === '要求中国法律执业资格') {
    if (profile.legal_practice === false) excluded.push('岗位要求中国法律执业资格');
    else if (profile.legal_practice == null) reasons.push('个人法律执业资格待设置');
  } else if (profile.legal_certificate === false || profile.legal_certificate === true) {
    const cert = profile.legal_certificate === true ? facts.with_certificate :
      profile.legal_objective_passed === true ? facts.objective_only :
        profile.legal_objective_passed === false ? (facts.neither_exam_nor_certificate ?? facts.without_certificate) :
          facts.without_certificate;
    if (cert) fields.push(cert);
  } else if (facts.without_certificate?.state === '不符合' || facts.without_certificate?.evidence) {
    reasons.push('岗位有法考或持证门槛，个人资格待设置');
  }
  if (facts.attachment_unverified) reasons.push('招聘附件条件待核验');
  for (const field of fields) {
    if (field.state === '不符合') excluded.push(field.reason);
    if (field.state === '待核验' && (field.evidence || field === facts.bachelor || field === facts.cohort_2027 || field.year ||
        field.reason === '学历要求未确认')) reasons.push(field.reason);
  }
  return {category: excluded.length ? 'ineligible' : stretch ? 'stretch' : reasons.length ? 'review' : 'eligible',
    reasons: [...excluded, ...reasons], exclusions: excluded, unknowns: reasons, advantages};
}

export function experienceYears(profile, today = shanghaiToday()) {
  const start = profile.social_preferences?.employment_start;
  if (profile.search_mode !== 'experienced' || !/^\d{4}-(0[1-9]|1[0-2])(?:-\d{2})?$/.test(start ?? '')) {
    return profile.experience_years ?? null;
  }
  // Month-only input counts from the end of that month, so it cannot overstate tenure.
  const [year, month, day] = start.split('-').map(Number);
  const startDay = day ?? new Date(Date.UTC(year, month, 0)).getUTCDate();
  if (!validDate(start.length === 7 ? start + '-01' : start) || !validDate(today)) return null;
  const [currentYear, currentMonth, currentDay] = today.split('-').map(Number);
  const months = (currentYear - year) * 12 + currentMonth - month - Number(currentDay < startDay);
  return months >= 0 ? months / 12 : null;
}

function range(value) {
  return Array.isArray(value) && value.length === 2 && value.every(n => Number.isFinite(n) && n >= 0) &&
    value[0] <= value[1] ? value : null;
}

export function compensation(salary) {
  if (salary?.currency !== 'CNY') return {floor: null, ceiling: null};
  const annual = range(salary.annual);
  const monthly = range(salary.monthly);
  const bonus = range(salary.guaranteed_bonus);
  if (annual) return {floor: salary.annual_complete === true && !salary.variable_performance ? annual[0] : null,
    ceiling: salary.annual_complete === true ? annual[1] : null};
  if (!monthly || !Number.isInteger(salary.months) || salary.months < 12 || salary.months > 24) {
    return {floor: null, ceiling: null};
  }
  const floor = monthly[0] * salary.months + (bonus?.[0] ?? 0);
  return {floor, ceiling: salary.annual_complete === true && !salary.variable_performance ?
    monthly[1] * salary.months + (bonus?.[1] ?? 0) : null};
}

function jobRole(job) {
  const title = job.title;
  const kind = job.qualifications?.social?.role_kind;
  if (kind === 'collections' || /催收|催款|清收/.test(title)) return 'collections';
  if (kind === 'government' || /政府事务|政府关系|公共事务|监管政策研究|government affairs|public affairs/i.test(title)) return 'government';
  if (kind === 'sales' || /^(?:法律咨询|法务服务|法律服务)?(?:销售|电销)(?:专员|顾问|经理)?$/.test(title)) return 'sales';
  if (/法务|法律|合规|合同管理|\blegal\b|\bcompliance\b|\bcontracts? (?:counsel|manager|specialist|analyst)/i.test(title)) return 'legal';
  if (kind === 'sales' || /销售|电销|\bsales\b/i.test(title)) return 'sales';
  if (/行政|文员|\badministrative\b/i.test(title)) return kind === 'legal' ? 'legal' : 'administrative';
  return kind === 'legal' ? 'legal' : 'unknown';
}

function campusEmployerPriority(job, profile) {
  if (profile.search_mode !== 'campus' || jobRole(job) !== 'legal') return 2;
  const employer = [job.employer_type, ...(job.qualifications?.social?.employer_tags ?? [])].join(' ');
  if (profile.prefer_state_owned !== false && /国有|央企|国企|中央企业/.test(employer)) return 0;
  return /大型企业|上市公司|上市企业|集团/.test(employer) ? 1 : 2;
}

export function socialPriority(job, profile) {
  const social = profile.search_mode === 'experienced' && profile.social_preferences;
  if (!social) return [100, 0, 0];
  const facts = job.qualifications?.social;
  const tags = facts?.employer_tags ?? [];
  const employerAliases = {外资: /外商独资|外资企业|外商投资/, 上市公司: /上市公司|上市企业/,
    集团: /集团|group/i, 央国企: /央企|国企|国有|中央企业/};
  const priority = (social.employer_priority ?? []).findIndex(tag => tags.includes(tag) || job.employer_type === tag ||
    (Object.hasOwn(employerAliases, tag) && employerAliases[tag].test(job.employer_type + ' ' + job.company)));
  const experience = job.qualifications?.experience;
  const within = experience?.minimum != null && (social.experience_min != null || social.experience_max != null) &&
    (social.experience_min == null || experience.minimum >= social.experience_min) &&
    (social.experience_max == null || experience.minimum <= social.experience_max);
  const monthly = facts?.salary?.currency === 'CNY' && range(facts.salary.monthly);
  const ideal = monthly && social.ideal_monthly_min != null && social.ideal_monthly_max != null &&
    monthly[1] >= social.ideal_monthly_min && monthly[0] <= social.ideal_monthly_max;
  return [priority < 0 ? 100 : priority, within ? -1 : 0,
    Number(ideal === true) * -2 - Number(social.pay_months != null && facts?.salary?.months === social.pay_months) -
      (social.skills ?? []).filter(skill => facts?.skills?.includes(skill)).length];
}

export function passesSocialFilters(job, profile, today = shanghaiToday()) {
  const social = profile.search_mode === 'experienced' && profile.social_preferences;
  if (!social) return true;
  if (social.verified_only && recruitmentStatus(job, today) !== '确认在招') return false;
  // Current adapters supply no reliable recruiter activity timestamps.
  if (social.hr_filter === 'verified') return false;
  return publishedWithin(job, social.recent_days, today);
}

const matchOrder = {eligible: 0, review: 1, stretch: 2, ineligible: 3};
const viewCategories = {recommended: 'eligible', review: 'review', stretch: 'stretch'};

export function listJobs(jobs, profile, {view = 'recommended', publishedDays = 0,
  query = '', records = new Map(), today = shanghaiToday()} = {}) {
  const ranked = [];
  for (const job of jobs) {
    if (!publishedWithin(job, publishedDays, today) ||
        (query && !(job.company + ' ' + job.title).toLowerCase().includes(query))) continue;
    const record = records.get(job.id);
    const match = matchJob(job, profile, today);
    if (view === 'saved' ? !record?.favorite : view !== 'all' &&
        (match.category !== viewCategories[view] || !passesSocialFilters(job, profile, today))) continue;

    const city = profile.cities.findIndex(value => job.city.split('、').includes(value));
    const keyword = profile.keywords.findIndex(value => matchesJobKeyword(job, value));
    const legal = jobRole(job) === 'legal';
    const order = [
      matchOrder[match.category],
      legal ? 0 : 1,
      recruitmentStatus(job, today) === '确认在招' ? 0 : 1,
      match.unknowns.length,
      -match.advantages.length,
      freshness(job, today, profile.search_mode === 'experienced').rank,
      ...socialPriority(job, profile),
      campusEmployerPriority(job, profile),
      keyword < 0 ? Infinity : keyword,
      city < 0 ? Infinity : city,
    ];
    ranked.push({job, match, record, order});
  }
  ranked.sort((a, b) => compareOrder(a.order, b.order));
  if (view !== 'recommended' || profile.cities[0] !== '上海') return ranked;

  // The target mix only affects jobs tied on every criterion before city.
  const display = [];
  for (let start = 0; start < ranked.length;) {
    const quality = ranked[start].order.slice(0, -1);
    let end = start + 1;
    while (end < ranked.length && compareOrder(quality, ranked[end].order) === 0) end++;
    const equal = ranked.slice(start, end);
    const shanghai = equal.filter(item => item.order.at(-1) === 0);
    const nearby = equal.filter(item => /^(苏州|无锡|常州)$/.test(profile.cities[item.order.at(-1)] ?? ''));
    const regional = new Set([...shanghai, ...nearby]);
    let left = 0, right = 0;
    while (left < shanghai.length && right < nearby.length) {
      display.push(...shanghai.slice(left, left + 4), nearby[right++]);
      left += 4;
    }
    display.push(...shanghai.slice(left), ...nearby.slice(right), ...equal.filter(item => !regional.has(item)));
    start = end;
  }
  return display;
}

function compareOrder(left, right) {
  for (const [i, value] of left.entries()) {
    if (value !== right[i]) return value < right[i] ? -1 : 1;
  }
  return 0;
}

function shanghaiToday() {
  return new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
}

function validDate(date) {
  const value = Date.parse(date + 'T00:00:00Z');
  return /^\d{4}-\d{2}-\d{2}$/.test(date ?? '') && Number.isFinite(value) &&
    new Date(value).toISOString().slice(0, 10) === date;
}

export function sourcePostedAt(job) {
  const details = job.qualifications?.source_details;
  const posted = details?.source_posted_at ?? (job.source === '国家大学生就业服务平台' ? '' : details?.posted_at);
  return validDate(posted) ? posted : '';
}

export function publishedWithin(job, days, today = shanghaiToday()) {
  if (!days) return true;
  const posted = sourcePostedAt(job);
  return [7, 14, 30].includes(days) && validDate(today) && Boolean(posted) &&
    posted <= today && (Date.parse(today) - Date.parse(posted)) / 86400000 <= days;
}

export function recruitmentStatus(job, today = shanghaiToday()) {
  const deadline = job.qualifications?.source_details?.deadline;
  if (job.check_status === '无法访问') return '无法访问';
  if (['已结束', '已过期'].includes(job.check_status) || (validDate(deadline) && deadline < today)) return '已截止';
  return job.check_status === '官网显示可投递' ? '确认在招' : '待核验';
}

export function duplicateGroupKey(job) {
  const d = job.qualifications?.source_details;
  if (!d || !/^[a-f0-9]{64}$/.test(d.grouping_hash ?? '') || !d.headcount || !d.degree ||
      !d.salary_display || !job.company || !job.title || !job.city) return null;
  const posted = sourcePostedAt(job);
  const updateDay = validDate(d.source_updated_at) ? d.source_updated_at.slice(5) : d.source_update_label?.slice(0, 5);
  // Same-cohort campus text can be reused across waves. Require a dated matching wave.
  const campusDay = job.recruitment_type === 'campus' ? (posted || d.source_updated_at || d.platform_listed_at) : '';
  if (job.recruitment_type === 'campus' && (!validDate(campusDay) || (updateDay && campusDay.slice(5) !== updateDay))) return null;
  if (job.recruitment_type !== 'campus' && !d.requisition && !validDate(d.source_updated_at) &&
      !validDate(d.platform_listed_at) && !posted) return null;
  const norm = value => String(value ?? '').normalize('NFKC').trim().toLowerCase();
  return JSON.stringify([job.company, job.title, job.city, job.cohort, job.recruitment_type, d.campaign,
    d.batch, d.department, d.requisition, d.deadline, d.headcount, d.degree, d.salary_display, d.grouping_hash,
    job.qualifications.education?.minimum, job.qualifications.education?.conflict,
    job.qualifications.attachment_unverified, campusDay].map(norm));
}

export function freshness(job, today = shanghaiToday(), includeUpdates = false) {
  const details = job.qualifications?.source_details;
  const published = sourcePostedAt(job);
  const update = details?.source_updated_at;
  const updated = includeUpdates && validDate(update) && update <= today && (!published || update > published);
  const posted = updated ? update : published;
  if (!validDate(posted) || !validDate(today) || posted > today) return {rank: 4, label: '发布日期未知'};
  const age = (Date.parse(today) - Date.parse(posted)) / 86400000;
  const event = updated ? '来源更新' : '发布';
  if (age > 90) return {rank: 5, label: event + '超过90天'};
  if (age > 60) return {rank: 3, label: event + '超过60天'};
  if (age > 30) return {rank: 2, label: event + '超过30天'};
  return {rank: age <= 7 ? 0 : 1, label: (age <= 7 ? '近7天' : '近30天') + event};
}

const legalTitleAliases = {
  法务: /\blegal (?:counsel|affairs|specialist|manager|analyst)\b/i,
  法律事务: /\blegal (?:counsel|affairs|specialist|manager|analyst)\b/i,
  合规: /\bcompliance (?:officer|counsel|manager|specialist|analyst)\b/i,
  合同管理: /\bcontracts? (?:counsel|manager|specialist|analyst)\b/i,
};

export function matchesJobKeyword(job, word) {
  return job.title.toLowerCase().includes(word.toLowerCase()) ||
    (Object.hasOwn(legalTitleAliases, word) && legalTitleAliases[word].test(job.title));
}

function unwrap(result) {
  if (result.error) throw result.error;
  return result.data;
}

async function fetchPages(query, order) {
  const rows = [];
  for (let offset = 0; ; offset += 100) {
    const page = unwrap(await query().order(order).range(offset, offset + 99));
    rows.push(...page);
    if (page.length < 100) return rows;
  }
}

export function createCloudStore(client, onChange) {
  let account = null;
  let generation = 0;
  let loadSequence = 0;
  let current = null;

  function setAccount(user) {
    if (account?.id === user?.id) return false;
    generation += 1;
    account = user || null;
    current = null;
    onChange(null);
    return true;
  }

  async function refresh() {
    if (!account) return null;
    const version = generation;
    const seq = ++loadSequence;
    const owner = account.id;
    const [profile, jobs, activity, interviews] = await Promise.all([
      client.from('profiles').select('*').eq('owner_id', owner).maybeSingle().then(unwrap),
      fetchPages(() => client.from('jobs').select('*'), 'id'),
      fetchPages(() => client.from('job_activity').select('*').eq('owner_id', owner), 'job_id'),
      fetchPages(() => client.from('interviews').select('*').eq('owner_id', owner), 'id'),
    ]);
    if (version !== generation || seq !== loadSequence) return null;
    current = {profile, jobs, activity, interviews};
    onChange(current);
    return current;
  }

  function scope() {
    if (!account || !current) throw new Error('请等待账号数据加载完成。');
    const version = generation;
    return {owner: account.id, assertCurrent() {
      if (version !== generation) throw new Error('账号已切换，请重新操作。');
    }};
  }

  async function saveVersioned(table, identity, fields, revision, session = scope()) {
    session.assertCurrent();
    const keys = {...identity, owner_id: session.owner};
    const query = client.from(table);
    const write = revision === null
      ? query.insert({...keys, ...fields})
      : query.update(fields).match(keys).eq('revision', revision);
    const response = await write.select('revision').maybeSingle();
    session.assertCurrent();
    if (response.error?.code === '23505' || (!response.error && !response.data)) {
      await refresh();
      throw Object.assign(new Error('另一台设备已更新这条记录。已刷新，请确认内容后重试。'),
        {code: 'REVISION_CONFLICT'});
    }
    unwrap(response);
  }

  return {
    setAccount, refresh,
    snapshot: () => current,
    async saveProfile(fields, revision) {
      const session = scope();
      await saveVersioned('profiles', {}, fields, revision, session);
      session.assertCurrent();
      await refresh();
    },
    async saveActivity(jobId, fields, revision) {
      const session = scope();
      await saveVersioned('job_activity', {job_id: jobId}, fields, revision, session);
      session.assertCurrent();
      await refresh();
    },
    async saveInterview(jobId, fields, record = null) {
      const session = scope();
      if (record) {
        await saveVersioned('interviews', {id: record.id}, fields, record.revision, session);
      } else {
        unwrap(await client.from('job_activity').upsert(
          {owner_id: session.owner, job_id: jobId},
          {onConflict: 'owner_id,job_id', ignoreDuplicates: true},
        ));
        session.assertCurrent();
        unwrap(await client.from('interviews').insert({owner_id: session.owner, job_id: jobId, ...fields}));
      }
      session.assertCurrent();
      await refresh();
    },
    async importLegacy(plan, expectedOwner) {
      const session = scope();
      if (session.owner !== expectedOwner) throw new Error('账号已切换，请重新预览备份。');
      if (!plan.ready.length) return 0;
      const inserted = unwrap(await client.from('job_activity').upsert(
        plan.ready.map(row => ({owner_id: session.owner, job_id: row.job_id, stage: row.stage})),
        {onConflict: 'owner_id,job_id', ignoreDuplicates: true},
      ).select('job_id'));
      session.assertCurrent();
      await refresh();
      return inserted.length;
    },
  };
}
