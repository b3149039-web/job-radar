"""Manual GitHub acceptance of the real collector, with staging-only target checks."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from collect_jobs import payload, sources
from publish_jobs import ROLE, ConnectionConfigError, connection_options, publish, read_report


class AcceptanceError(Exception):
    pass


def settings(env, parse):
    stage, production = (env.get(name, '') for name in ('JOBRADAR_STAGING_PROJECT_REF', 'JOBRADAR_PRODUCTION_PROJECT_REF'))
    if not re.fullmatch(r'[a-z0-9]{20}', stage) or not re.fullmatch(r'[a-z0-9]{20}', production) or stage == production:
        raise AcceptanceError('测试和生产项目引用必须分别配置且不同。')
    ids = env.get('JOBRADAR_STAGING_PROTECTED_IDS', '').split(',')
    ids = [value.strip() for value in ids]
    if len(ids) != 2 or len(set(ids)) != 2 or not all(re.fullmatch(r'[a-f0-9]{16}', value) for value in ids):
        raise AcceptanceError('请配置两条原模拟岗位的不同 ID。')
    options = connection_options(env.get('JOBRADAR_JOBS_DSN', ''), parse, env.get('JOBRADAR_JOBS_CA_FILE', ''), stage)
    return options, sorted(ids)


def snapshot(conn, ids):
    data = conn.execute('select * from public.jobs where id = any(%s) order by id', (ids,)).fetchall()
    if [row['id'] for row in data] != ids:
        raise AcceptanceError('未找到两条指定的测试基线岗位。')
    return data


def batch(conn, rows, today, jsonb, ids, original):
    counts = publish(conn, rows, today, jsonb, protected_ids=ids, expire_history=False)
    if snapshot(conn, ids) != original:
        raise AcceptanceError('原模拟岗位发生变化，本批事务回滚。')
    return counts


def fixtures(today, marker):
    body = '此段为跨平台验收合成正文。' * 8
    base = dict(company=marker, title='模拟法务岗位', city='上海', check_status='在招待确认',
                recruitment_type='experienced', posted_at=today.isoformat(), requirement_text=body)
    a = payload({**base, 'source': '模拟平台甲', 'url': 'https://a.example.test/' + marker}, today.isoformat())
    b = payload({**base, 'source': '模拟平台乙', 'url': 'https://b.example.test/' + marker}, today.isoformat())
    separate = payload({**base, 'source': '模拟平台乙', 'url': 'https://b.example.test/other-' + marker,
                        'campaign_title': '另一个批次', 'requirement_text': body + '不同批次正文'}, today.isoformat())
    return a, b, separate


def audit(conn, today, jsonb, ids, original, report, pg_error):
    for table in ('profiles', 'job_activity', 'interviews'):
        for op, sql in {
            '读取': f'select * from public.{table} where false',
            '新增': f'insert into public.{table} (owner_id) select null::uuid where false',
            '修改': f'update public.{table} set ' + ('social_preferences=null' if table == 'profiles' else "notes=''") + ' where false',
            '删除': f'delete from public.{table} where false',
        }.items():
            expect_denial(conn, sql, f'{table} {op}', report, pg_error)
    expect_denial(conn, 'delete from public.jobs where false', '禁止删除岗位', report, pg_error)
    expect_denial(conn, "update public.jobs set title='验收' where false", '禁止覆盖岗位名称', report, pg_error)

    marker = 'jobradar-actions-' + uuid4().hex
    a, b, separate = fixtures(today, marker)
    fixture_ids = [row['id'] for row in (a, b, separate)]
    if set(fixture_ids) & set(ids) or len(set(fixture_ids)) != 3:
        raise AcceptanceError('模拟岗位 ID 发生冲突。')
    if conn.execute('select 1 from public.jobs where id = any(%s)', (fixture_ids,)).fetchone():
        raise AcceptanceError('模拟岗位 ID 已存在。')
    with conn.transaction(force_rollback=True):
        first = batch(conn, [a, b, separate], today, jsonb, ids, original)
        require_counts(report, '跨平台去重并保留不同批次', first, (2, 0, 1))
        stored = conn.execute('select id from public.jobs where id = any(%s) order by id', (fixture_ids,)).fetchall()
        if [row['id'] for row in stored] != sorted([a['id'], separate['id']]):
            raise AcceptanceError('实际入库记录与跨平台去重结果不一致。')
        updated = {**a, 'check_status': '已结束'}
        require_counts(report, '更新原岗位状态', batch(conn, [updated], today, jsonb, ids, original), (0, 1, 0))
        actual = conn.execute('select * from public.jobs where id=%s', (a['id'],)).fetchone()
        if actual != updated:
            raise AcceptanceError('状态更新后复读不一致。')
        require_counts(report, '重复执行', batch(conn, [updated, b, separate], today, jsonb, ids, original), (0, 0, 3))
    try:
        with conn.transaction(force_rollback=True):
            require_counts(report, '异常前新增', batch(conn, [a], today, jsonb, ids, original), (1, 0, 0))
            conn.execute('select 1 / 0')
    except pg_error as exc:
        if exc.sqlstate != '22012':
            raise
    else:
        raise AcceptanceError('未触发预期的事务异常。')
    if (conn.execute('select 1 from public.jobs where id = any(%s)', (fixture_ids,)).fetchone()
            or snapshot(conn, ids) != original):
        raise AcceptanceError('模拟记录清理或原岗位复核失败。')
    report.append('通过 · 模拟岗位全部回滚，原两条岗位逐字段一致')


def expect_denial(conn, sql, label, report, pg_error):
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(sql)
    except pg_error as exc:
        if exc.sqlstate == '42501':
            report.append('通过 · 数据库拒绝 ' + label)
            return
        raise
    raise AcceptanceError('数据库未拒绝 ' + label)


def require_counts(report, label, counts, expected=None):
    actual = tuple(counts[key] for key in ('new', 'updated', 'duplicates'))
    if expected is not None and actual != expected:
        raise AcceptanceError(f'{label}计数不符，实际新增 {actual[0]}、更新 {actual[1]}、重复 {actual[2]}。')
    report.append(f'通过 · {label}，新增 {actual[0]}，更新 {actual[1]}，重复 {actual[2]}，'
                  f'保护跳过 {counts["protected_skipped"]}，待核验 {counts["needs_review"]}')


def failure_message(phase, category, hint):
    return f'失败 · 阶段「{phase}」 · 类别「{category}」 · {hint}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', type=Path, help='Optional real collection report, after simulated checks')
    args = parser.parse_args()
    report = ['# JobRadar 测试库同步验收', '', '范围 · jobradar-staging', '']
    result = 1
    try:
        import psycopg
    except ImportError:
        report.append(failure_message('加载依赖', '客户端不可用', '请检查 PostgreSQL 客户端依赖。'))
    else:
        phase = '加载依赖'
        try:
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
            phase = '校验项目与连接配置'
            try:
                options, ids = settings(os.environ, conninfo_to_dict)
            except psycopg.Error:
                raise ConnectionConfigError('无法解析 DSN，请核对连接串格式及特殊字符的 URL 编码。') from None
            now = datetime.now(sources.CST)
            phase = '建立 TLS 连接'
            with psycopg.connect(**options, autocommit=True, row_factory=dict_row) as conn:
                phase = '核对采集账号身份'
                if conn.execute('select current_user, session_user').fetchone() != {'current_user': ROLE, 'session_user': ROLE}:
                    raise AcceptanceError('数据库登录身份不符合预期。')
                report.append('通过 · 当前和会话角色都是专用采集账号')
                phase = '读取受保护岗位基线'
                original = snapshot(conn, ids)
                if args.apply:
                    phase = '校验真实采集报告'
                    rows, _ = read_report(args.apply, now)
                    collection = json.loads(args.apply.read_text(encoding='utf-8'))
                    report.append(f'采集报告 · 原始记录 {len(rows)}，来源警告 {len(collection["warnings"])}。'
                                  '详细来源警告见采集产物。')
                    phase = '增量写入与重放'
                    with conn.transaction():
                        first = batch(conn, rows, now.date(), Jsonb, ids, original)
                        replay = batch(conn, rows, now.date(), Jsonb, ids, original)
                        if replay['new'] or replay['updated']:
                            raise AcceptanceError('真实报告重放仍产生写入，整批回滚。')
                    require_counts(report, '真实采集增量已提交', first)
                    require_counts(report, '同一报告重放', replay)
                else:
                    phase = '模拟写入、权限与回滚验收'
                    audit(conn, now.date(), Jsonb, ids, original, report, psycopg.Error)
                    report.append('未验证 · 真实公开招聘增量写入')
            result = 0
        except ConnectionConfigError as error:
            report.append(failure_message(phase, '连接配置无效', str(error)))
        except AcceptanceError as error:
            report.append(failure_message(phase, '验收条件未满足', str(error)))
        except psycopg.Error as error:
            state = error.sqlstate if re.fullmatch(r'[A-Z0-9]{5}', error.sqlstate or '') else '无'
            if phase == '建立 TLS 连接':
                category, hint = '数据库连接失败', '请核对网络、测试采集账号凭据和 CA 证书。'
            else:
                category, hint = '数据库操作失败', '请核对测试库迁移、权限及事务结果。'
            report.append(failure_message(phase, category, f'SQLSTATE {state}。{hint}'))
        except ImportError:
            report.append(failure_message(phase, '客户端不可用', '请检查 PostgreSQL 客户端依赖。'))
        except (ValueError, TypeError, KeyError, OSError):
            hints = {
                '校验项目与连接配置': '请核对测试项目引用、受保护岗位 ID 和证书文件。',
                '校验真实采集报告': '请核对采集报告的格式、内容和生成时间。',
            }
            report.append(failure_message(phase, '输入或文件异常', hints.get(phase, '请核对本阶段输入。')))
    if result:
        report.append('未验证 · 提前停止后未执行的项目')
    summary = '\n'.join(report) + '\n'
    args.output.write_text(summary, encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as output:
            output.write(summary)
    print(summary, end='')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
