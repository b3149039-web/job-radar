"""Apply a checked public recruitment report with the restricted PostgreSQL role."""
import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re

from collect_jobs import INACTIVE, plan, rechecked_rows, sources
from supabase.generate_seed import FIELDS

ROLE = 'jobradar_collector'


class ConnectionConfigError(ValueError):
    """A configuration error whose message contains no connection values."""


def connection_options(dsn, parse, ca_file, expected_project=None):
    options = parse(dsn)
    host, user = options.get('host', ''), options.get('user', '')
    direct = re.fullmatch(r'db\.[a-z0-9]+\.supabase\.co', host) and user == ROLE
    pooler = re.fullmatch(r'aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com', host) and re.fullmatch(
        ROLE + r'\.[a-z0-9]+', user)
    allowed = {'host', 'user', 'dbname', 'password', 'port', 'sslmode'}
    if set(options) - allowed:
        raise ConnectionConfigError('DSN 含不允许的连接参数。CA 证书请通过独立配置提供。')
    if options.get('sslmode', 'verify-full') not in {'require', 'verify-full'}:
        raise ConnectionConfigError('DSN 的 sslmode 只接受 require 或 verify-full。')
    if (not (direct or pooler) or options.get('port', '5432') != '5432'
            or options.get('dbname') != 'postgres' or not options.get('password')):
        raise ConnectionConfigError('请核对专用账号、数据库名、主机、端口和密码是否已配置。')
    project = host.split('.')[1] if direct else user.split('.')[1]
    if expected_project is not None and project != expected_project:
        raise ConnectionConfigError('数据库目标与审批环境的项目引用不一致。')
    if not Path(ca_file).is_file() or Path(ca_file).stat().st_size == 0:
        raise ConnectionConfigError('指定的 CA 证书文件不存在或为空。')
    return {**options, 'port': '5432', 'sslmode': 'verify-full', 'sslrootcert': ca_file,
            'gssencmode': 'disable', 'connect_timeout': 12}


def read_report(path, now):
    report = json.loads(path.read_text(encoding='utf-8'))
    collected = datetime.fromisoformat(report['collected_at'])
    rows = report['rows']
    if not timedelta(0) <= now - collected <= timedelta(hours=12) or not report['sources'] or not rows:
        raise ValueError('采集报告为空或过期。')
    if not all(set(row) == set(FIELDS) for row in rows):
        raise ValueError('报告含未知岗位字段。')
    blocked = {warning['source'] for warning in report['warnings'] if warning.get('blocked')}
    return rows, blocked


def historical_expirations(rows, today):
    for row in rows:
        if row['check_status'] in INACTIVE:
            continue
        deadline = row['qualifications'].get('source_details', {}).get('deadline')
        if deadline:
            try:
                if date.fromisoformat(deadline) < today:
                    yield row['id']
            except (ValueError, TypeError):
                continue


def publish(conn, rows, today, jsonb, *, protected_ids=(), expire_history=True):
    conn.execute("set local statement_timeout = '30s'")
    conn.execute("set local lock_timeout = '10s'")
    identity = conn.execute('select current_user, session_user').fetchone()
    if identity != {'current_user': ROLE, 'session_user': ROLE}:
        raise PermissionError('Unexpected database role')
    conn.execute('select pg_advisory_xact_lock(9172027)')
    baseline = conn.execute('select * from public.jobs order by first_seen, id').fetchall()
    counts, changes = plan(rows, baseline, today, count_similar=True)
    protected_ids = set(protected_ids)
    counts['protected_skipped'] = 0
    columns = ', '.join(FIELDS)
    placeholders = ', '.join(['%s'] * len(FIELDS))
    for kind, row in changes:
        if kind == 'new':
            values = [jsonb(row[key]) if key == 'qualifications' else row[key] for key in FIELDS]
            inserted = conn.execute(
                f'insert into public.jobs ({columns}) values ({placeholders}) on conflict (id) do nothing', values)
            if not inserted.rowcount:
                counts['new'] -= 1
                counts['duplicates'] += 1
        else:
            if row['id'] in protected_ids:
                counts['updated'] -= 1
                counts['protected_skipped'] += 1
                continue
            updated = conn.execute('update public.jobs set check_status=%s, qualifications=%s where id=%s',
                                   (row['check_status'], jsonb(row['qualifications']), row['id']))
            if not updated.rowcount:
                counts['updated'] -= 1
    status_changes = {row['id'] for kind, row in changes if kind == 'updated'}
    for job in historical_expirations(baseline, today) if expire_history else ():
        if job not in status_changes and job not in protected_ids:
            updated = conn.execute("update public.jobs set check_status='已过期' where id=%s", (job,))
            counts['updated'] += updated.rowcount
            counts['inactive'] += updated.rowcount
    counts['first_discovered_against_baseline'] = counts['new']
    return counts


def read_public_baseline(conn):
    with conn.transaction():
        conn.execute('set transaction read only')
        conn.execute("set local statement_timeout = '15s'")
        if conn.execute('select current_user').fetchone()['current_user'] != ROLE:
            raise PermissionError('Unexpected database role')
        return conn.execute('select * from public.jobs order by first_seen, id').fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', required=True, type=Path, metavar='REPORT')
    args = parser.parse_args()
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        now = datetime.now(sources.CST)
        rows, blocked = read_report(args.apply, now)
        options = connection_options(os.environ.get('JOBRADAR_JOBS_DSN', ''), conninfo_to_dict,
                                     os.environ.get('JOBRADAR_JOBS_CA_FILE', ''),
                                     os.environ.get('JOBRADAR_JOBS_PROJECT_REF', ''))
        with psycopg.connect(**options, row_factory=dict_row) as conn:
            baseline = read_public_baseline(conn)
        cities = json.loads((Path(__file__).resolve().parents[1] / 'daily_sources.json').read_text(
            encoding='utf-8'))['preferred_cities']
        warnings = []
        rows.extend(rechecked_rows(sources.PublicReader(), baseline, rows, cities, now.date(),
                                  lambda source, scope, error: warnings.append(source), blocked))
        with psycopg.connect(**options, row_factory=dict_row) as conn:
            counts = publish(conn, rows, now.date(), Jsonb)
        counts['history_check_warnings'] = len(warnings)
    except (ImportError, ValueError, TypeError, KeyError, OSError, PermissionError):
        raise SystemExit('岗位写入未完成。请检查报告、依赖及专用连接配置。') from None
    except psycopg.Error as exc:
        # Database exception text can include connection details.
        state = exc.sqlstate if re.fullmatch(r'[A-Z0-9]{5}', exc.sqlstate or '') else 'unknown'
        raise SystemExit(f'岗位写入事务失败，SQLSTATE {state}。') from None
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == '__main__':
    main()
