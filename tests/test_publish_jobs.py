from datetime import date
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from publish_jobs import connection_options, historical_expirations


class PublishJobsTests(unittest.TestCase):
    def test_only_known_past_deadline_expires(self):
        rows = [
            {'id': 'a', 'check_status': '在招待确认', 'qualifications': {'source_details': {'deadline': '2026-11-12'}}},
            {'id': 'b', 'check_status': '在招待确认', 'qualifications': {}},
            {'id': 'c', 'check_status': '在招待确认', 'qualifications': {'source_details': {'deadline': '2026-11-13'}}},
        ]
        self.assertEqual(list(historical_expirations(rows, date(2026, 11, 13))), ['a'])

    def test_only_restricted_verified_tls_connection(self):
        sample = dict(host='aws-0-ap-southeast-1.pooler.supabase.com', dbname='postgres',
                      user='jobradar_collector.example', password='fixture')
        with TemporaryDirectory() as directory:
            ca = Path(directory) / 'fixture.pem'
            ca.write_text('fixture CA')
            self.assertEqual(connection_options('fixture', lambda _: sample, str(ca), 'example')['sslmode'], 'verify-full')
            with self.assertRaises(ValueError):
                connection_options('fixture', lambda _: sample, str(ca), 'other-project')
            for change in ({'user': 'postgres.example'}, {'host': 'example.test'}, {'sslrootcert': 'unexpected.pem'},
                           *({'sslmode': mode} for mode in ('disable', 'allow', 'prefer', 'verify-ca', ''))):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    connection_options('fixture', lambda _: {**sample, **change}, str(ca))
            for mode in ('require', 'verify-full'):
                result = connection_options('fixture', lambda _: {**sample, 'sslmode': mode}, str(ca))
                self.assertEqual(result['sslmode'], 'verify-full')
                self.assertEqual(result['sslrootcert'], str(ca))
                self.assertEqual(result['gssencmode'], 'disable')
            ca.write_text('')
            with self.assertRaisesRegex(ValueError, 'CA'):
                connection_options('fixture', lambda _: sample, str(ca))
