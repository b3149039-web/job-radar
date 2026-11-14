import assert from 'node:assert/strict';
import {existsSync, readFileSync} from 'node:fs';
import test from 'node:test';
import {blankProfile, compensation, createCloudStore, duplicateGroupKey, experienceYears, freshness, legacyRows, listJobs, matchJob,
  passesSocialFilters, planLegacyImport, publishedWithin, recruitmentStatus, socialPriority, validateConfig} from '../cloud/data.mjs';

const jobId = '0123456789abcdef';
const users = [{id: 'account-a', email: 'a@example.test'}, {id: 'account-b', email: 'b@example.test'}];
const current = {job_id: jobId, stage: '已投递', owner_id: users[0].id, revision: 2};

function mockClient(handler) {
  const calls = [];
  return {calls, from(table) {
    const request = {table, filters: {}};
    const query = {
      select() {request.method ||= 'select'; return query;},
      eq(field, value) {request.filters[field] = value; return query;},
      match(fields) {Object.assign(request.filters, fields); return query;},
      maybeSingle() {request.single = true; return query;},
      order() {return query;},
      range(start, end) {request.range = [start, end]; return query;},
      insert(data) {Object.assign(request, {method:'insert', data}); return query;},
      update(data) {Object.assign(request, {method:'update', data}); return query;},
      upsert(data, options) {Object.assign(request, {method:'upsert', data, options}); return query;},
      then(resolve, reject) {
        calls.push(request);
        return Promise.resolve().then(() => handler(request)).then(resolve, reject);
      },
    };
    return query;
  }};
}

function sampleResponse(request) {
  if (request.table === 'jobs') return {data: [{id: jobId}]};
  if (request.table === 'profiles') return {data: null};
  if (request.table === 'job_activity') {
    return {data: request.filters.owner_id === users[0].id ? [current] : []};
  }
  return {data: []};
}

test('only a Supabase HTTPS URL and publishable key pass the client configuration check', () => {
  const url = 'https://sample-ref.supabase.co';
  assert.equal(validateConfig({url, publishableKey: 'sb_publishable_fake_for_test'}).url, url);
  for (const publishableKey of ['sb_secret_example', 'eyJhbGciOiJ', 'sb_publishable_REPLACE_ME']) {
    assert.throws(() => validateConfig({url, publishableKey}));
  }
  assert.throws(() => validateConfig({url: url + '.example.test', publishableKey: 'sb_publishable_test'}));
});

test('legacy preview validates files, preserves unknown IDs and detects cloud conflicts', () => {
  const records = {[jobId]: '面试中', 'aaaaaaaaaaaaaaaa': '已投递', 'bbbbbbbbbbbbbbbb': '待投递'};
  const rows = legacyRows(JSON.stringify({format: 'jobradar-legacy-stages', version: 1, records}));
  const plan = planLegacyImport(rows, [{id: jobId}, {id:'aaaaaaaaaaaaaaaa'}], [current]);
  assert.deepEqual(plan.ready.map(row => row.job_id), ['aaaaaaaaaaaaaaaa']);
  assert.deepEqual(plan.unknown.map(row => row.job_id), ['bbbbbbbbbbbbbbbb']);
  assert.equal(plan.conflicts[0].cloud_stage, '已投递');
  assert.throws(() => legacyRows(JSON.stringify({format: 'jobradar-legacy-stages', version: 1, records: {bad:'已投递'}})));
  assert.throws(() => legacyRows('x'.repeat(500001)));
});

const historicalSeed = new URL('../supabase/seed_jobs.sql', import.meta.url);
const historicalJobs = new URL('../jobs.json', import.meta.url);

