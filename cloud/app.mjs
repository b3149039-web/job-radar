import {blankProfile, compensation, createCloudStore, duplicateGroupKey, experienceYears, freshness, interviewStatuses,
  legacyRows, listJobs, planLegacyImport, recruitmentStatus, sourcePostedAt, stages, validateConfig} from './data.mjs';

const $ = id => document.getElementById(id);
const node = (tag, text = '', className = '') => {
  const element = document.createElement(tag);
  element.textContent = text;
  element.className = className;
  return element;
};
const button = (label, action) => {
  const control = node('button', label);
  control.type = 'button';
  control.addEventListener('click', action);
  return control;
};
function choices(values, current, label) {
  const control = node('select');
  control.setAttribute('aria-label', label);
  for (const value of values) {
    const option = node('option', value);
    option.value = value;
    control.append(option);
  }
  control.value = current;
  return control;
}

const labels = {eligible: '符合条件', review: '待核验', stretch: '可尝试', ineligible: '不符合条件'};
const socialFields = {
  'employment-start': 'employment_start', 'minimum-annual': 'minimum_annual', 'ideal-monthly-min': 'ideal_monthly_min',
  'ideal-monthly-max': 'ideal_monthly_max', 'pay-months': 'pay_months', 'employer-priority': 'employer_priority',
  'excluded-keywords': 'excluded_keywords', 'excluded-types': 'excluded_types', 'accept-solo': 'accept_solo',
  'experience-min': 'experience_min', 'experience-max': 'experience_max', 'social-skills': 'skills',
  'include-government': 'include_government', 'recent-days': 'recent_days', 'verified-only': 'verified_only',
  'hr-filter': 'hr_filter',
};
let sdk, store, account, pendingEmail, importPreview;
let fileSequence = 0;
let action = null;
let refreshInProgress = false;
let authGeneration = 0;
let authReady = false;
let verification = null;
let profileRevision = null;
let profileDirty = false;
let lastSignature = '';

function message(error) {
  if (error?.code === '42501') return '此账号无权执行该操作。';
  if (error?.code === 'PGRST205') return '数据库表尚未配置，请先执行迁移 SQL。';
  return error?.message || '操作失败，请重试。';
}

function loginError(error) {
  if (error?.code === 'invalid_credentials' || error?.message === 'Invalid login credentials') return '邮箱或密码错误。';
  if (error?.code === 'email_not_confirmed') return '该邮箱尚未确认，请联系账号管理员。';
  if (error?.code === 'otp_expired') return '验证码或登录链接已过期，请重新发送。';
  if (error?.status === 429) return '请求过于频繁，请稍后再试。';
  return '登录暂未完成。请检查输入和网络后重试。';
}

function updateAuthControls() {
  const disabled = !authReady || Boolean(verification) || Boolean(action);
  for (const id of ['login-password', 'send-email', 'login-code', 'show-password']) $(id).disabled = disabled;
  $('logout').disabled = Boolean(action);
  $('login').setAttribute('aria-busy', String(disabled));
}

async function perform(task, {status = 'status', success = '', redraw = false,
  pending = '正在处理…', formatError = message} = {}) {
  if (action) return;
  const marker = {session: authGeneration};
  action = marker;
  $(status).textContent = pending;
  updateAuthControls();
  try {
    const result = await task();
    if (marker.session === authGeneration && success !== null) {
      $(status).textContent = typeof success === 'function' ? success(result) : success;
    }
  } catch (error) {
    if (marker.session === authGeneration) $(status).textContent = formatError(error);
  } finally {
    if (marker === action) action = null;
    updateAuthControls();
    if (redraw && marker.session === authGeneration) renderJobs(true);
  }
}

