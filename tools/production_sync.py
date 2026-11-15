"""Validate a public collection report and apply it through a restricted role."""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from html import escape
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from collect_jobs import INACTIVE, normalize_url, plan, sources, stable_job_id
from publish_jobs import ROLE, ConnectionConfigError, connection_options, publish
from supabase.generate_seed import FIELDS, redact_contacts

PRODUCTION_REF = 'hnkwyuhkqzrlhjhiejof'
STAGING_REF = 'rqxklouevsxjmgqdhkdv'
PRODUCTION_ROLE = 'jobradar_prod_collector'
SOURCE_ORIGINS = {
    sources.NCSS_NAME: sources.NCSS, sources.UIBE_NAME: sources.UIBE, sources.JNJ_NAME: sources.JNJ,
    sources.LILLY_NAME: sources.LILLY, sources.ASML_NAME: sources.ASML, sources.HEBUT_NAME: sources.HEBUT,
}
STATUSES = INACTIVE | {'在招待确认', '官网显示可投递'}
MAX_INSERTS = 60
MAX_UPDATES = 12
MAX_TERMINAL_CHANGES = 3


class SafetyStop(Exception):
    pass


def report_rows(path, now, *, pilot=False):
    if path.stat().st_size > 4_000_000:
        raise SafetyStop('采集报告超过大小上限。')
    report = json.loads(path.read_text(encoding='utf-8'))
    moment = datetime.fromisoformat(report['collected_at'])
    if not timedelta(0) <= now - moment <= timedelta(hours=2):
        raise SafetyStop('采集报告已过期或时间不一致。')
    counts = {name: Counter() for name in (sources.PILOT_NAMES if pilot else SOURCE_ORIGINS)}
    warnings = {}
    blocked = set()
    for item in report['sources']:
        name, returned, accepted = item['source'], item['returned'], item['accepted']
        if name not in counts or type(returned) is not int or type(accepted) is not int or not 0 <= accepted <= returned:
            raise SafetyStop('来源计数异常。')
        counts[name].update(scopes=1, returned=returned, accepted=accepted)
    for item in report['warnings']:
        name, reason = item['source'], item['error']
        if name not in counts or type(item['blocked']) is not bool:
            raise SafetyStop('来源警告格式异常。')
        if item['blocked']:
            blocked.add(name)
        # Only emit fixed reasons or HTTP status codes, never source-provided text.
        if not re.fullmatch(r'HTTP [1-5][0-9]{2}', reason) and reason not in {
            '公开访问规则限制', '网络或响应超时', '来源格式或详情核验失败',
        }:
            reason = '来源详情核验失败'
        warnings.setdefault(name, Counter())[reason] += 1
    records = report['rows']
    if not isinstance(records, list) or len(records) > 140 or type(report['extracted']) is not int or report['extracted'] != len(records):
        raise SafetyStop('候选岗位数量与报告不一致或超过上限。')
    for row in records:
        validate_row(row, moment, pilot=pilot)
        counts[row['source']]['collected'] += 1
    retained = []
    unavailable = set()
    for name, count in counts.items():
        if not count['scopes'] or name in blocked or (name in warnings and (pilot or not count['returned'])):
            unavailable.add(name)
        elif count['collected'] != count['accepted']:
            raise SafetyStop('来源接受数量与实际记录不一致。')
    for row in records:
        if row['source'] not in unavailable:
            retained.append(row)
    return retained, counts, warnings, unavailable