test('historical catalog preserves campus hard exclusions', {
  skip: !existsSync(historicalSeed) || !existsSync(historicalJobs) ? 'Local historical catalog only' : false,
}, () => {
  // Local matching fixture for the historical catalog. It is never part of Pages.
  const campusProfile = {
    ...blankProfile, search_mode: 'campus', target_cohort: 2027,
    bachelor: true, party_member: false, legal_objective_passed: true, legal_certificate: false,
  };
  const sql = readFileSync(historicalSeed, 'utf8');
  const jobs = JSON.parse(sql.split('$jobradar_seed$')[1]);
  assert.equal(jobs.length, 27);
  const old = JSON.parse(readFileSync(historicalJobs, 'utf8'));
  const previous = new Map(old.jobs.map(job => [job.id, job]));
  for (const job of jobs) {
    if (previous.get(job.id).match_status === 'ineligible') {
      assert.equal(matchJob(job, campusProfile).category, 'ineligible');
    }
  }
  const campusJob = jobs.find(job => previous.get(job.id).match_status === 'eligible');
  assert.equal(matchJob(campusJob, campusProfile).category, 'review');
  assert.equal(matchJob(campusJob, blankProfile).category, 'ineligible');
  const experienced = {...campusJob, recruitment_type: 'experienced', title: '企业法务'};
  assert.equal(matchJob(experienced, blankProfile).category, 'review');
});

test('two profiles get different results from the same jobs without access to the historical catalog', () => {
  const campus = {...blankProfile, search_mode:'campus', degree_level:2, major_kind:'law', target_cohort:2034};
  const experienced = {...blankProfile, degree_level:3, major_kind:'law', experience_years:5};
  const job = {id:'shared-campus', title:'法务专员', city:'上海', cohort:2034, recruitment_type:'campus',
    check_status:'官网显示可投递', qualifications:{education:{minimum:2}, major:{kind:'law'}}};
  const second = {...job, id:'shared-experienced', cohort:null, recruitment_type:'experienced',
    qualifications:{...job.qualifications, education:{minimum:3}, experience:{minimum:3}}};
  assert.deepEqual(listJobs([job, second], campus).map(item => item.job.id), ['shared-campus']);
  assert.deepEqual(listJobs([job, second], experienced).map(item => item.job.id), ['shared-experienced']);
});

test('public defaults leave personal conditions unset', () => {
  assert.deepEqual(blankProfile.cities, []);
  for (const field of ['target_cohort', 'bachelor', 'party_member', 'legal_objective_passed', 'legal_certificate']) {
    assert.equal(blankProfile[field], null, field);
  }
});

test('employer English titles match personal Chinese keywords and expired roles leave recommendations', () => {
  const job = {title: 'Sr Legal Counsel', city: '上海', recruitment_type: 'experienced'};
  const profile = {...blankProfile, keywords: ['法务']};
  assert.equal(matchJob(job, profile).category, 'review');
  assert.equal(matchJob({...job, check_status: '已过期'}, profile).category, 'ineligible');
});

test('the same role uses separate hard requirements and leaves unknown personal fields open', () => {
  const job = {title: 'Legal Counsel', city: '上海', recruitment_type: 'experienced', check_status: '官网显示可投递',
    qualifications: {education: {minimum: 3}, major: {kind: 'law'}, experience: {minimum: 3, kind: 'law_firm'},
      with_certificate: {reason: '要求中国法律执业资格'}}};
  assert.equal(matchJob(job, {...blankProfile, bachelor: true}).category, 'ineligible');
  assert.equal(matchJob(job, blankProfile).category, 'review');
  const qualified = {...blankProfile, degree_level: 3, major_kind: 'law', experience_years: 4,
    experience_kind: 'law_firm', legal_practice: true};
  assert.equal(matchJob(job, qualified).category, 'eligible');
  assert.equal(matchJob(job, {...qualified, experience_kind: 'in_house'}).category, 'ineligible');
});

test('freshness uses source date, never first discovery or recruiter activity', () => {
  for (const [posted, label] of [['2026-10-13', '发布超过30天'], ['2026-09-13', '发布超过60天'],
    ['2026-08-13', '发布超过90天'], ['', '发布日期未知'], ['2026-02-30', '发布日期未知']]) {
    assert.equal(freshness({first_seen: '2026-11-13', qualifications: {source_details: {posted_at: posted}}},
      '2026-11-13').label, label);
  }
});