function setProfile(profile) {
  const value = profile || blankProfile;
  $('mode').value = value.search_mode;
  $('cities').value = value.cities.join('、');
  $('keywords').value = value.keywords.join('、');
  $('cohort').value = value.target_cohort ?? '';
  $('degree-level').value = value.degree_level ?? (value.bachelor === true ? '2' : '');
  for (const [id, field] of [['major-kind', 'major_kind'], ['experience-years', 'experience_years'],
    ['experience-kind', 'experience_kind'], ['legal-practice', 'legal_practice'],
    ['prefer-state-owned', 'prefer_state_owned'], ['party-member', 'party_member'],
    ['objective', 'legal_objective_passed'], ['certificate', 'legal_certificate']]) {
    $(id).value = value[field] === null || value[field] === undefined ? '' : String(value[field]);
  }
  const preferences = value.social_preferences ?? {};
  for (const [id, field] of Object.entries(socialFields)) {
    if (id === 'excluded-types') {
      for (const option of $(id).options) option.selected = preferences[field]?.includes(option.value) ?? false;
    } else {
      const setting = preferences[field];
      $(id).value = Array.isArray(setting) ? setting.join('、') : String(setting ?? '');
    }
  }
  $('social-settings').open = false;
  $('social-settings').hidden = value.search_mode !== 'experienced';
}

function clearPreview() {
  fileSequence += 1;
  importPreview = null;
  $('legacy-file').value = '';
  $('legacy-preview').replaceChildren();
  $('import').disabled = true;
}

function maskPassword() {
  $('password').type = 'password';
  $('show-password').textContent = '显示密码';
}

function setAccount(user) {
  if (account?.id === user?.id) return;
  authGeneration += 1;
  account = user;
  action = null;
  pendingEmail = null;
  profileDirty = false;
  profileRevision = null;
  lastSignature = '';
  $('email').value = '';
  $('password').value = '';
  maskPassword();
  $('otp-email').value = '';
  $('email-alternative').open = false;
  $('code').value = '';
  $('search').value = '';
  $('view').value = 'recommended';
  $('published-window').value = '';
  $('verify-code').hidden = true;
  $('account').textContent = user?.email || '';
  $('status').textContent = '';
  $('jobs').replaceChildren();
  $('profile-form').inert = true;
  setProfile(null);
  clearPreview();
  $('login').hidden = Boolean(user);
  $('logout').hidden = !user;
  $('dashboard').hidden = !user;
  store.setAccount(user);
  if (user) {
    $('status').textContent = '正在读取个人记录…';
    const generation = authGeneration;
    setTimeout(() => { if (generation === authGeneration) refresh(true); }, 0);
  } else {
    $('login-message').textContent = '已退出当前设备。';
  }
}

function receiveSession(session, initialMessage) {
  authReady = true;
  if (!session?.user?.id) {
    verification = null;
    const wasSignedIn = Boolean(account);
    setAccount(null);
    if (!wasSignedIn) $('login-message').textContent = initialMessage || '请使用管理员已创建的账号登录。';
    updateAuthControls();
    return;
  }
  const id = session.user.id;
  if (account?.id === id || verification?.id === id) return;
  setAccount(null);
  const attempt = {id};
  verification = attempt;
  $('login-message').textContent = '正在确认登录身份…';
  updateAuthControls();
  // Leave the SDK auth callback before calling another auth method.
  setTimeout(async () => {
    if (verification !== attempt) return;
    try {
      const {data, error} = await sdk.auth.getUser();
      if (error || data?.user?.id !== id) throw new Error('Unverified session');
      if (verification === attempt) setAccount(data.user);
    } catch {
      if (verification === attempt) $('login-message').textContent = '无法确认当前登录身份，请检查网络后重新登录。';
    } finally {
      if (verification === attempt) {
        verification = null;
        updateAuthControls();
      }
    }
  }, 0);
}

function onData(data) {
  if (!data) return;
  if (!profileDirty) {
    profileRevision = data.profile?.revision ?? null;
    setProfile(data.profile);
  }
  $('profile-form').inert = false;
  if (!$('jobs').querySelector('[data-dirty]')) renderJobs();
}

