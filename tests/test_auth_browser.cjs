// Uses an installed Playwright and browser. All Supabase calls are mocked.
'use strict';

const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {chromium} = require('playwright');

function fakeSdk(emailAddress, fixturePassword, basePath) {
  const user = {id: 'test-account', email: emailAddress};
  const jobId = '0123456789abcdef';
  const key = 'jobradar-test-session';
  let session = JSON.parse(localStorage.getItem(key) || 'null');
  let checked = false;
  let listener;
  const calls = window.authCalls = [];
  const trace = window.dataCalls = [];
  function login() {
    session = {user};
    localStorage.setItem(key, JSON.stringify(session));
    listener?.('SIGNED_IN', session);
    return {data: {session}};
  }
  window.supabase = {createClient(_url, publicKey, options) {
    if (!publicKey.startsWith('sb_publishable_') || options.auth.flowType !== 'pkce' ||
        !options.auth.persistSession || options.auth.detectSessionInUrl) throw Error('Invalid client config');
    return {
      auth: {
        onAuthStateChange(callback) {
          listener = callback;
          queueMicrotask(() => listener('INITIAL_SESSION', session));
        },
        async signInWithPassword({email, password}) {
          calls.push('password');
          await new Promise(resolve => setTimeout(resolve, 250));
          if (email !== user.email || password !== fixturePassword) {
            return {error: {code: 'invalid_credentials'}};
          }
          return login();
        },
        async signInWithOtp({options}) {
          if (options.shouldCreateUser || options.emailRedirectTo !== location.origin + basePath) throw Error('Unsafe OTP options');
          calls.push('email');
          return {};
        },
        async verifyOtp({email, token, type}) {
          if (email !== user.email || token !== '123456' || type !== 'email') throw Error('Invalid test OTP');
          calls.push('otp');
          return login();
        },
        async exchangeCodeForSession(code) {
          if (code !== 'fixture-code') throw Error('Unexpected callback');
          calls.push('exchange');
          return login();
        },
        async getUser() {
          calls.push('identity');
          await new Promise(resolve => setTimeout(resolve, 200));
          if (!session) return {error: {code: 'session_not_found'}};
          checked = true;
          return {data: {user}};
        },
        async signOut({scope}) {
          if (scope !== 'local') throw Error('Unexpected sign-out scope');
          calls.push('logout');
          checked = false;
          session = null;
          localStorage.removeItem(key);
          listener('SIGNED_OUT', null);
          return {};
        },
      },
      from(table) {
        if (!checked || !session) throw Error('Data read before identity confirmation');
        let owner;
        let single = false;
        const query = {
          select() {return query;},
          eq(name, value) {if (name === 'owner_id') owner = value; return query;},
          maybeSingle() {single = true; return query;},
          order() {return query;},
          range() {return query;},
          then(resolve, reject) {
            return Promise.resolve().then(() => {
              if (table !== 'jobs' && owner !== user.id) throw Error('Incorrect identity');
              trace.push(table);
              const rows = {
                jobs: [{id:jobId,title:'企业法务',company:'测试企业',city:'上海',url:'https://example.test',
                  recruitment_type:'experienced'}, ...[1, 2].map(index => ({
                    id:'duplicate-' + index, title:'另一法务', company:'测试企业', city:'上海',
                    url:'https://example.test/' + index, recruitment_type:'experienced',
                    qualifications:{source_details:{grouping_hash:'a'.repeat(64),headcount:1,degree:'本科',
                      salary_display:'面议',source_updated_at:'2026-01-01',
                      source_posted_at:index === 1 ? new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10) : ''}},
                  }))],
                profiles: [{cities:['上海'],keywords:['法务'],search_mode:'experienced',target_cohort:2034,
                  bachelor:null,party_member:true,legal_objective_passed:false,legal_certificate:true,revision:1,
                  social_preferences:{minimum_annual:210000,employment_start:'2024-06',recent_days:null}}],
                job_activity: [{job_id:jobId,favorite:true,stage:'已投递',notes:'测试备注',revision:1}],
                interviews: [{id:'test-interview',job_id:jobId,round:'初面',status:'已安排',revision:1}],
              }[table];
              return {data: single ? rows[0] : rows};
            }).then(resolve, reject);
          },
        };
        return query;
      },
    };
  }};
}