test('12 reviewed NCSS postings form five display groups and retain the distinct masters vacancy', () => {
  const jobs = JSON.parse(readFileSync(new URL('./job_quality_fixture.json', import.meta.url), 'utf8'));
  const groups = Map.groupBy(jobs, duplicateGroupKey);
  assert.deepEqual([...groups.values()].map(group => group.length).sort(), [1, 2, 2, 2, 2, 3]);
  const construction = Map.groupBy(jobs.filter(job => job.company.includes('中建一局')), duplicateGroupKey);
  assert.deepEqual([...construction.values()].map(group => group.length).sort(), [2, 3]);
  assert.ok([...construction.values()].every(group => new Set(group.map(job =>
    job.qualifications.source_details.source_updated_at)).size === 1));
  const masters = jobs.find(job => job.id === 'e0c191c0d234ff34');
  assert.deepEqual(groups.get(duplicateGroupKey(masters)), [masters]);
  for (const members of groups.values()) {
    assert.equal(new Set(members.map(job => job.id)).size, members.length);
    assert.equal(new Set(members.map(job => job.url)).size, members.length);
  }
  const row = jobs[0];
  for (const patch of [{grouping_hash: ''}, {department: '不同部门'}, {requisition: 'HR-2'}, {batch: '另一批'}]) {
    assert.notEqual(duplicateGroupKey(row), duplicateGroupKey({...row, qualifications: {...row.qualifications,
      source_details: {...row.qualifications.source_details, ...patch}}}));
  }
});

test('publication filters and availability use distinct source evidence', () => {
  const today = '2026-11-13';
  const job = {source:'国家大学生就业服务平台', first_seen:today,
    qualifications:{source_details:{posted_at:today, source_updated_at:today}}};
  assert.equal(publishedWithin(job, 7, today), false);
  for (const [date, expected] of [['2026-11-06',[true,true,true]], ['2026-10-30',[false,true,true]],
    ['2026-10-14',[false,false,true]], ['2026-10-13',[false,false,false]]]) {
    const explicit = {...job, qualifications:{source_details:{source_posted_at:date}}};
    assert.deepEqual([7,14,30].map(days => publishedWithin(explicit, days, today)), expected);
  }
  assert.equal(recruitmentStatus({...job, check_status:'在招待确认'}, today), '待核验');
  assert.equal(recruitmentStatus({...job, check_status:'官网显示可投递'}, today), '确认在招');
  assert.equal(recruitmentStatus({...job, check_status:'已过期'}, today), '已截止');
  assert.equal(recruitmentStatus({...job, check_status:'无法访问'}, today), '无法访问');
});

test('social filters are account-local and unknown HR activity never passes verified-only', () => {
  const profile = {...blankProfile, social_preferences: {recent_days: 14, verified_only: true}};
  const job = {check_status:'官网显示可投递', qualifications:{source_details:{posted_at:'2026-11-01'}}};
  assert.equal(passesSocialFilters(job, profile, '2026-11-13'), true);
  assert.equal(passesSocialFilters(job, {...profile, social_preferences:{recent_days:7}}, '2026-11-13'), false);
  assert.equal(passesSocialFilters(job, {...profile, social_preferences:{hr_filter:'verified'}}), false);
  assert.equal(passesSocialFilters(job, {...profile, search_mode:'campus', social_preferences:{hr_filter:'verified'}}), true);
  assert.equal(experienceYears({...profile, social_preferences:{employment_start:'2024-06'}}, '2026-07-15'), 2);
  assert.equal(experienceYears({...profile, social_preferences:{employment_start:'2024-06-01'}}, '2026-07-15'), 25 / 12);
});

