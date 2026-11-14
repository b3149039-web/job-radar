const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {PGlite} = require('@electric-sql/pglite');

const root = path.join(__dirname, '..');
const migration = readFileSync(path.join(root, 'supabase/007_jobradar_production_collector.review.sql'), 'utf8');
const writer = readFileSync(path.join(root, 'tools/production_sync.py'), 'utf8');
const permissionQuery = /def check_permissions\(conn, role\):\s+result = conn\.execute\("""([\s\S]*?)"""/.exec(writer)[1];

async function setup(db) {
  await db.exec(`
    create role anon nologin;
    create role authenticated nologin;
    create table public.jobs (
      id text, company text, title text, city text, source text, url text, summary text, cohort integer,
      recruitment_type text, employer_type text, check_status text, qualifications jsonb, first_seen text
    );
    alter table public.jobs enable row level security;
    create table public.profiles (id integer);
    create table public.job_activity (id integer);
    create table public.interviews (id integer);
    create table public.manual_jobs (id integer);
    alter table public.profiles enable row level security;
    alter table public.job_activity enable row level security;
    alter table public.interviews enable row level security;
    alter table public.manual_jobs enable row level security;

    create function public.rls_auto_enable() returns event_trigger
    language plpgsql security definer set search_path = pg_catalog as $$
    begin
      perform * from pg_event_trigger_ddl_commands();
    end;
    $$;
    grant execute on function public.rls_auto_enable() to public;
    create event trigger fixture_auto_enable on ddl_command_end
      when tag in ('CREATE TABLE') execute function public.rls_auto_enable();
  `);
}

test('event-trigger EXECUTE passes the migration and runtime checks but rejects direct calls', async () => {
  const db = new PGlite();
  try {
    await setup(db);
    await db.exec(migration);
    await db.exec('set session authorization jobradar_prod_collector');
    const {rows: [privileges]} = await db.query(`
      select has_function_privilege('public.rls_auto_enable()', 'EXECUTE') as can_execute,
             has_schema_privilege('public', 'CREATE') as can_create
    `);
    assert.deepEqual(privileges, {can_execute: true, can_create: false});
    assert.deepEqual((await db.query(permissionQuery)).rows, [{
      current_user: 'jobradar_prod_collector', session_user: 'jobradar_prod_collector',
      restricted: true, jobs_rls: true, personal_denied: true, jobs_restricted: true,
    }]);
    await assert.rejects(db.query('select public.rls_auto_enable()'), {
      code: '0A000', message: /only be called as triggers/,
    });
  } finally {
    await db.close();
  }
});

test('callable SECURITY DEFINER functions still block the migration and runtime checks', async () => {
  const db = new PGlite();
  try {
    await setup(db);
    await db.exec(`
      create function public.fixture_definer() returns integer
      language sql security definer set search_path = pg_catalog as $$ select 1 $$;
      grant execute on function public.fixture_definer() to public;
    `);
    await assert.rejects(db.exec(migration), {message: /Unexpected inherited collector privileges/});
    await db.exec(`
      rollback;
      revoke execute on function public.fixture_definer() from public;
    `);
    await db.exec(migration);
    await db.exec(`
      grant execute on function public.fixture_definer() to jobradar_prod_collector;
      set session authorization jobradar_prod_collector;
    `);
    assert.equal((await db.query('select public.fixture_definer() as value')).rows[0].value, 1);
    const {rows: [checks]} = await db.query(permissionQuery);
    assert.equal(checks.jobs_restricted, false);
    assert.equal(checks.personal_denied, true);
  } finally {
    await db.close();
  }
});