async function refresh(showStatus = false) {
  if (!account || action || refreshInProgress) return;
  const generation = authGeneration;
  refreshInProgress = true;
  try {
    const data = await store.refresh();
    if (showStatus && data && generation === authGeneration) $('status').textContent = '已同步。';
  } catch (error) {
    if (generation === authGeneration) $('status').textContent = message(error);
  } finally {
    refreshInProgress = false;
    if (generation !== authGeneration && account) refresh(true);
  }
}

function renderJobs(force = false) {
  const data = store.snapshot();
  if (!data || !account) return;
  const profile = data.profile || blankProfile;
  const filter = $('view').value;
  const publishedWindow = Number($('published-window').value);
  const query = $('search').value.trim().toLowerCase();
  const signature = JSON.stringify([profile.revision, filter, query, publishedWindow,
    new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10),
    data.jobs,
    data.activity.map(row => [row.job_id, row.revision]), data.interviews.map(row => [row.id, row.revision])]);
  if (!force && lastSignature === signature) return;
  lastSignature = signature;
  const records = new Map(data.activity.map(row => [row.job_id, row]));
  const jobs = listJobs(data.jobs, profile, {view: filter, publishedDays: publishedWindow, query, records});
  const openGroups = new Set([...$('jobs').querySelectorAll('.duplicate-group[open]')].map(group => group.dataset.key));
  $('jobs').replaceChildren();
  if (!data.profile) $('jobs').append(node('p', '请先保存你的求职条件。可从校招模板开始填写。', 'muted'));
  if (profile.social_preferences?.employment_start && profile.search_mode === 'experienced') {
    const years = experienceYears(profile);
    if (years !== null) $('jobs').append(node('p', '按当前正式入职日期计算：' + Math.round(years * 12) + '个月', 'muted'));
  }
  if (!jobs.length) $('jobs').append(node('p', '当前范围内没有岗位。'));
  const groupKeys = new Map(data.jobs.map(job => [job.id, duplicateGroupKey(job)]));
  const originalGroups = new Map();
  for (const job of data.jobs) {
    const key = groupKeys.get(job.id);
    if (!key) continue;
    if (!originalGroups.has(key)) originalGroups.set(key, []);
    originalGroups.get(key).push(job);
  }
  const groups = new Map();
  for (const item of jobs) {
    const key = groupKeys.get(item.job.id) ?? item.job.id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  for (const [key, members] of groups) {
    const first = members[0].job;
    const originals = originalGroups.get(key) ?? [];
    const destination = originals.length < 2 ? $('jobs') : node('details', '', 'panel duplicate-group');
    if (originals.length > 1) {
      destination.dataset.key = key;
      destination.open = openGroups.has(key);
      destination.append(node('summary', `疑似重复 · ${first.company} · ${first.title}（${originals.length} 条原记录）`));
      if (members.length < originals.length) {
        const visibleIds = new Set(members.map(({job}) => job.id));
        destination.append(node('p', '以下原记录未匹配当前筛选，可打开原页核对。', 'muted'));
        const links = node('ul');
        for (const job of originals.filter(job => !visibleIds.has(job.id))) {
          const item = node('li');
          const issuer = job.qualifications?.source_details?.publisher || job.source;
          const link = originalLink(job, [job.title, issuer, '原招聘页'].filter(Boolean).join(' · '));
          item.append(link || node('span', job.title + ' · 原始链接缺失'));
          links.append(item);
        }
        destination.append(links);
      }
      $('jobs').append(destination);
    }
    for (const {job, match, record} of members) {
      destination.append(renderJob(job, match, record, data.interviews.filter(row => row.job_id === job.id)));
    }
  }
}