test('only provably low annual compensation is excluded; month count is a preference', () => {
  const profile = {...blankProfile, degree_level:3, social_preferences:{minimum_annual:190000, pay_months:15}};
  const job = {title:'法务', city:'上海', recruitment_type:'experienced', check_status:'官网显示可投递',
    qualifications:{education:{minimum:2}, experience:{minimum:0}, social:{}}};
  const salary = value => ({...job, qualifications:{...job.qualifications, social:{salary:{currency:'CNY', ...value}}}});
  assert.equal(matchJob(salary({monthly:[11000,12000], months:12}), profile).category, 'review');
  assert.equal(matchJob(salary({annual:[130000,180000], annual_complete:true}), profile).category, 'ineligible');
  assert.equal(matchJob(salary({monthly:[16000,20000], months:12}), profile).category, 'eligible');
  assert.deepEqual(compensation({currency:'CNY', monthly:[10000,10000], months:12,
    guaranteed_bonus:[10000,10000], variable_performance:true}), {floor:130000,ceiling:null});
  assert.equal(compensation({currency:'CNY', annual:[180000,250000], variable_performance:true, annual_complete:true}).floor, null);
  assert.deepEqual(compensation({currency:'CNY', annual:[180000,250000], annual_complete:false}), {floor:null,ceiling:null});
});

test('strong skills move experience gaps to stretch but never override hard exclusions', () => {
  const profile = {...blankProfile, degree_level:3, experience_years:1, social_preferences:{
    skills:['合同审查','争议解决'], accept_solo:false, include_government:true,
    excluded_types:['collections','sales'], employer_priority:['外资'],
  }};
  const job = {title:'法务', company:'示例集团', employer_type:'外商独资企业', city:'上海',
    recruitment_type:'experienced', check_status:'官网显示可投递',
    qualifications:{education:{minimum:3}, experience:{minimum:2, maximum:4},
      social:{role_kind:'legal', skills:['合同审查','争议解决']}}};
  assert.equal(matchJob(job, profile).category, 'stretch');
  assert.equal(socialPriority(job, profile)[0], 0);
  assert.equal(matchJob({...job,title:'唯一法务'}, profile).category, 'ineligible');
  assert.equal(matchJob({...job,qualifications:{...job.qualifications,social:{role_kind:'collections'}}}, profile).category, 'ineligible');
  assert.equal(matchJob({...job,recruitment_type:'campus'}, profile).category, 'ineligible');
  const govt = {...job,title:'公共事务专员',qualifications:{...job.qualifications,social:{role_kind:'government'}}};
  assert.ok(!matchJob(govt, profile).reasons.includes('职位未匹配关键词'));
});

const preferenceCities = ['上海', '苏州', '无锡', '常州'];
const rankingDate = {today: '2026-11-14'};

test('campus recommendations prioritize confirmed legal fit beyond Shanghai', () => {
  const profile = {...blankProfile, search_mode:'campus', target_cohort:2027, degree_level:2,
    major_kind:'law', legal_certificate:false, cities:preferenceCities, keywords:['法务','销售','行政']};
  const base = {id:'suzhou', company:'示例企业', title:'法务专员', city:'苏州', cohort:2027,
    recruitment_type:'campus', check_status:'官网显示可投递',
    qualifications:{education:{minimum:2}, major:{kind:'law'}, source_details:{source_posted_at:'2026-11-12'}}};
  const shanghai = {...base, city:'上海'};
  const weaker = {...shanghai, id:'stale', qualifications:{education:{minimum:2}}};
  const needsReview = {...shanghai, id:'uncertain', check_status:'在招待确认', cohort:null};
  const admin = {...shanghai, id:'admin', title:'行政助理'};
  const sales = {...shanghai, id:'sales', title:'法律咨询销售顾问'};
  const degree = {...shanghai, id:'degree', qualifications:{education:{minimum:3}}};
  const cohort = {...shanghai, id:'cohort', cohort:2026};
  const certificate = {...shanghai, id:'certificate', qualifications:{...base.qualifications,
    without_certificate:{state:'不符合', reason:'投递时持证'}}};
  const rows = [weaker, admin, sales, degree, cohort, certificate, needsReview, base];
  const result = listJobs(rows, profile, rankingDate);
  assert.deepEqual(result.map(x => x.job.id), ['suzhou','stale']);
  assert.ok(result[0].match.advantages.includes('明确接受 2027 届'));
  assert.ok(result[0].match.advantages.includes('符合明示的最低学历'));
  assert.deepEqual(listJobs(rows, profile, {...rankingDate, view:'review'}).map(x => x.job.id), ['uncertain']);
  for (const job of [admin, sales, degree, cohort, certificate]) assert.equal(matchJob(job, profile).category, 'ineligible', job.id);
});

