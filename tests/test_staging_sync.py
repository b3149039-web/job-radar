from contextlib import redirect_stdout
from datetime import date, datetime
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit, parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from collect_jobs import plan
import staging_sync as stage


class StagingSyncTests(unittest.TestCase):
    def test_controlled_acceptance_exercises_production_batches_and_both_rollbacks(self):
        from contextlib import contextmanager
        from copy import deepcopy
        from supabase.generate_seed import FIELDS

        class DatabaseError(Exception):
            def __init__(self, state):
                self.sqlstate = state

        class Connection:
            def __init__(self):
                original, *_ = stage.fixtures(date(2026, 11, 13), '已有岗位')
                self.jobs = {original['id']: original}
                self.writes = []

            @contextmanager
            def transaction(self, force_rollback=False):
                before = deepcopy(self.jobs)
                try:
                    yield
                except Exception:
                    self.jobs = before
                    raise
                if force_rollback:
                    self.jobs = before

            def execute(self, sql, params=None):
                if sql == 'select 1 / 0':
                    raise DatabaseError('22012')
                if sql.startswith('insert into public.jobs'):
                    job = dict(zip(FIELDS, params))
                    self.writes.append(('insert', job['check_status']))
                    self.jobs[job['id']] = deepcopy(job)
                    return MagicMock(rowcount=1)
                if sql.startswith('update public.jobs set check_status='):
                    status, qualifications, job_id, previous_status, previous_qualifications = params
                    job = self.jobs[job_id]
                    if (job['check_status'], job['qualifications']) != (previous_status, previous_qualifications):
                        return MagicMock(rowcount=0)
                    self.writes.append(('update', status))
                    job.update(check_status=status, qualifications=deepcopy(qualifications))
                    return MagicMock(rowcount=1)
                if sql.startswith(('update ', 'delete ')):
                    raise DatabaseError('42501')
                if ', session_user' in sql:
                    identity = dict(current_user=stage.ROLE, session_user=stage.ROLE)
                    if 'as jobs_restricted' in sql:
                        identity.update(restricted=True, jobs_rls=True, personal_denied=True, jobs_restricted=True)
                    return MagicMock(fetchone=lambda: identity)
                if sql.endswith(('order by id', 'order by first_seen, id')):
                    return MagicMock(fetchall=lambda: deepcopy(sorted(self.jobs.values(), key=lambda row: row['id'])))
                return MagicMock(fetchone=lambda: self.jobs.get(params[0]))

        conn = Connection()
        now = datetime(2026, 11, 14, 9, 17, tzinfo=stage.sources.CST)
        report = []
        original = deepcopy(conn.jobs)
        with patch.object(stage, 'audit_permissions'):
            stage.audit_production_writer(conn, now, dict, report, DatabaseError)
        self.assertEqual(conn.writes, [('insert', '在招待确认'), ('update', '已结束'), ('insert', '在招待确认')])
        self.assertEqual(conn.jobs, original)
        self.assertTrue(any('异常 SQLSTATE 22012' in line for line in report))
        self.assertTrue(any('未验证 · 私人记录' in line for line in report))

    def test_refuses_production_even_if_credential_points_there(self):
        with TemporaryDirectory() as directory:
            ca = Path(directory) / 'ca.pem'
            ca.write_text('fixture CA')
            env = {'JOBRADAR_STAGING_PROJECT_REF': 's' * 20, 'JOBRADAR_PRODUCTION_PROJECT_REF': 'p' * 20,
                   'JOBRADAR_STAGING_PROTECTED_IDS': 'a' * 16 + ',' + 'b' * 16,
                   'JOBRADAR_JOBS_CA_FILE': str(ca)}
            def options(ref):
                return lambda _: {'host': f'db.{ref}.supabase.co', 'user': 'jobradar_collector',
                                  'dbname': 'postgres', 'password': 'fixture'}
            self.assertEqual(stage.settings(env, options('s' * 20))[0]['sslmode'], 'verify-full')
            with self.assertRaises(ValueError):
                stage.settings(env, options('p' * 20))
            with self.assertRaises(stage.AcceptanceError):
                stage.settings({**env, 'JOBRADAR_STAGING_PROJECT_REF': 'p' * 20}, options('p' * 20))
            with self.assertRaises(stage.AcceptanceError):
                stage.settings({**env, 'JOBRADAR_STAGING_PROTECTED_IDS': 'a' * 16}, options('s' * 20))

    def test_dsn_require_upgrades_and_failures_redact_values(self):
        class DriverError(Exception):
            sqlstate = None

        def parse(dsn):
            parts = urlsplit(dsn)
            return {'host': parts.hostname, 'user': parts.username, 'password': parts.password,
                    'dbname': parts.path[1:], 'port': str(parts.port), **dict(parse_qsl(parts.query))}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            ca, output = root / 'fixture.pem', root / 'report.md'
            ca.write_text('fixture CA')
            secret = 'not-a-real-credential'
            dsn = f'postgresql://jobradar_collector.{"s" * 20}:{secret}@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require'
            env = {'JOBRADAR_STAGING_PROJECT_REF': 's' * 20, 'JOBRADAR_PRODUCTION_PROJECT_REF': 'p' * 20,
                   'JOBRADAR_STAGING_PROTECTED_IDS': 'a' * 16 + ',' + 'b' * 16,
                   'JOBRADAR_JOBS_CA_FILE': str(ca), 'JOBRADAR_JOBS_DSN': dsn}
            driver = ModuleType('psycopg')
            driver.Error = DriverError
            driver.connect = MagicMock(side_effect=DriverError(dsn))
            modules = {'psycopg': driver}
            for module, attr, value in [('conninfo', 'conninfo_to_dict', parse), ('rows', 'dict_row', dict),
                                        ('types.json', 'Jsonb', dict)]:
                name = 'psycopg.' + module
                modules[name] = ModuleType(name)
                setattr(modules[name], attr, value)
            with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['stage', '--output', str(output)]):
                for suffix, phase, category in [
                    ('require', '建立 TLS 连接', '数据库连接失败'),
                    ('disable', '校验项目与连接配置', '连接配置无效'),
                ]:
                    driver.connect.reset_mock()
                    console = StringIO()
                    with patch.dict(stage.os.environ, {**env, 'JOBRADAR_JOBS_DSN': dsn.replace('require', suffix)},
                                    clear=True), redirect_stdout(console):
                        self.assertEqual(stage.main(), 1)
                    text = output.read_text(encoding='utf-8')
                    self.assertIn(phase, text)
                    self.assertIn(category, text)
                    for reported in (text, console.getvalue()):
                        self.assertNotIn(secret, reported)
                        self.assertNotIn('postgresql://', reported)
                    if suffix == 'require':
                        self.assertEqual(driver.connect.call_args.kwargs['sslmode'], 'verify-full')
                        self.assertEqual(driver.connect.call_args.kwargs['sslrootcert'], str(ca))
                    else:
                        driver.connect.assert_not_called()

    def test_cross_platform_match_preserves_new_batch_and_replays_without_changes(self):
        today = date(2026, 11, 13)
        a, b, separate = stage.fixtures(today, 'fixture')
        self.assertEqual(len({row['id'] for row in (a, b, separate)}), 3)
        self.assertNotEqual(a['source'], b['source'])
        counts, changes = plan([a, b, separate], [], today)
        stage.require_counts([], 'first', {**counts, 'protected_skipped': 0}, (2, 0, 1))
        baseline = [row for _, row in changes]
        updated = {**a, 'check_status': '已结束'}
        counts, changes = plan([updated], baseline, today)
        stage.require_counts([], 'update', {**counts, 'protected_skipped': 0}, (0, 1, 0))
        counts, _ = plan([updated, b, separate], [changes[0][1], separate], today)
        stage.require_counts([], 'replay', {**counts, 'protected_skipped': 0}, (0, 0, 3))

    def test_aborts_if_protected_snapshot_changes(self):
        with patch.object(stage, 'publish'), patch.object(stage, 'snapshot', return_value=[{'id': 'changed'}]):
            with self.assertRaisesRegex(stage.AcceptanceError, '回滚'):
                stage.batch(object(), [], None, None, ['a', 'b'], [{'id': 'original'}])

    def test_protected_id_is_skipped_even_if_new_evidence_requests_update(self):
        today = date(2026, 11, 13)
        row, _, _ = stage.fixtures(today, 'fixture')
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'current_user':stage.ROLE, 'session_user':stage.ROLE}
        conn.execute.return_value.fetchall.return_value = [row]
        counts = stage.publish(conn, [{**row, 'check_status':'已结束'}], today, dict,
                               protected_ids=[row['id']], expire_history=False)
        self.assertEqual((counts['updated'], counts['protected_skipped']), (0, 1))
        self.assertFalse(any(call.args[0].startswith(('update ', 'insert ')) for call in conn.execute.call_args_list))
