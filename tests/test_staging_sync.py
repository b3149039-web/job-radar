from datetime import date
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from collect_jobs import plan
import staging_sync as stage


class StagingSyncTests(unittest.TestCase):
    def test_refuses_production_even_if_credential_points_there(self):
        with TemporaryDirectory() as directory:
            ca = Path(directory) / 'ca.pem'
            ca.touch()
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