def validate_row(row, moment, *, pilot=False):
    if not isinstance(row, dict) or set(row) != set(FIELDS) or not all(isinstance(row[field], str) and len(row[field]) <= 2048
                                          for field in FIELDS if field not in ('qualifications', 'cohort')):
        raise SafetyStop('岗位字段类型或长度异常。')
    parts = urlsplit(row['url'])
    origin = f'{parts.scheme}://{parts.netloc}'
    source_ok = sources.pilot_posting_url(row) if pilot else origin == SOURCE_ORIGINS.get(row['source'])
    if (not source_ok or not row['company'].strip() or not row['title'].strip()
            or row['city'] not in sources.CITY_EN or row['id'] != stable_job_id(row)
            or row['first_seen'] != moment.isoformat(timespec='seconds') or row['summary']
            or row['check_status'] not in STATUSES or row['recruitment_type'] not in {'campus', 'experienced', 'unknown'}
            or (row['cohort'] is not None and (type(row['cohort']) is not int or not 2000 <= row['cohort'] <= 2100))):
        raise SafetyStop('岗位来源、身份、日期或状态异常。')
    qualifications = row['qualifications']
    if not isinstance(qualifications, dict) or len(json.dumps(qualifications, ensure_ascii=False, allow_nan=False)) > 16_000:
        raise SafetyStop('任职证据格式异常。')
    social = qualifications.get('social')
    cohorts = qualifications.get('cohort_evidence')
    if (not isinstance(social, dict) or not isinstance(social.get('salary'), dict)
            or social.get('hr_activity') is not None or not isinstance(cohorts, list)
            or any(not isinstance(item, dict) or type(item.get('year')) is not int for item in cohorts)):
        raise SafetyStop('工作经验、薪资或届别证据格式异常。')
    details = qualifications.get('source_details')
    if not isinstance(details, dict) or not isinstance(details.get('status_evidence'), str) or not details['status_evidence'].strip():
        raise SafetyStop('岗位缺少来源状态证据。')
    if row['check_status'] == '官网显示可投递' and row['source'] == sources.NCSS_NAME:
        raise SafetyStop('来源未提供可确认的官网投递状态。')
    posted = details.get('source_posted_at')
    if not isinstance(posted, str) or posted != details.get('posted_at') or (
            posted and datetime.fromisoformat(posted).date() > moment.date()):
        raise SafetyStop('招聘发布日期无效。')


