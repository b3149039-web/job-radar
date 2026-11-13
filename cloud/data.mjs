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

export function matchJob(job, profile) {
  const reasons = [];
  const excluded = [];
  const facts = job.qualifications || {};
  const fields = [];
  if (job.recruitment_type !== 'unknown' && job.recruitment_type !== profile.search_mode) {
    excluded.push(profile.search_mode === 'campus' ? '社招岗位' : '校招岗位');
  } else if (job.recruitment_type === 'unknown') reasons.push('招聘类型待核验');
  if (profile.cities.length && !job.city.split('、').some(city => profile.cities.includes(city))) {
    if (job.city) excluded.push('城市不在意向范围');
    else reasons.push('工作城市待核验');
  }
  if (profile.keywords.length && !profile.keywords.some(word => job.title.includes(word))) {
    excluded.push('职位未匹配关键词');
  }
  if (profile.bachelor === true) fields.push(facts.bachelor || {state: '待核验', reason: '学历要求未确认'});
  if (profile.search_mode === 'campus') {
    if (profile.target_cohort === 2027 && facts.cohort_2027) fields.push(facts.cohort_2027);
    else if (job.cohort !== profile.target_cohort || !job.cohort) reasons.push('目标届别待核验');
  }
  if (profile.party_member === false && facts.non_party_member) fields.push(facts.non_party_member);
  if (profile.legal_certificate === false) {
    const cert = profile.legal_objective_passed === true ? facts.objective_only : facts.without_certificate;
    if (cert) fields.push(cert);
  }
  if (facts.attachment_unverified) reasons.push('招聘附件条件待核验');
  for (const field of fields) {
    if (field.state === '不符合') excluded.push(field.reason);
    if (field.state === '待核验' && (field.evidence || field === facts.bachelor || field === facts.cohort_2027 ||
        field.reason === '学历要求未确认')) reasons.push(field.reason);
  }
  if (profile.search_mode === 'experienced') reasons.push('社招资格以原招聘页为准');
  return {category: excluded.length ? 'ineligible' : reasons.length ? 'review' : 'eligible', reasons: [...excluded, ...reasons]};
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
