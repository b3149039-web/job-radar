from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import production_sync as production
from collect_jobs import payload, sources


class ProductionSyncTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 14, 9, 17, tzinfo=sources.CST)
        self.moment = self.now.isoformat()
        self.job = payload(dict(company='测试企业', title='法务', city='上海',
                                source=sources.JNJ_NAME, url=sources.JNJ + '/en/jobs/r-1/legal-counsel',
                                check_status='在招待确认'), self.moment)
        self.report = dict(collected_at=self.moment, rows=[self.job], extracted=1,
                           sources=[dict(source=sources.JNJ_NAME, accepted=1, returned=1)], warnings=[])

    def read(self, report):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            return production.report_rows(path, self.now)

    def test_validation_ignores_first_discovery_for_publication_and_quarantines_blocked_source(self):
        rows, counts, warnings, _ = self.read(self.report)
        self.assertEqual(rows, [self.job])
        self.assertEqual(counts[sources.JNJ_NAME]['collected'], 1)
        self.assertEqual(rows[0]['qualifications']['source_details']['source_posted_at'], '')
        self.assertFalse(warnings)
        self.report['warnings'].append(dict(source=sources.JNJ_NAME, error='do not log this', blocked=True))
        rows, _, warnings, unavailable = self.read(self.report)
        self.assertEqual(rows, [])
        self.assertIn(sources.JNJ_NAME, unavailable)
        self.assertNotIn('do not log this', str(warnings))

    def test_rejects_unsafe_or_expired_reports(self):
        for changes in ({'url': 'https://example.test'}, {'id': 'a' * 16}, {'first_seen': '2026-11-14'},
                        {'check_status': '其他'}, {'summary': 'unexpected content'}, {'cohort': True}):
            with self.subTest(changes=changes), self.assertRaises(production.SafetyStop):
                self.read({**self.report, 'rows': [{**self.job, **changes}]})
        with self.assertRaises(production.SafetyStop):
            self.read({**self.report, 'collected_at': (self.now - timedelta(hours=3)).isoformat()})
        with self.assertRaises(production.SafetyStop):
            self.read({**self.report, 'sources': [{**self.report['sources'][0], 'accepted': 0}]})

    def test_connection_rejects_wrong_project_and_role(self):
        with TemporaryDirectory() as directory:
            ca = Path(directory) / 'ca.pem'
            ca.write_text('fixture')
            env = dict(JOBRADAR_JOBS_CA_FILE=str(ca), JOBRADAR_PRODUCTION_PROJECT_REF=production.PRODUCTION_REF,
                       JOBRADAR_STAGING_PROJECT_REF=production.STAGING_REF)
            def options(project, role):
                return lambda _: dict(host='aws-0-ap-southeast-1.pooler.supabase.com', port='5432',
                                      user=f'{role}.{project}', password='fiction', dbname='postgres', sslmode='require')

            prod = options(production.PRODUCTION_REF, production.PRODUCTION_ROLE)
            stage = options(production.STAGING_REF, production.ROLE)
            self.assertEqual(production.settings(env, prod, False)[0]['sslmode'], 'verify-full')
            self.assertEqual(production.settings(env, stage, True)[1], production.ROLE)
            for parse, staging in ((prod, True), (stage, False),
                                   (options(production.STAGING_REF, production.PRODUCTION_ROLE), False),
                                   (options(production.PRODUCTION_REF, production.ROLE), False)):
                with self.subTest(staging=staging), self.assertRaises(production.ConnectionConfigError):
                    production.settings(env, parse, staging)

    def test_threshold_stops_before_sql_writes(self):
        result = MagicMock()
        result.fetchone.side_effect = [
            dict(current_user=production.PRODUCTION_ROLE, session_user=production.PRODUCTION_ROLE,
                 restricted=True, jobs_rls=True, personal_denied=True, jobs_restricted=True),
            dict(current_user=production.PRODUCTION_ROLE, session_user=production.PRODUCTION_ROLE),
        ]
        conn = MagicMock()
        conn.execute.return_value = result
        result.fetchall.return_value = []
        rows = [payload(dict(company='测试企业', title=f'法务{i}', city='上海',
                             source=sources.JNJ_NAME, url=sources.JNJ + f'/en/jobs/r-{i}/legal-counsel',
                             check_status='在招待确认'), self.moment) for i in range(production.MAX_INSERTS + 1)]
        with self.assertRaisesRegex(production.SafetyStop, '超过安全上限'):
            production.apply_batch(conn, rows, self.now.date(), dict, production.PRODUCTION_ROLE)
        self.assertFalse(any(call.args[0].startswith(('insert ', 'update ')) for call in conn.execute.call_args_list))

    def test_blocks_a_large_status_change_batch(self):
        row = self.job
        baseline = [{**row, 'id': f'{i:016x}'} for i in range(27)]
        changes = [('updated', {**job, 'check_status': '已结束'}) for job in baseline[:4]]
        with self.assertRaisesRegex(production.SafetyStop, '转为失效 4'):
            production.review_plan(baseline, changes)

    def test_insert_only_defers_updates_but_keeps_new_jobs(self):
        result = MagicMock()
        conn = MagicMock()
        conn.execute.return_value = result
        result.fetchone.return_value = dict(current_user=production.PRODUCTION_ROLE,
                                            session_user=production.PRODUCTION_ROLE)
        result.fetchall.return_value = [self.job]
        result.rowcount = 1
        new = payload(dict(company='测试企业乙', title='合规专员', city='苏州',
                           source=sources.JNJ_NAME, url=sources.JNJ + '/en/jobs/r-2/compliance-specialist',
                           check_status='在招待确认'), self.moment)
        previous = {**self.job, 'check_status': '已结束'}
        by_source = {sources.JNJ_NAME: production.Counter()}
        counts = production.publish(conn, [previous, new], self.now.date(), dict, role=production.PRODUCTION_ROLE,
                                    expire_history=False, insert_only=True, by_source=by_source,
                                    review_plan=production.review_plan)
        self.assertEqual((counts['new'], counts['updated'], counts['deferred_updates']), (1, 0, 1))
        self.assertEqual(by_source[sources.JNJ_NAME]['deferred_updates'], 1)
        self.assertFalse(any(call.args[0].startswith('update ') for call in conn.execute.call_args_list))

    def test_staging_verification_checks_rollback_against_full_snapshot(self):
        conn = MagicMock()
        cursor = conn.execute.return_value
        cursor.fetchall.side_effect = [[], [self.job], []]
        with patch.object(production, 'check_permissions'), patch.object(production, 'publish') as publish:
            publish.side_effect = [dict(new=1, updated=0), dict(new=0, updated=0)]
            production.apply_batch(conn, [self.job], self.now.date(), dict, production.ROLE, rollback=True)
            conn.transaction.assert_called_once_with(force_rollback=True)
            self.assertEqual(publish.call_count, 2)