def review_plan(baseline, changes):
    baseline_by_id = {row['id']: row for row in baseline}
    new = sum(kind == 'new' for kind, _ in changes)
    updates = [row for kind, row in changes if kind == 'updated']
    terminal = sum(row['check_status'] in INACTIVE and
                   baseline_by_id[row['id']]['check_status'] not in INACTIVE for row in updates)
    limit = min(MAX_UPDATES, max(3, len(baseline) // 3))
    if new > MAX_INSERTS or len(updates) > limit or terminal > MAX_TERMINAL_CHANGES:
        raise SafetyStop(f'写入计划超过安全上限，拟新增 {new}、更新 {len(updates)}、转为失效 {terminal}。')


def apply_batch(conn, rows, today, jsonb, role, *, rollback=False, insert_only=False):
    with conn.transaction(force_rollback=rollback):
        conn.execute("set local statement_timeout = '30s'")
        conn.execute("set local lock_timeout = '10s'")
        conn.execute('select pg_advisory_xact_lock(9172027)')
        check_permissions(conn, role)
        original = conn.execute('select * from public.jobs order by id').fetchall()

        def guarded_plan(baseline, changes):
            # The read and plan must see the same baseline. Any concurrent changes halt this batch.
            if {row['id']: row for row in baseline} != {row['id']: row for row in original}:
                raise SafetyStop('岗位基线在处理期间变化。')
            review_plan(baseline, changes)

        by_source = {name: Counter() for name in {*SOURCE_ORIGINS, *sources.PILOT_NAMES}}
        counts = publish(conn, rows, today, jsonb, role=role, expire_history=False,
                         review_plan=guarded_plan, by_source=by_source, insert_only=insert_only)
        replay = publish(conn, rows, today, jsonb, role=role, expire_history=False, review_plan=review_plan,
                         insert_only=insert_only)
        actual = {row['id']: row for row in conn.execute('select * from public.jobs order by id').fetchall()}
        if replay['new'] or replay['updated'] or len(actual) != len(original) + counts['new']:
            raise SafetyStop('重放或岗位数量复核失败，整批回滚。')
        immutable = tuple(field for field in FIELDS if field not in ('qualifications', 'check_status'))
        if any(row['id'] not in actual or any(row[field] != actual[row['id']][field] for field in immutable)
               for row in original):
            raise SafetyStop('历史岗位身份、链接或首次发现时间变化，整批回滚。')
    if rollback and conn.execute('select * from public.jobs order by id').fetchall() != original:
        raise SafetyStop('测试事务回滚后岗位库与原始快照不一致。')
    return counts, by_source, replay


def read_only_plan(conn, rows, today, role):
    conn.read_only = True
    with conn.transaction(force_rollback=True):
        conn.execute('set transaction isolation level repeatable read read only')
        conn.execute("set local statement_timeout = '15s'")
        mode = conn.execute("""
            select current_setting('transaction_read_only') as read_only,
                   row_security_active('public.jobs'::regclass) as jobs_rls_active
        """).fetchone()
        if mode != {'read_only': 'on', 'jobs_rls_active': True}:
            raise SafetyStop('数据库没有确认只读事务及当前会话的岗位 RLS。')
        check_permissions(conn, role)
        baseline = conn.execute('select * from public.jobs order by first_seen, id').fetchall()
    by_source = {name: Counter() for name in SOURCE_ORIGINS}
    counts, changes = plan(rows, baseline, today, count_similar=True, by_source=by_source)
    try:
        review_plan(baseline, changes)
    except SafetyStop as error:
        guard = str(error)
    else:
        guard = '本次预测未触发批量写入上限。'
    return len(baseline), counts, by_source, changes, guard


def candidate_review(row, today):
    facts = row['qualifications']
    details = facts['source_details']
    social = facts['social']
    posted = details.get('source_posted_at')
    deadline = details.get('deadline')
    try:
        expired = date.fromisoformat(deadline) < today
    except (TypeError, ValueError):
        expired = False
    if row['check_status'] in INACTIVE or expired:
        priority, status = 4, '失效或无法访问'
    elif row['check_status'] == '官网显示可投递':
        priority, status = 0, '有官网投递入口证据'
    elif posted and (today - datetime.fromisoformat(posted).date()).days > 60:
        priority, status = 3, '可能过期（发布超过 60 天，投递状态未确认）'
    else:
        priority, status = 1, '待核验'

    flags = []
    if social.get('role_kind') in ('collections', 'sales', 'administrative'):
        flags.append('明显偏离法务求职方向')
        priority = max(priority, 2)
    if row['recruitment_type'] == 'campus':
        bachelor = facts.get('bachelor')
        cohort = facts.get('cohort_2027')
        if isinstance(bachelor, dict) and bachelor.get('state') == '不符合':
            flags.append('校招本科不符')
        if isinstance(cohort, dict) and cohort.get('state') == '不符合':
            flags.append('校招 2027 届不符')
    if row['recruitment_type'] != 'campus' and social.get('solo_legal') is True:
        flags.append('公开证据显示一人法务')
    if flags:
        priority = max(priority, 2)
    return priority, status, '、'.join(flags) or '待按个人条件确认'


def candidate_lines(planned, today):
    rows = [(candidate_review(row, today), row) for kind, row in planned if kind == 'new']
    lines = ['## 拟新增岗位（按公开证据排序）', '',
             '筛查仅使用公开岗位条件，账户偏好及私人记录不在预检范围。', '']
    if not rows:
        return lines + ['无拟新增岗位。']
    lines += ['| 公司 | 职位 | 城市 | 招聘状态 | 来源 | 实际发布日期 | 人工核对提示 | 原始链接 |',
              '|---|---|---|---|---|---|---|---|']
    for (_, status, flag), row in sorted(rows, key=lambda item: (item[0][0], item[1]['id'])):
        details = row['qualifications']['source_details']
        cells = (row['company'], row['title'], row['city'], f'{status}（来源记录：{row["check_status"]}）',
                 row['source'], details.get('source_posted_at') or '未知', flag)
        # The report carries public posting identifiers only. Drop all other query parameters.
        parts = urlsplit(row['url'])
        query = urlencode([(key, value) for key, value in parse_qsl(parts.query)
                           if row['source'] == sources.UIBE_NAME and key in {'tid', 'xqzwId', 'zpxxId'}
                           and re.fullmatch(r'[0-9]{1,24}', value)])
        url = quote(urlunsplit((parts.scheme, parts.netloc, parts.path, query, '')), safe=':/?&=+%~@')
        display = [escape(' '.join(redact_contacts(value).split())).replace('|', '&#124;')
                   .replace('[', '&#91;').replace(']', '&#93;').replace('!', '&#33;') for value in cells]
        lines.append('| ' + ' | '.join([*display, f'[原始招聘页](<{url}>)']) + ' |')
    return lines


def check_permissions(conn, role):
    result = conn.execute("""
        select current_user, session_user,
          (select not (rolsuper or rolcreaterole or rolcreatedb or rolreplication or rolbypassrls
              or exists (select from pg_auth_members where member = r.oid))
             from pg_roles r where rolname = current_user) as restricted,
          (select relrowsecurity from pg_class where oid = 'public.jobs'::regclass) as jobs_rls,
          (select count(*) = 4 and bool_and(relrowsecurity
              and not has_any_column_privilege(oid, 'SELECT,INSERT,UPDATE,REFERENCES')
              and not has_table_privilege(oid, 'DELETE,TRUNCATE,TRIGGER'))
             from pg_class where relnamespace = 'public'::regnamespace
             and relname in ('profiles', 'job_activity', 'interviews', 'manual_jobs')) as personal_denied,
          not has_schema_privilege('public', 'CREATE')
          -- PL/pgSQL event handlers run through DDL events, not through SELECT or CALL.
          and not exists (select from pg_proc
             where pronamespace = 'public'::regnamespace and prosecdef
             and not (prorettype = 'pg_catalog.event_trigger'::regtype
                      and prolang = (select oid from pg_language where lanname = 'plpgsql'))
             and has_function_privilege(oid, 'EXECUTE'))
          and not has_table_privilege('public.jobs', 'UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
          and not exists (select from pg_attribute
             where attrelid = 'public.jobs'::regclass and attnum > 0 and not attisdropped
             and attname not in ('check_status', 'qualifications')
             and has_column_privilege(attrelid, attnum, 'UPDATE')) as jobs_restricted
    """).fetchone()
    if result != {'current_user': role, 'session_user': role, 'restricted': True, 'jobs_rls': True,
                  'personal_denied': True, 'jobs_restricted': True}:
        raise SafetyStop('采集账号权限或岗位 RLS 未达到写入要求。')


def settings(env, parse, staging):
    project, role = (STAGING_REF, ROLE) if staging else (PRODUCTION_REF, PRODUCTION_ROLE)
    key = 'JOBRADAR_STAGING_PROJECT_REF' if staging else 'JOBRADAR_PRODUCTION_PROJECT_REF'
    if env.get(key) != project:
        raise ConnectionConfigError('审批环境的项目引用不符合预期。')
    return connection_options(env.get('JOBRADAR_JOBS_DSN', ''), parse,
                              env.get('JOBRADAR_JOBS_CA_FILE', ''), project, role=role), role


def source_lines(collected, warnings, unavailable, changes, *, has_baseline):
    lines = ['| 来源 | 返回项 | 采集候选 | 新增 | 更新 | 暂缓更新 | 重复 | 待核验 | 失败或限制 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for name in collected:
        counts = collected[name]
        reasons = '、'.join(f'{reason} × {n}' for reason, n in warnings.get(name, {}).items())
        if name in unavailable:
            reasons = ('本来源暂停入库' + ('，' + reasons if reasons else '，无成功采集范围'))
        totals = [str(changes[name][key]) if changes is not None and
                  (has_baseline or key in ('duplicates', 'needs_review')) and name not in unavailable else '—'
                  for key in ('new', 'updated', 'deferred_updates', 'duplicates', 'needs_review')]
        lines.append(f'| {name} | {counts["returned"]} | {counts["collected"]} | '
                     + ' | '.join(totals) + f' | {reasons or "—"} |')
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply-production', action='store_true')
    mode.add_argument('--verify-staging', action='store_true', help='Exercise real writes, then roll them back')
    mode.add_argument('--inspect-production', action='store_true', help='Plan against production in a read-only transaction')
    parser.add_argument('--insert-only', action='store_true', help='Defer all updates of existing jobs')
    parser.add_argument('--pilot-sources', action='store_true', help='Validate the pilot allowlist on staging only')
    args = parser.parse_args()
    uses_database = args.apply_production or args.verify_staging or args.inspect_production
    if args.insert_only and not (args.apply_production or args.verify_staging):
        parser.error('--insert-only requires --apply-production or --verify-staging')
    if args.pilot_sources and not args.verify_staging:
        parser.error('--pilot-sources requires --verify-staging')
    lines = ['# JobRadar 生产只读预检' if args.inspect_production else '# JobRadar 岗位采集核验', '']
    phase, result = '校验报告', 1
    collected = changes = None
    candidate_section = []
    now = datetime.now(sources.CST)
    try:
        rows, collected, warnings, unavailable = report_rows(args.report, now, pilot=args.pilot_sources)
        if not rows or len(unavailable) > len(collected) // 2:
            raise SafetyStop('没有可信候选，或超过半数来源无法完成采集。')
        if uses_database:
            phase = '连接配置与项目校验'
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb

            options, role = settings(os.environ, conninfo_to_dict, args.verify_staging)
            phase = '建立 TLS 连接'
            # libpq consults ambient PG* variables (including PGHOSTADDR) for unset options.
            for key in tuple(os.environ):
                if key.upper().startswith('PG'):
                    del os.environ[key]
            with psycopg.connect(**options, row_factory=dict_row, autocommit=True) as conn:
                if args.inspect_production:
                    phase = '权限、只读事务及岗位对照'
                    baseline_count, counts, changes, planned, guard = read_only_plan(conn, rows, now.date(), role)
                else:
                    phase = '权限、增量写入与重放'
                    counts, changes, replay = apply_batch(conn, rows, now.date(), Jsonb, role, rollback=args.verify_staging,
                                                          insert_only=args.insert_only)
        if args.inspect_production:
            lines.append('通过 · 正式项目引用、TLS 证书与主机名、受限采集角色、RLS 和只读事务。')
            lines.append(f'生产基线 {baseline_count} 条。预计新增 {counts["new"]} 条，更新 {counts["updated"]} 条。')
            lines.append(f'批量保护预览 · {guard}')
            candidate_section = candidate_lines(planned, now.date())
        elif args.apply_production or args.verify_staging:
            lines.append('结果 · 测试库试写已回滚。' if args.verify_staging else '结果 · 正式库增量已提交。')
            lines.append(f'本批新增 {counts["new"]}，更新 {counts["updated"]}。')
            if args.insert_only:
                lines.append(f'只新增模式 · {counts["deferred_updates"]} 条已有岗位更新暂缓。')
            lines.append(f'同一报告重放 · 新增 {replay["new"]}，更新 {replay["updated"]}，重复 {replay["duplicates"]}。')
        else:
            phase = '本地去重'
            changes = {name: Counter() for name in SOURCE_ORIGINS}
            counts, _ = plan(rows, [], now.date(), count_similar=True, by_source=changes)
            lines.append(f'结果 · 未连接数据库。批内去重后有 {counts["new"]} 条候选；历史库新增量未核实。')
        lines.append(f'{"本批" if uses_database else "批内"}重复 {counts["duplicates"]} 条，'
                     f'待核验 {counts["needs_review"]} 条，失效 {counts["inactive"]} 条。')
        dated = sum(bool(row['qualifications']['source_details']['source_posted_at']) for row in rows)
        open_entries = sum(row['check_status'] == '官网显示可投递' for row in rows)
        lines.append(f'来源证据 · 可用采集记录中，发布日期已知 {dated} 条，官网显示可投递 {open_entries} 条。')
        result = 0
    except (ConnectionConfigError, SafetyStop) as exc:
        lines.append(f'停止 · {phase} · {exc}')
    except (ValueError, TypeError, KeyError, AttributeError, OSError, ImportError, RuntimeError) as exc:
        lines.append(f'停止 · {phase} · {type(exc).__name__}，请检查该阶段的输入和依赖。')
    except Exception as exc:
        state = getattr(exc, 'sqlstate', None)
        code = state if isinstance(state, str) and re.fullmatch('[A-Z0-9]{5}', state) else '无'
        lines.append(f'失败 · {phase} · SQLSTATE {code}，请核对连接、角色和事务结果。')
    if collected is not None:
        lines.extend(['', *source_lines(collected, warnings, unavailable, changes,
                                       has_baseline=uses_database)])
    log = '\n'.join(lines) + '\n'
    if candidate_section:
        lines.extend(['', *candidate_section])
    summary = '\n'.join(lines) + '\n'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(summary, encoding='utf-8')
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(path).open('a', encoding='utf-8') as output:
            output.write(log)
    print(log, end='')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