test('experienced recommendations keep Shanghai salary, solo and collections counterexamples below qualified nearby jobs', () => {
  const profile = {...blankProfile, cities:preferenceCities, degree_level:3, experience_years:2,
    social_preferences:{minimum_annual:160000, accept_solo:false, skills:['合同审查','公司治理']}};
  const base = {id:'wuxi', title:'法务专员', city:'无锡', recruitment_type:'experienced', check_status:'官网显示可投递',
    qualifications:{education:{minimum:2}, experience:{minimum:2}, social:{role_kind:'legal', solo_legal:false,
      salary:{currency:'CNY', annual:[170000,210000], annual_complete:true}, skills:['合同审查','公司治理']}}};
  const shanghai = {...base, city:'上海'};
  const social = (id, patch) => ({...shanghai, id, qualifications:{...base.qualifications, social:{...base.qualifications.social, ...patch}}});
  const low = social('low', {salary:{currency:'CNY', annual:[130000,159999], annual_complete:true}});
  const unknown = social('unknown', {salary:{}, solo_legal:null});
  const solo = {...shanghai, id:'solo', title:'唯一法务'};
  const collections = {...shanghai, id:'collections', title:'法务催收'};
  const years = {...shanghai, id:'experience', qualifications:{...base.qualifications, experience:{minimum:4}}};
  const rows = [low, unknown, solo, collections, years, base];
  assert.deepEqual(listJobs(rows, profile, rankingDate).map(x => x.job.id), ['wuxi']);
  const review = listJobs(rows, profile, {...rankingDate, view:'review'});
  assert.deepEqual(review.map(x => x.job.id), ['unknown']);
  assert.deepEqual(review[0].match.unknowns, ['法务团队配置待核验','年总包是否达到底线待核验']);
  assert.deepEqual(listJobs(rows, profile, {...rankingDate, view:'stretch'}).map(x => x.job.id), ['experience']);
  for (const job of [low, solo, collections]) assert.equal(matchJob(job, profile).category, 'ineligible', job.id);
});

test('80/20 presentation only interleaves equally qualified jobs and never drops a city shortfall', () => {
  const profile = {...blankProfile, degree_level:2, cities:preferenceCities};
  const base = {title:'企业法务', city:'上海', recruitment_type:'experienced', check_status:'官网显示可投递',
    qualifications:{education:{minimum:2}, experience:{minimum:0}}};
  const cityJobs = Array.from({length:6}, (_, index) => ({...base, id:'sh' + index}));
  const nearby = {...base, id:'su', city:'苏州'};
  assert.deepEqual(listJobs([...cityJobs, nearby], profile, rankingDate).map(x => x.job.id),
    ['sh0','sh1','sh2','sh3','su','sh4','sh5']);
  assert.equal(listJobs(cityJobs, profile, rankingDate).length, 6);
  const stronger = {...nearby, id:'wx', city:'无锡', qualifications:{...base.qualifications,
    source_details:{source_posted_at:'2026-11-12'}}};
  assert.equal(listJobs([...cityJobs, nearby, stronger], profile, rankingDate)[0].job.id, 'wx');
});