for (const basePath of ['/', '/job-radar/']) test('browser authentication at ' + basePath, async () => {
  const browser = await chromium.launch({
    headless: true, executablePath: process.env.JOBRADAR_BROWSER || undefined,
  });
  const origin = 'http://jobradar-auth.test';
  const email = randomUUID() + '@example.test';
  const secret = randomUUID();
  try {
    const context = await browser.newContext({viewport: {width: 390, height: 844}});
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin || !url.pathname.startsWith(basePath)) return route.abort();
      const routePath = '/' + url.pathname.slice(basePath.length);
      if (routePath === '/config.json') return route.fulfill({
        json: {url:'https://test-project.supabase.co',publishableKey:'sb_publishable_browser_fixture'},
      });
      if (routePath === '/sdk.js') return route.fulfill({
        body:'(' + fakeSdk.toString() + ')(...' + JSON.stringify([email, secret, basePath]) + ');',
        contentType:'text/javascript',
      });
      const asset = {'/':'index.html','/style.css':'style.css','/app.mjs':'app.mjs','/data.mjs':'data.mjs'}[routePath];
      if (!asset) return route.abort();
      const type = asset.endsWith('.html') ? 'text/html' : asset.endsWith('.css') ? 'text/css' : 'text/javascript';
      return route.fulfill({contentType:type+'; charset=utf-8', body:fs.readFileSync(path.join(__dirname,'..','cloud',asset))});
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin + basePath);
    const password = page.locator('#password');
    const submit = page.getByRole('button', {name:'登录', exact:true});
    async function showReview() {
      const view = page.getByLabel('查看范围');
      await view.waitFor();
      await view.selectOption('review');
      await page.getByLabel('企业法务投递状态').waitFor();
    }
    assert.equal(await page.locator('#dashboard').isVisible(), false);
    const privateFields = ['cities', 'cohort', 'degree-level', 'party-member', 'objective', 'certificate',
      'minimum-annual', 'employment-start'];
    const readPrivateFields = () => Promise.all(privateFields.map(id => page.locator('#' + id).inputValue()));
    assert.deepEqual(await readPrivateFields(), privateFields.map(() => ''));
    assert.deepEqual(await page.evaluate(() => window.dataCalls), []);
    await page.locator('#email').fill(email);
    await password.fill(randomUUID());
    await page.getByRole('button', {name:'显示密码'}).click();
    assert.equal(await password.getAttribute('type'), 'text');
    await page.getByRole('button', {name:'隐藏密码'}).click();
    assert.equal(await password.getAttribute('type'), 'password');
    await submit.click();
    await page.getByText('正在登录…', {exact:true}).waitFor();
    assert.equal(await submit.isDisabled(), true);
    await page.getByText('邮箱或密码错误。', {exact:true}).waitFor();
    assert.equal(await password.inputValue(), '');
    assert.deepEqual(await page.evaluate(() => window.dataCalls), []);
    assert.equal(await page.locator('#dashboard').isVisible(), false);

    await password.fill(secret);
    await submit.click();
    await page.getByText('正在确认登录身份…', {exact:true}).waitFor();
    assert.equal(await page.locator('#dashboard').isVisible(), false);
    await page.getByText('当前范围内没有岗位。', {exact:true}).waitFor();
    await showReview();
    assert.equal(await page.getByLabel('企业法务投递状态').inputValue(), '已投递');
    assert.ok(await page.getByText(/^待核验：/).count() > 0);
    assert.equal(await page.getByRole('button', {name:'已收藏'}).count(), 1);
    const group = page.locator('details.duplicate-group');
    assert.equal(await group.count(), 1);
    assert.equal(await group.locator('article').first().isVisible(), false);
    await group.locator('summary').first().click();
    assert.deepEqual(await group.getByRole('link', {name:'原招聘页'}).evaluateAll(links => links.map(link => link.href)),
      ['https://example.test/1','https://example.test/2']);
    assert.equal(await group.getByLabel('另一法务投递状态').count(), 2);
    await page.getByLabel('来源发布日期').selectOption('7');
    assert.equal(await group.getByLabel('另一法务投递状态').count(), 1);
    assert.deepEqual(await group.getByRole('link').evaluateAll(links => links.map(link => link.href).sort()),
      ['https://example.test/1','https://example.test/2']);
    await page.getByLabel('来源发布日期').selectOption('');
    const trackedJob = page.locator('article').filter({has:page.getByRole('heading', {name:'企业法务', exact:true})});
    await trackedJob.getByText('个人备注与面试', {exact:true}).click();
    assert.equal(await trackedJob.getByLabel('个人备注', {exact:true}).inputValue(), '测试备注');
    assert.equal(await trackedJob.getByLabel('面试轮次', {exact:true}).first().inputValue(), '初面');
    const savedFields = ['上海', '2034', '', 'true', 'false', 'true', '210000', '2024-06'];
    assert.deepEqual(await readPrivateFields(), savedFields);
    await page.getByRole('button', {name:'切换为校招模式'}).click();
    assert.equal(await page.locator('#mode').inputValue(), 'campus');
    assert.deepEqual(await readPrivateFields(), savedFields);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);

    await page.reload();
    await showReview();
    assert.deepEqual(await page.evaluate(() => window.authCalls), ['identity']);
    assert.equal(await page.locator('#mode').inputValue(), 'experienced');
    assert.deepEqual(await readPrivateFields(), savedFields);
    await page.getByRole('button', {name:'退出当前设备'}).click();
    await submit.waitFor();
    assert.equal(await page.locator('#jobs article').count(), 0);
    assert.deepEqual(await readPrivateFields(), privateFields.map(() => ''));
    await page.reload();
    await submit.waitFor();
    assert.equal(await page.locator('#dashboard').isVisible(), false);
    assert.deepEqual(await page.evaluate(() => window.dataCalls), []);

    await page.getByText('使用邮件验证码或登录链接', {exact:true}).click();
    await page.locator('#otp-email').fill(email);
    await page.getByRole('button', {name:'发送登录邮件'}).click();
    await page.locator('#code').fill('123456');
    await page.getByRole('button', {name:'验证并登录'}).click();
    await showReview();
    assert.deepEqual(await page.evaluate(() => window.authCalls), ['email', 'otp', 'identity']);
    await page.getByRole('button', {name:'退出当前设备'}).click();
    await submit.waitFor();
    await page.goto(origin + basePath + '?code=fixture-code');
    await showReview();
    assert.equal(page.url(), origin + basePath);
    assert.deepEqual(await page.evaluate(() => window.authCalls), ['exchange', 'identity']);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