function originalLink(job, label = '原招聘页') {
  if (!/^https?:\/\//i.test(job.url)) return null;
  const link = node('a', label);
  link.href = job.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function renderJob(job, match, record, interviews) {
  const card = node('article', '', 'panel');
  const heading = node('div', '', 'job-head');
  heading.append(node('h2', job.title), node('span', labels[match.category], 'tag ' + match.category));
  const status = recruitmentStatus(job);
  const evidence = job.qualifications?.source_details;
  card.append(heading, node('p', [job.company, job.city, status].filter(Boolean).join(' · '), 'muted'));
  card.append(node('p', [freshness(job).label, 'HR 活跃度暂无数据'].join(' · '), 'muted'));
  if (freshness(job).rank >= 2 && status === '待核验') {
    card.append(node('p', '投递前请核对原页是否仍开放。', 'muted'));
  }
  if (evidence) {
    const posted = sourcePostedAt(job);
    const update = evidence.source_updated_at || evidence.source_update_label;
    const education = job.qualifications?.education;
    const degreeLabels = ['不限', '专科', '本科', '硕士', '博士或同等层级'];
    const degree = education?.conflict ? '学历表述冲突，待核验' :
      education?.minimum > 0 ? '最低学历：' + degreeLabels[education.minimum] :
        evidence.degree || '学历待核验';
    card.append(node('p', [posted && '来源发布 ' + posted, update && '来源更新 ' + update, degree,
      evidence.experience || '经验待核验', evidence.deadline && '截止 ' + evidence.deadline]
      .filter(Boolean).join(' · '), 'muted'));
  }
  if (job.first_seen) card.append(node('p', 'JobRadar 首次发现 ' + job.first_seen, 'muted'));
  card.append(node('p', [job.source, evidence?.publisher, evidence?.status_evidence,
    evidence?.status_checked_at && '状态核验 ' + evidence.status_checked_at].filter(Boolean).join(' · '), 'muted'));
  const facts = job.qualifications?.social;
  const salary = facts?.salary;
  const pay = compensation(salary);
  if (facts?.employer_tags?.length) card.append(node('p', facts.employer_tags.join(' · '), 'muted'));
  if (salary?.evidence) card.append(node('p', [salary.evidence,
    pay.floor != null && '可确认年收入下限 ¥' + pay.floor.toLocaleString('zh-CN'),
    pay.ceiling != null && '可确认总包上限 ¥' + pay.ceiling.toLocaleString('zh-CN')]
    .filter(Boolean).join(' · '), 'muted'));
  else card.append(node('p', '薪资未公开或待核验', 'muted'));
  if (match.advantages.length) card.append(node('p', '匹配依据与优势：' + match.advantages.join(' · ')));
  if (match.unknowns.length) card.append(node('p', '待核验：' + match.unknowns.join(' · ')));
  if (match.exclusions.length) card.append(node('p', '排除理由：' + match.exclusions.join(' · ')));
  const link = originalLink(job);
  if (link) card.append(link);
  const save = fields => perform(() => store.saveActivity(job.id, fields, record?.revision ?? null),
    {success: '已保存到当前账号。', redraw: true});
  const actions = node('div', '', 'actions');
  const star = button(record?.favorite ? '已收藏' : '收藏', () => save({favorite: !record?.favorite}));
  star.setAttribute('aria-pressed', String(Boolean(record?.favorite)));
  const stage = choices(stages, record?.stage || stages[0], job.title + '投递状态');
  stage.addEventListener('change', () => save({stage: stage.value}));
  actions.append(star, stage);
  card.append(actions);
  const details = node('details');
  details.append(node('summary', '个人备注与面试'));
  const notes = node('textarea');
  notes.maxLength = 4000;
  notes.value = record?.notes || '';
  notes.setAttribute('aria-label', '个人备注');
  notes.addEventListener('input', () => { notes.dataset.dirty = 'true'; });
  details.append(notes, button('保存备注', () => save({notes: notes.value})));
  for (const interview of interviews) details.append(interviewForm(job.id, interview));
  details.append(interviewForm(job.id));
  card.append(details);
  return card;
}

function interviewForm(jobId, record = null) {
  const form = node('form', '', 'actions');
  const round = node('input');
  round.placeholder = '面试轮次';
  round.setAttribute('aria-label', '面试轮次');
  round.required = true;
  round.maxLength = 100;
  round.value = record?.round || '';
  const status = choices(interviewStatuses, record?.status || interviewStatuses[0], '面试进度');
  const time = node('input');
  time.type = 'datetime-local';
  time.setAttribute('aria-label', '面试时间（设备当地时区）');
  if (record?.scheduled_at) {
    const date = new Date(record.scheduled_at);
    time.value = new Date(date.valueOf() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  const notes = node('textarea');
  notes.maxLength = 2000;
  notes.placeholder = '面试备注';
  notes.setAttribute('aria-label', '面试备注');
  notes.value = record?.notes || '';
  const submit = node('button', record ? '更新面试' : '添加面试');
  submit.type = 'submit';
  form.append(round, status, time, notes, submit);
  form.addEventListener('input', () => { form.dataset.dirty = 'true'; });
  form.addEventListener('submit', event => {
    event.preventDefault();
    perform(() => store.saveInterview(jobId, {round: round.value.trim(), status: status.value,
      scheduled_at: time.value ? new Date(time.value).toISOString() : null, notes: notes.value}, record),
    {success: '面试进度已保存。', redraw: true});
  });
  return form;
}

function showPreview(plan) {
  const preview = $('legacy-preview');
  preview.replaceChildren(node('p', '迁入账号：' + account.email),
    node('p', '可导入 ' + plan.ready.length + ' 条 · 已相同 ' + plan.identical.length +
      ' 条 · 冲突 ' + plan.conflicts.length + ' 条 · 找不到岗位 ' + plan.unknown.length + ' 条'));
  const list = new Map(store.snapshot().jobs.map(job => [job.id, job.title]));
  if (plan.conflicts.length || plan.unknown.length) {
    const skipped = node('details');
    skipped.append(node('summary', '保留原备份中的冲突和未关联记录'));
    for (const row of plan.conflicts) skipped.append(node('p', (list.get(row.job_id) || row.job_id) +
      ' · 备份 ' + row.stage + ' · 云端 ' + row.cloud_stage));
    for (const row of plan.unknown) skipped.append(node('p', row.job_id + ' · ' + row.stage));
    preview.append(skipped);
  }
  $('import').disabled = !plan.ready.length;
}

function setupHandlers() {
  $('show-password').addEventListener('click', () => {
    if ($('password').type === 'text') maskPassword();
    else {
      $('password').type = 'text';
      $('show-password').textContent = '隐藏密码';
    }
  });
  $('password-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!authReady || verification || account) return;
    const email = $('email').value.trim();
    const password = $('password').value;
    perform(async () => {
      try {
        const {data, error} = await sdk.auth.signInWithPassword({email, password});
        if (error) throw error;
        if (data.session) receiveSession(data.session);
        else throw new Error('Missing session');
      } finally {
        $('password').value = '';
        maskPassword();
      }
    }, {status: 'login-message', pending: '正在登录…', success: null, formatError: loginError});
  });
  $('email-alternative').addEventListener('toggle', () => {
    if ($('email-alternative').open && !$('otp-email').value) $('otp-email').value = $('email').value;
  });
  $('send-code').addEventListener('submit', event => {
    event.preventDefault();
    if (!authReady || verification || account) return;
    const email = $('otp-email').value.trim();
    const generation = authGeneration;
    perform(async () => {
      const {error} = await sdk.auth.signInWithOtp({email, options: {
        shouldCreateUser: false, emailRedirectTo: new URL('.', location.href).href,
      }});
      if (error) throw error;
      if (generation !== authGeneration) return;
      pendingEmail = email;
      $('verify-code').hidden = false;
      $('code').value = '';
      $('code').focus();
    }, {status: 'login-message', pending: '正在发送邮件…', formatError: loginError,
      success: '请查收邮件。有验证码可在下方输入，有登录链接请在当前浏览器打开。'});
  });
  $('verify-code').addEventListener('submit', event => {
    event.preventDefault();
    if (!pendingEmail || verification || account) return;
    perform(async () => {
      const {data, error} = await sdk.auth.verifyOtp({email: pendingEmail, token: $('code').value, type: 'email'});
      if (error) throw error;
      if (data.session) receiveSession(data.session);
      else throw new Error('Missing session');
    }, {status: 'login-message', pending: '正在验证…', success: null, formatError: loginError});
  });
  $('logout').addEventListener('click', () => perform(async () => {
    const {error} = await sdk.auth.signOut({scope: 'local'});
    if (error) throw error;
    if (account) receiveSession(null);
  }, {pending: '正在退出…'}));
  $('sync').addEventListener('click', () => refresh(true));
  $('search').addEventListener('input', () => renderJobs(true));
  $('view').addEventListener('change', () => renderJobs(true));
  $('published-window').addEventListener('change', () => renderJobs(true));
  $('profile-form').addEventListener('input', () => { profileDirty = true; });
  $('mode').addEventListener('change', () => { $('social-settings').hidden = $('mode').value !== 'experienced'; });
  $('campus-preset').addEventListener('click', () => {
    $('mode').value = 'campus';
    $('social-settings').hidden = true;
    profileDirty = true;
  });
  $('profile-form').addEventListener('submit', event => {
    event.preventDefault();
    const list = id => [...new Set($(id).value.split(/[、,，]/).map(text => text.trim()).filter(Boolean))];
    const flag = id => $(id).value === '' ? null : $(id).value === 'true';
    const number = id => $(id).value === '' ? null : Number($(id).value);
    const degree = number('degree-level');
    const fields = {search_mode: $('mode').value, cities: list('cities'), keywords: list('keywords'),
      target_cohort: number('cohort'), bachelor: degree === 2 ? true : null, degree_level: degree,
      major_kind: $('major-kind').value || null, experience_years: number('experience-years'),
      experience_kind: $('experience-kind').value || null, legal_practice: flag('legal-practice'),
      prefer_state_owned: flag('prefer-state-owned'),
      party_member: flag('party-member'), legal_objective_passed: flag('objective'), legal_certificate: flag('certificate')};
    if (fields.search_mode === 'experienced') {
      const start = $('employment-start').value.trim();
      const date = start.length === 7 ? start + '-01' : start;
      const parsed = new Date(date + 'T00:00:00Z');
      const monthlyMin = number('ideal-monthly-min'), monthlyMax = number('ideal-monthly-max');
      const expMin = number('experience-min'), expMax = number('experience-max');
      if ((start && (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date ||
          date > new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10))) ||
          (monthlyMin != null && monthlyMax != null && monthlyMin > monthlyMax) ||
          (expMin != null && expMax != null && expMin > expMax)) {
        $('status').textContent = '请核对入职日期和区间上下限。';
        return;
      }
      const preferences = {
        employment_start: start || null, minimum_annual: number('minimum-annual'),
        ideal_monthly_min: monthlyMin, ideal_monthly_max: monthlyMax, pay_months: number('pay-months'),
        employer_priority: list('employer-priority'), excluded_keywords: list('excluded-keywords'),
        excluded_types: Array.from($('excluded-types').selectedOptions, option => option.value),
        accept_solo: flag('accept-solo'), experience_min: expMin, experience_max: expMax,
        skills: list('social-skills'), include_government: flag('include-government'),
        recent_days: number('recent-days'), verified_only: flag('verified-only'), hr_filter: $('hr-filter').value || null,
      };
      if (store.snapshot()?.profile?.social_preferences != null || Object.values(preferences).some(
        value => value != null && (!Array.isArray(value) || value.length))) {
        fields.social_preferences = preferences;
      }
    }
    const generation = authGeneration;
    perform(async () => {
      try {
        await store.saveProfile(fields, profileRevision);
      } catch (error) {
        if (generation === authGeneration && error.code === 'REVISION_CONFLICT') {
          profileDirty = false;
          profileRevision = store.snapshot()?.profile?.revision ?? null;
          setProfile(store.snapshot()?.profile);
        }
        throw error;
      }
      if (generation === authGeneration) {
        profileDirty = false;
        profileRevision = store.snapshot()?.profile?.revision ?? null;
        setProfile(store.snapshot()?.profile);
      }
    }, {success: '求职条件已同步。', redraw: true});
  });
  $('legacy-file').addEventListener('change', async () => {
    const file = $('legacy-file').files[0];
    clearPreview();
    const generation = authGeneration;
    const sequence = fileSequence;
    try {
      if (!file || !account || !store.snapshot()) return;
      if (file.size > 500000) throw new Error('备份文件过大。');
      const rows = legacyRows(await file.text());
      if (generation !== authGeneration || sequence !== fileSequence) return;
      const data = store.snapshot();
      importPreview = {owner: account.id, rows, plan: planLegacyImport(rows, data.jobs, data.activity)};
      showPreview(importPreview.plan);
    } catch (error) {
      if (generation === authGeneration && sequence === fileSequence) $('legacy-preview').textContent = message(error);
    }
  });
  $('import').addEventListener('click', () => {
    if (!importPreview) return;
    const preview = importPreview;
    const generation = authGeneration;
    perform(async () => {
      const added = await store.importLegacy(preview.plan, preview.owner);
      if (generation === authGeneration) {
        const data = store.snapshot();
        preview.plan = planLegacyImport(preview.rows, data.jobs, data.activity);
        showPreview(preview.plan);
      }
      return added;
    }, {success: count => '已导入 ' + count + ' 条新记录，已有记录保留。', redraw: true});
  });
  $('export').addEventListener('click', () => perform(async () => {
    const data = await store.refresh();
    if (!data || !account) throw new Error('无法读取当前账号的数据。');
    const backup = {format: 'jobradar-private-backup', version: 1, exported_at: new Date().toISOString(),
      profile: data.profile, activity: data.activity, interviews: data.interviews};
    const url = URL.createObjectURL(new Blob([JSON.stringify(backup, null, 2)], {type: 'application/json'}));
    const download = node('a');
    download.href = url;
    download.download = 'jobradar-private-backup.json';
    document.body.append(download);
    download.click();
    download.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, {success: '备份已请求下载，请保存在私人目录。'}));
}

async function start() {
  const url = new URL(location.href);
  const code = url.searchParams.get('code');
  const fragment = new URLSearchParams(url.hash.slice(1));
  let callbackMessage = url.searchParams.has('error') || fragment.has('error') || fragment.has('access_token')
    ? '邮件链接未能完成登录，请重新发送登录邮件或使用密码。' : '';
  if (code || callbackMessage) history.replaceState(null, '', url.pathname);
  setupHandlers();
  try {
    const response = await fetch('config.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('无法读取本机配置。请从本地服务打开 JobRadar。');
    const config = validateConfig(await response.json());
    if (!globalThis.supabase?.createClient) throw new Error('缺少本机 Supabase SDK，请按配置指南安装项目依赖。');
    sdk = globalThis.supabase.createClient(config.url, config.publishableKey, {
      auth: {persistSession: true, autoRefreshToken: true, flowType: 'pkce', detectSessionInUrl: false},
    });
    store = createCloudStore(sdk, onData);
    if (code) {
      $('login-message').textContent = '正在验证邮件链接…';
      try {
        const {error} = await sdk.auth.exchangeCodeForSession(code);
        if (error) throw error;
      } catch {
        callbackMessage = '邮件链接未能完成登录，请在发起请求的浏览器重新发送。';
      }
    }
    sdk.auth.onAuthStateChange((event, session) => receiveSession(session, event === 'INITIAL_SESSION' ? callbackMessage : ''));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(true); });
    setInterval(() => { if (!document.hidden) refresh(); }, 20000);
  } catch (error) {
    $('login-message').textContent = message(error);
  }
}

start();