test('campus employer preference ranks a comparable listed firm or SOE ahead of city', () => {
  const profile = {...blankProfile, search_mode:'campus', target_cohort:2027, degree_level:2,
    major_kind:'law', cities:preferenceCities, keywords:['合规']};
  const job = {id:'sh', title:'合规专员', city:'上海', recruitment_type:'campus', cohort:2027,
    check_status:'官网显示可投递', qualifications:{education:{minimum:2}, major:{kind:'law'}}};
  const listed = {...job, id:'su', city:'苏州', employer_type:'上市公司'};
  const state = {...job, id:'wx', city:'无锡', employer_type:'国有企业'};
  assert.deepEqual(listJobs([job, listed, state], profile, rankingDate).map(x => x.job.id), ['wx','su','sh']);
  assert.deepEqual(listJobs([job, listed, {...state, check_status:'已结束'}], profile, rankingDate)
    .map(x => x.job.id), ['su','sh']);
});

test('signed-out storage makes no requests and verified accounts load only their own cloud configuration', async () => {
  const profiles = [
    {...blankProfile, owner_id: users[0].id, cities: ['测试城市甲'], revision: 2},
    {...blankProfile, owner_id: users[1].id, cities: ['测试城市乙'], revision: 7},
  ];
  const client = mockClient(request => request.table === 'profiles'
    ? {data: profiles.find(profile => profile.owner_id === request.filters.owner_id)}
    : sampleResponse(request));
  const store = createCloudStore(client, () => {});
  assert.equal(await store.refresh(), null);
  await assert.rejects(store.saveProfile(blankProfile, null), /账号数据加载完成/);
  assert.deepEqual(client.calls, []);
  for (const [index, user] of users.entries()) {
    store.setAccount(user);
    assert.equal(store.snapshot(), null);
    await store.refresh();
    assert.deepEqual(store.snapshot().profile, profiles[index]);
  }
  store.setAccount(null);
  assert.equal(store.snapshot(), null);
  assert.equal(await store.refresh(), null);
  assert.equal(client.calls.length, 8);
  assert.ok(client.calls.every(request => request.method === 'select'));
});

test('a stale request cannot restore A records after switching to B', async () => {
  let release;
  const delay = new Promise(resolve => {release = resolve;});
  const client = mockClient(async request => {
    if (request.filters.owner_id === users[0].id) await delay;
    return sampleResponse(request);
  });
  const snapshots = [];
  const store = createCloudStore(client, data => snapshots.push(data));
  store.setAccount(users[0]);
  const aRead = store.refresh();
  store.setAccount(users[1]);
  await store.refresh();
  release();
  assert.equal(await aRead, null);
  assert.deepEqual(store.snapshot().activity, []);
  assert.equal(snapshots.filter(Boolean).length, 1);
  store.setAccount(null);
  assert.equal(store.snapshot(), null);
});

test('version mismatch refreshes the current account without unguarded retry', async () => {
  const client = mockClient(request => request.method === 'update' ? {data: null} : sampleResponse(request));
  const store = createCloudStore(client, () => {});
  store.setAccount(users[0]);
  await store.refresh();
  await assert.rejects(store.saveActivity(jobId, {stage:'面试中'}, 1), {code:'REVISION_CONFLICT'});
  const writes = client.calls.filter(call => call.method === 'update');
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].filters, {owner_id: users[0].id, job_id: jobId, revision: 1});
  assert.equal(store.snapshot().activity[0].stage, '已投递');
});

test('legacy writes only add records for the confirmed session and ignore concurrent duplicates', async () => {
  const client = mockClient(request => request.method === 'upsert' ? {data: []} : sampleResponse(request));
  const store = createCloudStore(client, () => {});
  store.setAccount(users[1]);
  await store.refresh();
  const plan = {ready: [{job_id: jobId, stage:'待投递'}]};
  await assert.rejects(store.importLegacy(plan, users[0].id), /账号已切换/);
  assert.equal(await store.importLegacy(plan, users[1].id), 0);
  const writes = client.calls.filter(call => call.method === 'upsert');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].options.ignoreDuplicates, true);
  assert.deepEqual(writes[0].data, [{owner_id: users[1].id, job_id: jobId, stage:'待投递'}]);
});
